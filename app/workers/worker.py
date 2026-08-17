"""The simulated visitor.

A worker executes one scenario as a browser session: it opens the site, takes
the identity the site offers, enters a forum, browses, optionally posts and
replies, and records telemetry for every step.

Everything the worker depends on is injected, so the same class runs against a
real browser, against the bundled mock site, or against a stub adapter in the
unit tests.
"""

from __future__ import annotations

import asyncio
import random
import time
import traceback
from typing import Any, Awaitable, Callable, Sequence

from app.browser.browser_manager import BrowserManager, ContextOptions
from app.browser.selectors import SelectorSet
from app.browser.session import BrowserSession
from app.content.rotation import ContentExhausted
from app.core.content_manager import ContentManager
from app.core.forum_manager import ForumSelector
from app.core.metrics_manager import MetricsManager
from app.core.session_manager import SessionManager, SessionRecord
from app.locations.location import Location
from app.scheduler.jobs import Job
from app.scheduler.scenarios import Scenario, Step, StepAction
from app.site.base_adapter import (
    ActionResult, AdapterError, BaseSiteAdapter, ForumRef, PostRef, RateLimitedError,
)
from app.site.generic_adapter import GenericForumAdapter
from app.utils.config import Settings
from app.utils.logger import WORKER_LOGGER, get_logger
from app.utils.time_utils import random_interval
from app.workers.worker_state import (
    WorkerAction, WorkerProfile, WorkerRuntime, WorkerStatus,
)

LOGGER = get_logger(WORKER_LOGGER)

AdapterFactory = Callable[
    ["Worker", SessionRecord],
    Awaitable[tuple[BaseSiteAdapter, Callable[[], Awaitable[None]]]],
]


class Worker:
    """One simulated visitor."""

    def __init__(self, profile: WorkerProfile, *, settings: Settings,
                 location: Location, scenario: Scenario,
                 content_manager: ContentManager, forum_selector: ForumSelector,
                 metrics: MetricsManager, session_manager: SessionManager,
                 browser_manager: BrowserManager | None = None,
                 selectors: SelectorSet | None = None,
                 adapter_factory: AdapterFactory | None = None,
                 stop_event: asyncio.Event | None = None,
                 pause_event: asyncio.Event | None = None,
                 rng: random.Random | None = None,
                 on_state_change: Callable[["Worker"], None] | None = None) -> None:
        self.profile = profile
        self.runtime = WorkerRuntime(profile)
        self.settings = settings
        self.location = location
        self.scenario = scenario
        self.content = content_manager
        self.forums = forum_selector
        self.metrics = metrics
        self.sessions = session_manager
        self.browser_manager = browser_manager
        self.selectors = selectors or SelectorSet()
        self.adapter_factory = adapter_factory
        self.stop_event = stop_event or asyncio.Event()
        self.pause_event = pause_event
        self._rng = rng or random.Random()
        self.on_state_change = on_state_change

        self.dry_run_findings: dict[str, Any] = {}
        self._backoff_seconds = settings.safety.backoff_initial_seconds

    # --------------------------------------------------------------- helpers
    @property
    def worker_id(self) -> str:
        """This worker's identifier."""
        return self.profile.worker_id

    def _notify(self) -> None:
        if self.on_state_change is not None:
            try:
                self.on_state_change(self)
            except Exception as exc:  # pragma: no cover - listener must not break a run
                LOGGER.debug("state listener failed: %s", exc)

    def _set(self, status: WorkerStatus | None = None,
             action: WorkerAction | None = None) -> None:
        if status is not None:
            self.runtime.set_status(status, action)
        elif action is not None:
            self.runtime.set_action(action)
        self._notify()

    async def _sleep(self, seconds: float) -> bool:
        """Interruptible sleep.  Returns False when a stop was requested."""
        if seconds <= 0:
            return not self.stop_event.is_set()
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return True
        return False

    async def _await_resume(self) -> None:
        if self.pause_event is not None and not self.pause_event.is_set():
            self._set(WorkerStatus.PAUSED, WorkerAction.SLEEP)
            await self.pause_event.wait()
            self._set(WorkerStatus.ACTIVE)

    # ---------------------------------------------------------------- adapter
    async def _create_adapter(self, record: SessionRecord
                              ) -> tuple[BaseSiteAdapter, Callable[[], Awaitable[None]]]:
        """Build the site adapter for one session (browser-backed by default)."""
        if self.adapter_factory is not None:
            return await self.adapter_factory(self, record)

        if self.browser_manager is None:
            raise RuntimeError(
                "Worker requires either a browser manager or an adapter factory")

        options = ContextOptions(
            locale=self.location.locale,
            timezone_id=self.location.timezone,
            viewport_width=self.settings.browser.viewport_width,
            viewport_height=self.settings.browser.viewport_height,
            proxy_url=self.location.proxy_url,
            location_key=self.location.key,
            identify_as=self.settings.website.identify_as,
            ignore_https_errors=self.settings.browser.ignore_https_errors,
        )
        browser_session = BrowserSession(
            self.browser_manager, options,
            test_run_id=record.test_run_id, worker_id=self.worker_id,
            session_id=record.session_id,
            capture_console=self.settings.browser.capture_console,
            capture_network=self.settings.browser.capture_network,
            debug_directory=self.settings.reporting.debug_directory,
            screenshot_directory=self.settings.reporting.screenshot_directory,
        )
        await browser_session.open()
        adapter = GenericForumAdapter(
            browser_session, self.settings.website.url,
            selectors=self.selectors, dry_run=self.settings.dry_run,
            rate_limit_status_codes=tuple(self.settings.safety.rate_limit_status_codes),
            typing_delay_ms=(self.settings.browser.typing_delay_min_ms,
                             self.settings.browser.typing_delay_max_ms),
            human_typing=self.settings.browser.human_typing,
            rng=self._rng,
        )
        self._browser_session = browser_session

        async def _close() -> None:
            await browser_session.close()

        return adapter, _close

    # ------------------------------------------------------------- telemetry
    def _record(self, record: SessionRecord, result: ActionResult, *,
                forum: str = "", content_ref: str = "") -> None:
        """Persist one adapter action as an execution + event."""
        status = "OK" if result.ok else "FAILED"
        record.record_action(ok=result.ok, duration=result.duration_ms)
        if result.ok:
            self.runtime.note_success()
        else:
            self.runtime.note_error(result.error or f"{result.action} failed")
        self.metrics.record_execution(
            result.action, worker_id=self.worker_id, session_id=record.session_id,
            location=self.location.key, forum=forum or record_forum(record, self),
            target_id=result.target_id, content_ref=content_ref, status=status,
            http_status=result.http_status, duration_ms=result.duration_ms,
            url=str(result.metadata.get("url", "")), detail=result.detail or result.error,
            dry_run=result.dry_run,
        )
        self.metrics.record_event(
            "action", worker_id=self.worker_id, session_id=record.session_id,
            location=self.location.key, action=result.action, result=status,
            duration_ms=result.duration_ms, payload=result.to_dict(),
        )
        LOGGER.info("%s %s", result.action, status,
                    extra={"worker_id": self.worker_id, "location": self.location.key,
                           "action": result.action, "result": status,
                           "duration_ms": result.duration_ms,
                           "test_run_id": self.metrics.test_run_id,
                           "error": result.error})

    async def _record_failure(self, record: SessionRecord, action: str, error: Exception,
                              *, adapter: BaseSiteAdapter | None = None,
                              selector: str = "") -> None:
        """Record an error, capturing debug artefacts where possible."""
        artefacts: dict[str, str] = {}
        session = getattr(adapter, "session", None)
        if (session is not None and self.settings.browser.screenshot_on_failure
                and hasattr(session, "capture_failure")):
            try:
                artefacts = await session.capture_failure(
                    action, selector=selector, error=str(error))
            except Exception as exc:  # pragma: no cover - best effort
                LOGGER.debug("failure capture failed: %s", exc)

        self.runtime.note_error(str(error))
        self.metrics.record_error(
            type(error).__name__, str(error), worker_id=self.worker_id,
            session_id=record.session_id, location=self.location.key, action=action,
            url=artefacts.get("url", ""), selector=selector,
            screenshot_path=artefacts.get("screenshot_path", ""),
            html_path=artefacts.get("html_path", ""),
            traceback_text="".join(traceback.format_exception(
                type(error), error, error.__traceback__))[:8000],
        )
        record.record_action(ok=False)
        LOGGER.error("%s failed: %s", action, error,
                     extra={"worker_id": self.worker_id, "location": self.location.key,
                            "action": action, "result": "FAILED", "error": str(error)})
        self._notify()

    # ------------------------------------------------------------------- run
    async def run_session(self, job: Job | None = None) -> dict[str, Any]:
        """Execute one visitor session and return its summary."""
        dry_run = bool(self.settings.dry_run or (job.dry_run if job else False))
        record = await self.sessions.start(
            worker_id=self.worker_id, location_key=self.location.key,
            scenario=self.scenario.name,
            session_id=job.session_id if job else "", dry_run=dry_run,
        )
        self.runtime.current_session_id = record.session_id
        self.runtime.started_at = record.started_at.isoformat()
        self._set(WorkerStatus.STARTING, WorkerAction.OPENING)
        self.metrics.session_started(self.location.key)
        self.metrics.record_event("session_start", worker_id=self.worker_id,
                                  session_id=record.session_id,
                                  location=self.location.key, action="START",
                                  result="OK", payload={"scenario": self.scenario.name,
                                                        "dry_run": dry_run})

        deadline = time.monotonic() + max(
            1.0, random_interval(self.settings.session.min_duration_seconds,
                                 self.settings.session.max_duration_seconds))
        status = "COMPLETED"
        adapter: BaseSiteAdapter | None = None
        closer: Callable[[], Awaitable[None]] | None = None
        self.dry_run_findings = {"worker_id": self.worker_id,
                                 "location": self.location.key,
                                 "dry_run": dry_run}

        try:
            adapter, closer = await self._create_adapter(record)
            adapter.dry_run = dry_run
            self._set(WorkerStatus.ACTIVE)

            for step in self.scenario.steps:
                if self.stop_event.is_set():
                    status = "STOPPED"
                    break
                await self._await_resume()
                if time.monotonic() > deadline and step.action is not StepAction.END_SESSION:
                    LOGGER.info("Session budget exhausted; ending early",
                                extra={"worker_id": self.worker_id})
                    status = "COMPLETED"
                    break
                if step.probability < 1.0 and self._rng.random() > step.probability:
                    continue
                for _ in range(step.repeat):
                    if self.stop_event.is_set():
                        status = "STOPPED"
                        break
                    keep_going = await self._execute_step(step, adapter, record)
                    if not keep_going:
                        status = "ABORTED"
                        break
                if status in {"STOPPED", "ABORTED"}:
                    break

        except RateLimitedError as exc:
            status = "RATE_LIMITED"
            await self._handle_rate_limit(record, exc)
        except asyncio.CancelledError:
            status = "CANCELLED"
            raise
        except Exception as exc:  # noqa: BLE001 - recorded, never hidden
            status = "FAILED"
            await self._record_failure(record, "SESSION", exc, adapter=adapter)
        finally:
            if adapter is not None:
                try:
                    end = await adapter.end_session()
                    self._record(record, end)
                except Exception as exc:  # pragma: no cover - best effort
                    LOGGER.debug("end_session failed: %s", exc)
            if closer is not None:
                try:
                    await closer()
                except Exception as exc:  # pragma: no cover
                    LOGGER.debug("adapter close failed: %s", exc)

            record.username = self.runtime.username
            record.avatar = self.runtime.avatar
            await self.sessions.finish(record, status=status)
            self.metrics.session_finished(
                self.location.key, duration_ms=record.duration_ms,
                success=status in {"COMPLETED", "STOPPED"},
                pages_visited=record.pages_visited)
            self.metrics.record_event(
                "session_end", worker_id=self.worker_id, session_id=record.session_id,
                location=self.location.key, action="END", result=status,
                duration_ms=record.duration_ms, payload=record.to_dict())

            if status in {"COMPLETED", "STOPPED"}:
                self.runtime.sessions_completed += 1
                self.runtime.note_success()
            else:
                self.runtime.sessions_failed += 1
            self.runtime.current_session_id = ""
            self._set(WorkerStatus.COMPLETED if status == "COMPLETED"
                      else WorkerStatus.ERROR if status == "FAILED"
                      else WorkerStatus.STOPPED, WorkerAction.IDLE)

        summary = record.to_dict()
        summary["status"] = status
        if dry_run:
            summary["dry_run_findings"] = self.dry_run_findings
        return summary

    # ------------------------------------------------------------------ steps
    async def _execute_step(self, step: Step, adapter: BaseSiteAdapter,
                            record: SessionRecord) -> bool:
        """Run one scenario step.  Returns False when the session must abort."""
        action = step.action
        try:
            if action is StepAction.OPEN_SITE:
                self._set(WorkerStatus.ACTIVE, WorkerAction.OPENING)
                result = await adapter.initialize_session()
                record.pages_visited += 1
                self._record(record, result)
                self._capture_page_metrics(record, result)
                if not result.ok:
                    return self._tolerate(record)

            elif action is StepAction.WAIT_FOR_LOAD:
                waiter = getattr(adapter, "wait_for_ready", None)
                if waiter is not None:
                    self._record(record, await waiter())

            elif action is StepAction.SELECT_AVATAR:
                self._set(action=WorkerAction.IDENTITY)
                avatars = await adapter.get_available_avatars()
                self.dry_run_findings["avatars_detected"] = [a.describe() for a in avatars]
                result = await adapter.select_avatar()
                self._record(record, result)
                if result.ok:
                    self.runtime.avatar = result.detail or result.target_id
                    record.avatar = self.runtime.avatar
                    self.dry_run_findings["avatar_selected"] = self.runtime.avatar
                else:
                    return self._tolerate(record)

            elif action is StepAction.SELECT_USERNAME:
                self._set(action=WorkerAction.IDENTITY)
                usernames = await adapter.get_available_usernames()
                self.dry_run_findings["usernames_detected"] = [u.describe()
                                                               for u in usernames]
                result = await adapter.select_username()
                self._record(record, result)
                if result.ok:
                    self.runtime.username = result.target_id or result.detail
                    record.username = self.runtime.username
                    self.dry_run_findings["username_selected"] = self.runtime.username
                else:
                    return self._tolerate(record)

            elif action is StepAction.ENTER_FORUM:
                return await self._enter_forum(adapter, record, step.forum)

            elif action is StepAction.SWITCH_FORUM:
                back = getattr(adapter, "return_to_forum_list", None)
                if back is not None:
                    self._record(record, await back())
                    record.pages_visited += 1
                return await self._enter_forum(adapter, record, step.forum,
                                               exclude_current=True)

            elif action is StepAction.BROWSE_FORUM:
                self._set(action=WorkerAction.READING)
                result = await adapter.browse_forum(depth=1)
                record.pages_visited += 1
                self._record(record, result)

            elif action is StepAction.READ_POST:
                return await self._read_post(adapter, record)

            elif action is StepAction.CREATE_POST:
                return await self._create_post(adapter, record)

            elif action is StepAction.CREATE_REPLY:
                return await self._create_reply(adapter, record)

            elif action is StepAction.THINK:
                self._set(action=WorkerAction.SLEEP)
                low, high = self._think_time(step)
                await self._sleep(random_interval(low, high))
                self._set(action=WorkerAction.READING)

            elif action is StepAction.END_SESSION:
                self._set(action=WorkerAction.ENDING)
                return True

        except RateLimitedError:
            raise
        except AdapterError as exc:
            await self._record_failure(record, action.value, exc, adapter=adapter,
                                       selector=exc.selector)
            return self._tolerate(record)
        except Exception as exc:  # noqa: BLE001
            await self._record_failure(record, action.value, exc, adapter=adapter)
            return self._tolerate(record)

        self.runtime.note_success()
        self.runtime.beat()
        return True

    def _think_time(self, step: Step) -> tuple[float, float]:
        """Think time for a THINK step.

        The scenario supplies the default pause; the session configuration is
        authoritative, so the scenario value is clamped into the operator's
        configured think-time window.
        """
        session = self.settings.session
        low = step.min_seconds or session.min_think_time_seconds
        high = step.max_seconds or session.max_think_time_seconds
        low = min(max(low, session.min_think_time_seconds), session.max_think_time_seconds)
        high = min(max(high, session.min_think_time_seconds),
                   session.max_think_time_seconds)
        return (low, high) if low <= high else (high, low)

    def _tolerate(self, record: SessionRecord) -> bool:
        """Decide whether the session may continue after a failed step."""
        limit = self.settings.safety.max_consecutive_worker_errors
        if self.runtime.consecutive_errors >= limit:
            LOGGER.error("Worker %s hit %d consecutive errors; aborting session",
                         self.worker_id, self.runtime.consecutive_errors,
                         extra={"worker_id": self.worker_id})
            return False
        return True

    def _capture_page_metrics(self, record: SessionRecord, result: ActionResult) -> None:
        """Store navigation-timing metrics returned by the adapter."""
        for key, value in result.metadata.items():
            if not isinstance(value, (int, float)) or key in {"options", "index"}:
                continue
            self.metrics.record_metric(
                key if key.endswith("_ms") else f"{key}", float(value),
                unit="ms" if key.endswith("_ms") else "count",
                worker_id=self.worker_id, session_id=record.session_id,
                location=self.location.key)

    # ------------------------------------------------------------ step bodies
    async def _enter_forum(self, adapter: BaseSiteAdapter, record: SessionRecord,
                           requested: str = "", *, exclude_current: bool = False) -> bool:
        self._set(action=WorkerAction.FORUM)
        forums = await adapter.get_forums()
        if not forums:
            await self._record_failure(
                record, "ENTER_FORUM",
                AdapterError("No forums were discovered on the site"), adapter=adapter)
            return self._tolerate(record)

        available = self.forums.register(forums)
        self.dry_run_findings["forums_detected"] = [forum.describe()
                                                    for forum in available]
        pool: Sequence[ForumRef] = available
        if exclude_current and adapter.current_forum is not None and len(available) > 1:
            pool = [forum for forum in available
                    if forum.key != adapter.current_forum.key]

        forum = self.forums.select(self.worker_id, forums=pool,
                                   override=requested or self.profile.forum)
        if forum is None:
            return self._tolerate(record)

        result = await adapter.enter_forum(forum)
        record.pages_visited += 1
        self._record(record, result, forum=forum.key)
        if result.ok:
            record.forums_visited += 1
            self.runtime.current_forum = forum.key
            self.forums.record_visit(forum.key, result.duration_ms)
            self.dry_run_findings["forum_entered"] = forum.describe()
            return True
        self.forums.record_error(forum.key)
        return self._tolerate(record)

    async def _read_post(self, adapter: BaseSiteAdapter, record: SessionRecord) -> bool:
        self._set(action=WorkerAction.READING)
        posts = await adapter.get_initial_posts(limit=50)
        self.dry_run_findings["initial_posts_detected"] = len(posts)
        if not posts:
            LOGGER.info("No initial posts in %s yet", self.runtime.current_forum,
                        extra={"worker_id": self.worker_id})
            return True
        target = self._select_reply_target(adapter, posts)
        if target is None:
            return True
        result = await adapter.read_post(target)
        record.pages_visited += 1
        self._record(record, result, forum=self.runtime.current_forum)
        self.dry_run_findings["reply_target"] = target.describe()
        return True if result.ok else self._tolerate(record)

    async def _create_post(self, adapter: BaseSiteAdapter, record: SessionRecord) -> bool:
        if not self.scenario.allow_posting or not self.runtime.can_post():
            return True
        if record.posts_created >= self.profile.max_posts_per_session:
            return True
        if self.settings.dry_run:
            # A dry run reports capability, so the probability roll is reported
            # rather than applied - the operator wants to see what a live run
            # would be able to do.
            self.dry_run_findings["post_probability"] = self.profile.post_probability
        elif self._rng.random() > self.profile.post_probability:
            self.dry_run_findings["would_create_post"] = False
            return True

        if record.posts_created > 0 or self.runtime.posts_created > 0:
            wait = random_interval(self.profile.min_post_interval_seconds,
                                   self.profile.max_post_interval_seconds)
            if wait > 0:
                self._set(WorkerStatus.WAITING, WorkerAction.SLEEP)
                LOGGER.info("Waiting %.0fs before the next post", wait,
                            extra={"worker_id": self.worker_id, "action": "POST_INTERVAL"})
                if not await self._sleep(wait):
                    return False
                self._set(WorkerStatus.ACTIVE)

        try:
            entry = self.content.next_post(self.worker_id,
                                           category=self.profile.content_category or None)
        except ContentExhausted as exc:
            await self._record_failure(record, "CREATE_POST", exc, adapter=adapter)
            return True

        self._set(action=WorkerAction.POSTING)
        self.dry_run_findings["would_create_post"] = True
        self.dry_run_findings["post_content_ref"] = entry.ref
        result = await adapter.create_post(entry.body, title=entry.title)
        self._record(record, result, forum=self.runtime.current_forum,
                     content_ref=entry.ref)
        if result.ok:
            self.runtime.last_post_at = time.monotonic()
            if not result.dry_run:
                record.posts_created += 1
                self.runtime.posts_created += 1
                self.runtime.posts_today += 1
                self.forums.record_post(self.runtime.current_forum)
            return True
        self.forums.record_error(self.runtime.current_forum)
        return self._tolerate(record)

    async def _create_reply(self, adapter: BaseSiteAdapter, record: SessionRecord) -> bool:
        if not self.scenario.allow_replying or not self.runtime.can_reply():
            return True
        if record.replies_created >= self.profile.max_replies_per_session:
            return True
        if self.settings.dry_run:
            self.dry_run_findings["reply_probability"] = self.profile.reply_probability
        elif self._rng.random() > self.profile.reply_probability:
            self.dry_run_findings["would_reply"] = False
            return True

        target = getattr(adapter, "current_post", None)
        if target is None:
            posts = await adapter.get_initial_posts(limit=50)
            self.dry_run_findings["initial_posts_detected"] = len(posts)
            if not posts:
                return True
            target = self._select_reply_target(adapter, posts)
            if target is None:
                return True
            opened = await adapter.read_post(target)
            record.pages_visited += 1
            self._record(record, opened, forum=self.runtime.current_forum)
            if not opened.ok:
                return self._tolerate(record)

        if record.replies_created > 0 or self.runtime.replies_created > 0:
            wait = random_interval(self.profile.min_reply_interval_seconds,
                                   self.profile.max_reply_interval_seconds)
            if wait > 0:
                self._set(WorkerStatus.WAITING, WorkerAction.SLEEP)
                LOGGER.info("Waiting %.0fs before the next reply", wait,
                            extra={"worker_id": self.worker_id,
                                   "action": "REPLY_INTERVAL"})
                if not await self._sleep(wait):
                    return False
                self._set(WorkerStatus.ACTIVE)

        try:
            entry = self.content.next_reply(self.worker_id,
                                            category=self.profile.reply_category or None)
        except ContentExhausted as exc:
            await self._record_failure(record, "CREATE_REPLY", exc, adapter=adapter)
            return True

        self._set(action=WorkerAction.REPLYING)
        self.dry_run_findings["would_reply"] = True
        self.dry_run_findings["reply_content_ref"] = entry.ref
        self.dry_run_findings["reply_target"] = target.describe()
        result = await adapter.create_reply(target, entry.body)
        self._record(record, result, forum=self.runtime.current_forum,
                     content_ref=entry.ref)
        if result.ok:
            self.runtime.last_reply_at = time.monotonic()
            if not result.dry_run:
                record.replies_created += 1
                self.runtime.replies_created += 1
                self.runtime.replies_today += 1
                self.forums.record_reply(self.runtime.current_forum)
            return True
        self.forums.record_error(self.runtime.current_forum)
        return self._tolerate(record)

    def _select_reply_target(self, adapter: BaseSiteAdapter,
                             posts: Sequence[PostRef]) -> PostRef | None:
        chooser = getattr(adapter, "select_reply_target", None)
        config = self.settings.replies
        if chooser is not None:
            return chooser(posts, mode=config.target_selection_mode,
                           keywords=config.target_filter_keywords,
                           specific_post_id=config.specific_post_id)
        return self._rng.choice(list(posts)) if posts else None

    # -------------------------------------------------------------- back-off
    async def _handle_rate_limit(self, record: SessionRecord,
                                 exc: RateLimitedError) -> None:
        """Record the throttling signal and back off; never work around it."""
        self.runtime.rate_limit_hits += 1
        self._set(WorkerStatus.BACKOFF, WorkerAction.SLEEP)
        self.metrics.record_error(
            "RateLimited", str(exc), worker_id=self.worker_id,
            session_id=record.session_id, location=self.location.key,
            action="RATE_LIMIT", http_status=exc.status)
        delay = exc.retry_after or self._backoff_seconds
        delay = min(delay, self.settings.safety.backoff_max_seconds)
        LOGGER.warning("Rate limited (HTTP %s); backing off %.0fs", exc.status, delay,
                       extra={"worker_id": self.worker_id, "location": self.location.key,
                              "action": "RATE_LIMIT", "result": "BACKOFF"})
        self._backoff_seconds = min(
            self._backoff_seconds * self.settings.safety.backoff_multiplier,
            self.settings.safety.backoff_max_seconds)
        await self._sleep(delay)

    # ------------------------------------------------------------------ views
    def snapshot(self) -> dict[str, Any]:
        """Runtime snapshot for the live monitor."""
        return self.runtime.snapshot()


def record_forum(record: SessionRecord, worker: "Worker") -> str:
    """Best-effort current forum for telemetry rows."""
    return worker.runtime.current_forum or ""
