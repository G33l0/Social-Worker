"""The Social Worker controller.

Owns the whole test run: configuration, database, content, locations, workers,
scheduling, metrics and reporting.  The CLI and the (optional) FastAPI control
API are both thin layers over this class.

    controller = Controller(settings)
    await controller.start()          # non-blocking; scheduler runs in the background
    await controller.wait()           # block until the plan finishes
    await controller.stop_all()       # emergency stop
"""

from __future__ import annotations

import asyncio
import random
from pathlib import Path
from typing import Any, Sequence

from sqlalchemy import func, select

from app.browser.browser_manager import BrowserManager, BrowserUnavailableError
from app.browser.selectors import SelectorSet
from app.core.content_manager import ContentManager
from app.core.forum_manager import ForumSelector
from app.core.metrics_manager import MetricsManager
from app.core.session_manager import SessionManager
from app.core.worker_manager import WorkerManager
from app.database.database import Database, get_database
from app.database.migrations import upgrade
from app.database.models import Forum as ForumRow
from app.database.models import Location as LocationRow
from app.database.models import TestRun
from app.locations.allocation import RateLimiter
from app.locations.location_manager import LocationManager
from app.reporting.report_manager import ReportManager
from app.scheduler.jobs import Job, build_job
from app.scheduler.scenarios import ScenarioLibrary
from app.scheduler.scheduler import SchedulerState, SessionScheduler
from app.utils.config import ConfigPaths, Settings, load_settings
from app.utils.ids import new_test_run_id
from app.utils.logger import configure_logging, get_logger
from app.utils.time_utils import date_slug, utc_now
from app.workers.heartbeat import HeartbeatMonitor
from app.workers.worker import AdapterFactory, Worker
from app.workers.worker_pool import WorkerPool
from app.workers.worker_state import WorkerProfile, WorkerStatus

LOGGER = get_logger("controller")


class AuthorizationError(RuntimeError):
    """Raised when a run is attempted without the operator's authorization flag."""


class ControllerState:
    """Controller lifecycle values."""

    IDLE = "IDLE"
    PREPARED = "PREPARED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    COMPLETED = "COMPLETED"


class Controller:
    """Central orchestration for one Social Worker test run."""

    def __init__(self, settings: Settings | None = None, *,
                 database: Database | None = None,
                 config_paths: ConfigPaths | None = None,
                 adapter_factory: AdapterFactory | None = None,
                 selectors: SelectorSet | None = None,
                 rng: random.Random | None = None) -> None:
        self.paths = config_paths or ConfigPaths()
        self.settings = settings or load_settings(self.paths.settings)
        configure_logging(self.settings.logging.directory,
                          level=self.settings.logging.level,
                          console=self.settings.logging.console,
                          console_level=self.settings.logging.console_level,
                          max_bytes=self.settings.logging.max_bytes,
                          backups=self.settings.logging.backups)

        self.database = database or get_database(self.settings.database, refresh=True)
        self.adapter_factory = adapter_factory
        self.selectors = selectors or SelectorSet.load(self.paths.selectors)
        self._rng = rng or random.Random()

        self.locations = LocationManager.load(self.paths.locations, self.paths.schedules)
        self.scenarios = ScenarioLibrary.load(self.paths.scenarios)
        self.workers = WorkerManager(self.settings, self.locations)
        self.workers.load(self.paths.workers)
        self.content = ContentManager(self.settings.content, self.settings.posting,
                                      self.settings.replies)
        self.forum_selector = ForumSelector(self.settings.forums, rng=self._rng)
        self.reports = ReportManager(self.database,
                                     output_directory=self.settings.reporting.output_directory)

        self.state = ControllerState.IDLE
        self.test_run_id = ""
        self.metrics: MetricsManager | None = None
        self.sessions: SessionManager | None = None
        self.pool: WorkerPool | None = None
        self.scheduler: SessionScheduler | None = None
        self.browser_manager: BrowserManager | None = None
        self.heartbeat = HeartbeatMonitor(
            timeout_seconds=self.settings.controller.heartbeat_timeout_seconds,
            interval_seconds=self.settings.controller.heartbeat_interval_seconds)

        self._stop_event = asyncio.Event()
        self._pause_event = asyncio.Event()
        self._pause_event.set()
        self._run_task: asyncio.Task[Any] | None = None
        self._started_at = None
        self._plan: list[WorkerProfile] = []
        self._dry_run_findings: list[dict[str, Any]] = []

    # ------------------------------------------------------------- validation
    def preflight(self, *, dry_run: bool = False) -> list[str]:
        """Return blocking problems that must be fixed before a run starts."""
        problems: list[str] = []
        website = self.settings.website
        if not website.authorized_test_mode:
            problems.append(
                "Authorized test mode is OFF. Social Worker only generates traffic "
                "against systems you own or have written permission to test. Enable it "
                "in Configure Website once that authorization is in place.")
        if website.environment.value == "production" and not website.authorization_reference:
            problems.append(
                "Production targets require an authorization reference (ticket, contract "
                "or written approval) to be recorded in the configuration.")
        if not website.url:
            problems.append("No website URL configured")

        ready, content_problems = self.content.is_ready()
        if not ready and not dry_run:
            problems.extend(content_problems)

        if not self.locations.enabled():
            problems.append("No enabled geographic test locations")

        return problems

    # -------------------------------------------------------------- test runs
    def _next_test_run_id(self) -> str:
        today = date_slug()

        def _query(session: Any) -> int:
            stmt = select(func.count(TestRun.id)).where(
                TestRun.test_run_id.like(f"TEST-{today}-%"))
            return int(session.execute(stmt).scalar_one())

        try:
            existing = self.database.read(_query)
        except Exception:  # pragma: no cover - schema may not exist yet
            existing = 0
        return new_test_run_id(existing + 1)

    async def prepare(self, *, label: str = "", dry_run: bool = False,
                      force: bool = False) -> str:
        """Validate, initialise the database and build the run plan."""
        upgrade(self.database)
        self.settings.dry_run = dry_run or self.settings.dry_run

        if self.settings.content.reload_on_start:
            self.content.load()
        problems = self.preflight(dry_run=self.settings.dry_run)
        if problems and not force:
            raise AuthorizationError("; ".join(problems))
        for problem in problems:
            LOGGER.warning("Preflight (forced past): %s", problem)

        self.test_run_id = self._next_test_run_id()
        self.metrics = MetricsManager(self.database, self.test_run_id)
        self.sessions = SessionManager(self.database, self.test_run_id)

        self._plan = self.workers.plan()
        allocation_rows = {}
        for profile in self._plan:
            allocation_rows[profile.location_key] = \
                allocation_rows.get(profile.location_key, 0) + 1

        await self._create_test_run_row(label)
        await self._persist_locations(allocation_rows)
        await self.workers.persist(self.database, self.test_run_id, self._plan)
        self.content.sync_to_database(self.database)

        self.browser_manager = None
        if self.adapter_factory is None:
            self.browser_manager = BrowserManager(self.settings.browser,
                                                  self.settings.website)

        self._stop_event = asyncio.Event()
        self._pause_event = asyncio.Event()
        self._pause_event.set()

        self.pool = WorkerPool(
            settings=self.settings, content_manager=self.content,
            forum_selector=self.forum_selector, metrics=self.metrics,
            session_manager=self.sessions, scenarios=self.scenarios,
            browser_manager=self.browser_manager, selectors=self.selectors,
            adapter_factory=self.adapter_factory, heartbeat=self.heartbeat,
            stop_event=self._stop_event, pause_event=self._pause_event,
            rng=self._rng, on_state_change=self._on_worker_state,
        )
        locations = {location.key: location for location in self.locations.all()}
        self.pool.create_many(self._plan, locations)

        concurrency = self.locations.concurrency_controller(
            self.settings.concurrency.max_global_workers,
            per_worker_limit=self.settings.concurrency.max_concurrent_sessions_per_worker)
        rate_limiter = RateLimiter(
            per_minute=self.settings.concurrency.max_requests_per_minute,
            per_hour=self.settings.concurrency.max_sessions_per_hour,
            per_day=self.settings.concurrency.max_sessions_per_day)
        self.scheduler = SessionScheduler(
            concurrency=concurrency, runner=self._run_job,
            location_manager=self.locations, rate_limiter=rate_limiter,
            ramp_up_seconds=self.settings.concurrency.ramp_up_seconds)

        self.state = ControllerState.PREPARED
        LOGGER.info("Test run %s prepared: %d worker(s), %d location(s), dry_run=%s",
                    self.test_run_id, len(self._plan), len(allocation_rows),
                    self.settings.dry_run)
        return self.test_run_id

    async def _create_test_run_row(self, label: str) -> None:
        settings_snapshot = self.settings.model_dump(mode="json")
        # Never persist controller credentials with the run snapshot.
        settings_snapshot.get("controller", {}).pop("api_token", None)

        def _write(session: Any) -> None:
            session.add(TestRun(
                test_run_id=self.test_run_id,
                label=label,
                target_url=self.settings.website.url,
                environment=self.settings.website.environment.value,
                status="PREPARED",
                dry_run=self.settings.dry_run,
                authorized=self.settings.website.authorized_test_mode,
                authorization_reference=self.settings.website.authorization_reference,
                scenario=self.settings.session.default_scenario,
                started_at=utc_now(),
                config_snapshot=settings_snapshot,
            ))

        await self.database.run(_write)

    async def _persist_locations(self, allocation: dict[str, int]) -> None:
        def _write(session: Any) -> None:
            for location in self.locations.all():
                session.add(LocationRow(
                    test_run_id=self.test_run_id,
                    key=location.key,
                    name=location.name,
                    region_group=location.region_group,
                    country=location.country,
                    region=location.region,
                    city=location.city,
                    timezone=location.timezone,
                    locale=location.locale,
                    enabled=location.enabled,
                    allocated_workers=allocation.get(location.key, 0),
                    max_concurrent_workers=location.max_concurrent_workers,
                    runner=location.runner.value,
                    schedule=location.schedule,
                ))

        await self.database.run(_write)

    # ------------------------------------------------------------------- jobs
    def build_jobs(self) -> list[Job]:
        """Create the session jobs for every planned worker."""
        jobs: list[Job] = []
        for profile in self._plan:
            for index in range(max(1, profile.sessions)):
                jobs.append(build_job(
                    profile.worker_id, profile.location_key, profile.scenario,
                    test_run_id=self.test_run_id, forum=profile.forum,
                    dry_run=self.settings.dry_run, sequence=index + 1,
                ))
        return jobs

    async def _run_job(self, job: Job) -> dict[str, Any]:
        """Scheduler callback: execute one session with the owning worker."""
        assert self.pool is not None
        worker = self.pool.get(job.worker_id)
        self.heartbeat.beat(job.worker_id, status=WorkerStatus.ACTIVE.value)
        summary = await worker.run_session(job)
        self.heartbeat.beat(job.worker_id, status=worker.runtime.status.value)
        if self.settings.dry_run and worker.dry_run_findings:
            self._dry_run_findings.append(dict(worker.dry_run_findings))
        await self._check_safety_thresholds()
        return summary

    def _on_worker_state(self, worker: Worker) -> None:
        self.heartbeat.beat(worker.worker_id, status=worker.runtime.status.value,
                            action=worker.runtime.action.value)

    async def _check_safety_thresholds(self) -> None:
        """Abort the run when the observed error rate breaches the safety limit."""
        if self.metrics is None:
            return
        stats = self.metrics.global_stats
        samples = stats.successful_actions + stats.failed_actions
        threshold = self.settings.safety.abort_on_error_rate
        if (samples >= self.settings.safety.abort_error_rate_min_samples
                and stats.error_rate >= threshold > 0):
            LOGGER.error("Error rate %.1f%% exceeded the %.1f%% abort threshold; "
                         "stopping the test run", stats.error_rate * 100, threshold * 100)
            await self._abort("error-rate-threshold")
        if (self.settings.safety.respect_rate_limits
                and stats.rate_limit_events >= self.settings.safety.stop_on_rate_limit_streak):
            LOGGER.error("Target reported rate limiting %d times; stopping the run so "
                         "the site is not pressured further", stats.rate_limit_events)
            await self._abort("rate-limit")

    async def _abort(self, reason: str) -> None:
        """Stop the run from inside a worker task.

        Unlike :meth:`stop_all` this never cancels the caller (a safety abort is
        raised from within a running session), so it drains the queue and asks
        every worker to finish at its next step instead.
        """
        if self.state is ControllerState.STOPPING:
            return
        self.state = ControllerState.STOPPING
        self._stop_event.set()
        self._pause_event.set()
        if self.pool is not None:
            self.pool.stop_all()
        if self.scheduler is not None:
            self.scheduler.stop()
            dropped = await self.scheduler.queue.drain()
            LOGGER.warning("Run aborted (%s): %d queued session(s) dropped",
                           reason, dropped)

    # ------------------------------------------------------------------ start
    async def start(self, *, label: str = "", dry_run: bool = False,
                    force: bool = False, wait: bool = False) -> str:
        """Prepare (if needed) and start dispatching sessions."""
        if self.state in {ControllerState.RUNNING, ControllerState.PAUSED}:
            raise RuntimeError("A test run is already in progress")
        if self.state is not ControllerState.PREPARED or dry_run:
            await self.prepare(label=label, dry_run=dry_run, force=force)

        assert self.scheduler is not None and self.metrics is not None
        if self.browser_manager is not None:
            try:
                await self.browser_manager.start()
            except BrowserUnavailableError:
                await self._set_run_status("FAILED")
                raise

        await self.metrics.start()
        await self.heartbeat.start()
        await self.scheduler.submit(self.build_jobs())
        await self._set_run_status("RUNNING")
        self.state = ControllerState.RUNNING
        self._started_at = utc_now()
        LOGGER.info("Test run %s started", self.test_run_id)

        self._run_task = asyncio.create_task(self._run(), name="controller-run")
        if wait:
            await self.wait()
        return self.test_run_id

    async def _run(self) -> dict[str, Any]:
        assert self.scheduler is not None
        try:
            stats = await self.scheduler.run()
        except asyncio.CancelledError:  # pragma: no cover - stop path
            raise
        finally:
            await self._finalise()
        return stats

    async def wait(self) -> dict[str, Any]:
        """Block until the run finishes; returns the final status snapshot."""
        if self._run_task is not None:
            try:
                await self._run_task
            except asyncio.CancelledError:  # pragma: no cover
                pass
        return self.status()

    # ---------------------------------------------------------------- control
    async def pause(self) -> None:
        """Pause dispatching (running sessions continue to their next step)."""
        if self.scheduler is None:
            return
        self.scheduler.pause()
        self._pause_event.clear()
        self.state = ControllerState.PAUSED
        await self._set_run_status("PAUSED")

    async def resume(self) -> None:
        """Resume a paused run."""
        if self.scheduler is None:
            return
        self.scheduler.resume()
        self._pause_event.set()
        self.state = ControllerState.RUNNING
        await self._set_run_status("RUNNING")

    async def stop(self) -> dict[str, Any]:
        """Graceful stop: no new sessions, in-flight sessions finish."""
        if self.scheduler is None:
            return self.status()
        self.state = ControllerState.STOPPING
        self.scheduler.stop()
        self._pause_event.set()
        await self.wait()
        return self.status()

    async def stop_all(self, *, reason: str = "operator") -> dict[str, Any]:
        """Emergency stop: cancel every queued and running session immediately."""
        LOGGER.warning("STOP ALL requested (%s)", reason)
        self.state = ControllerState.STOPPING
        self._stop_event.set()
        self._pause_event.set()
        if self.pool is not None:
            self.pool.stop_all()
        result: dict[str, Any] = {}
        if self.scheduler is not None:
            result = await self.scheduler.stop_all()
        current = asyncio.current_task()
        if (self._run_task is not None and not self._run_task.done()
                and current is not self._run_task):
            try:
                await asyncio.wait_for(asyncio.shield(self._run_task), timeout=30)
            except (asyncio.TimeoutError, asyncio.CancelledError):  # pragma: no cover
                self._run_task.cancel()
        else:
            await self._finalise()
        self.state = ControllerState.STOPPED
        result["state"] = self.state
        return result

    async def stop_location(self, location_key: str) -> dict[str, Any]:
        """Stop every worker in one location."""
        if self.pool is not None:
            self.pool.stop_location(location_key)
        if self.scheduler is None:
            return {}
        return await self.scheduler.stop_location(location_key)

    async def stop_worker(self, worker_id: str) -> dict[str, Any]:
        """Stop one worker."""
        if self.pool is not None:
            self.pool.stop_worker(worker_id)
        if self.scheduler is None:
            return {}
        return await self.scheduler.stop_worker(worker_id)

    # ------------------------------------------------------------- finalising
    async def _finalise(self) -> None:
        """Flush telemetry, persist final counters and export reports."""
        if self.state in {ControllerState.COMPLETED, ControllerState.IDLE}:
            return
        if self.metrics is not None:
            await self.metrics.flush()
        if self.pool is not None:
            await self.workers.sync_runtime(self.database, self.test_run_id,
                                            self.pool.all())
        await self._persist_forums()
        if self.metrics is not None:
            await self.metrics.stop()
        await self.heartbeat.stop()
        if self.browser_manager is not None:
            await self.browser_manager.stop()

        status = "STOPPED" if self.state == ControllerState.STOPPING else "COMPLETED"
        await self._set_run_status(status, closing=True)
        self.state = (ControllerState.STOPPED if status == "STOPPED"
                      else ControllerState.COMPLETED)

        if self.settings.reporting.auto_export_on_stop:
            try:
                exported = self.reports.export_all(
                    self.test_run_id, formats=self.settings.reporting.formats)
                LOGGER.info("Exported %d report file(s)", len(exported))
            except Exception as exc:  # pragma: no cover - reporting must not break stop
                LOGGER.error("Report export failed: %s", exc)
        LOGGER.info("Test run %s finalised (%s)", self.test_run_id, status)

    async def _persist_forums(self) -> None:
        rows = self.forum_selector.rows()
        if not rows:
            return

        def _write(session: Any) -> None:
            for row in rows:
                record = session.query(ForumRow).filter_by(
                    test_run_id=self.test_run_id, key=row["key"]).one_or_none()
                if record is None:
                    record = ForumRow(test_run_id=self.test_run_id, key=row["key"])
                    session.add(record)
                record.name = row["name"]
                record.url = row["url"]
                record.weight = row["weight"]
                record.visits = row["visits"]
                record.posts = row["posts"]
                record.replies = row["replies"]
                record.errors = row["errors"]
                record.avg_load_ms = row["avg_load_ms"]

        await self.database.run(_write)

    async def _set_run_status(self, status: str, *, closing: bool = False) -> None:
        if not self.test_run_id:
            return

        def _write(session: Any) -> None:
            row = session.query(TestRun).filter_by(
                test_run_id=self.test_run_id).one_or_none()
            if row is None:  # pragma: no cover - defensive
                return
            row.status = status
            if closing:
                row.ended_at = utc_now()
                if row.started_at:
                    row.duration_seconds = (row.ended_at - row.started_at).total_seconds()

        await self.database.run(_write)

    # ------------------------------------------------------------------ modes
    async def run_dry(self, *, workers: int = 1, label: str = "dry-run") -> dict[str, Any]:
        """Walk the site without submitting posts or replies."""
        original_sessions = self.settings.session.sessions_per_worker
        original_limit = self.settings.concurrency.max_global_workers
        self.settings.session.sessions_per_worker = 1
        self.settings.concurrency.max_global_workers = max(1, workers)
        self._dry_run_findings = []
        try:
            await self.start(label=label, dry_run=True, force=True, wait=True)
        finally:
            self.settings.session.sessions_per_worker = original_sessions
            self.settings.concurrency.max_global_workers = original_limit
            self.settings.dry_run = False
        return {
            "test_run_id": self.test_run_id,
            "findings": self._dry_run_findings,
            "status": self.status(),
        }

    async def run_single_worker(self, *, location_key: str = "", scenario: str = "",
                                dry_run: bool = False) -> dict[str, Any]:
        """Run exactly one worker for one session (menu option 11)."""
        original_plan_limit = self.settings.concurrency.max_global_workers
        original_sessions = self.settings.session.sessions_per_worker
        self.settings.concurrency.max_global_workers = 1
        self.settings.session.sessions_per_worker = 1

        keep = dict(self.workers.profiles)
        try:
            location = (self.locations.get(location_key) if location_key
                        else (self.locations.enabled() or self.locations.all())[0])
            profile = self.workers.template(location.key)
            if scenario:
                profile.scenario = scenario
            self.workers.profiles = {profile.worker_id: profile}
            await self.start(label="single-worker", dry_run=dry_run, force=True,
                             wait=True)
            summary = self.status()
            summary["worker"] = profile.model_dump(mode="json")
            if self._dry_run_findings:
                summary["findings"] = self._dry_run_findings
            return summary
        finally:
            self.workers.profiles = keep
            self.settings.concurrency.max_global_workers = original_plan_limit
            self.settings.session.sessions_per_worker = original_sessions
            self.settings.dry_run = False

    # ----------------------------------------------------------------- status
    def status(self) -> dict[str, Any]:
        """Live status snapshot used by the dashboard and the CLI."""
        snapshot: dict[str, Any] = {
            "state": self.state,
            "test_run_id": self.test_run_id,
            "target": self.settings.website.url,
            "environment": self.settings.website.environment.value,
            "dry_run": self.settings.dry_run,
            "authorized": self.settings.website.authorized_test_mode,
            "workers_planned": len(self._plan),
            "started_at": self._started_at.isoformat() if self._started_at else "",
        }
        if self.metrics is not None:
            snapshot.update(self.metrics.snapshot())
        if self.scheduler is not None:
            snapshot["scheduler"] = self.scheduler.snapshot()
        if self.pool is not None:
            snapshot["workers"] = self.pool.rows()
            snapshot["worker_status_counts"] = self.pool.status_counts()
        if self.sessions is not None:
            snapshot["sessions"] = self.sessions.snapshot()
        snapshot["forums"] = self.forum_selector.rows()
        snapshot["heartbeats"] = self.heartbeat.snapshot()
        if self.browser_manager is not None:
            snapshot["browser"] = self.browser_manager.snapshot()
        return snapshot

    @property
    def is_running(self) -> bool:
        """True while sessions are being dispatched."""
        return self.state in {ControllerState.RUNNING, ControllerState.PAUSED}

    async def shutdown(self) -> None:
        """Release every resource (called when the CLI exits)."""
        if self.is_running:
            await self.stop_all(reason="shutdown")
        if self.metrics is not None:
            await self.metrics.stop()
        await self.heartbeat.stop()
        if self.browser_manager is not None:
            await self.browser_manager.stop()
        self.database.dispose()
        LOGGER.info("Controller shut down")

    # ------------------------------------------------------------- reporting
    def export_reports(self, formats: Sequence[str] | None = None,
                       test_run_id: str = "") -> list[Path]:
        """Export every report category for a test run."""
        target = test_run_id or self.test_run_id
        if not target:
            raise ValueError("No test run to export")
        return self.reports.export_all(
            target, formats=list(formats or self.settings.reporting.formats))
