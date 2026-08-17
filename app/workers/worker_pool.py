"""Worker pool: builds and holds the Worker objects for a test run."""

from __future__ import annotations

import asyncio
import random
from typing import Any, Callable, Iterable

from app.browser.browser_manager import BrowserManager
from app.browser.selectors import SelectorSet
from app.core.content_manager import ContentManager
from app.core.forum_manager import ForumSelector
from app.core.metrics_manager import MetricsManager
from app.core.session_manager import SessionManager
from app.locations.location import Location
from app.scheduler.scenarios import Scenario, ScenarioLibrary
from app.utils.config import Settings
from app.utils.logger import get_logger
from app.workers.heartbeat import HeartbeatMonitor
from app.workers.worker import AdapterFactory, Worker
from app.workers.worker_state import WorkerProfile, WorkerStatus

LOGGER = get_logger("workers.pool")


class WorkerPool:
    """Creates workers, exposes them by id/location and tracks their state."""

    def __init__(self, *, settings: Settings, content_manager: ContentManager,
                 forum_selector: ForumSelector, metrics: MetricsManager,
                 session_manager: SessionManager,
                 scenarios: ScenarioLibrary | None = None,
                 browser_manager: BrowserManager | None = None,
                 selectors: SelectorSet | None = None,
                 adapter_factory: AdapterFactory | None = None,
                 heartbeat: HeartbeatMonitor | None = None,
                 stop_event: asyncio.Event | None = None,
                 pause_event: asyncio.Event | None = None,
                 rng: random.Random | None = None,
                 on_state_change: Callable[[Worker], None] | None = None) -> None:
        self.settings = settings
        self.content_manager = content_manager
        self.forum_selector = forum_selector
        self.metrics = metrics
        self.session_manager = session_manager
        self.scenarios = scenarios or ScenarioLibrary()
        self.browser_manager = browser_manager
        self.selectors = selectors or SelectorSet()
        self.adapter_factory = adapter_factory
        self.heartbeat = heartbeat
        self.stop_event = stop_event or asyncio.Event()
        self.pause_event = pause_event
        self._rng = rng or random.Random()
        self.on_state_change = on_state_change
        self._workers: dict[str, Worker] = {}

    # ------------------------------------------------------------------ build
    def create(self, profile: WorkerProfile, location: Location,
               scenario: Scenario | None = None) -> Worker:
        """Instantiate one worker and register it in the pool."""
        chosen = scenario or self.scenarios.get(profile.scenario)
        worker = Worker(
            profile,
            settings=self.settings,
            location=location,
            scenario=chosen,
            content_manager=self.content_manager,
            forum_selector=self.forum_selector,
            metrics=self.metrics,
            session_manager=self.session_manager,
            browser_manager=self.browser_manager,
            selectors=self.selectors,
            adapter_factory=self.adapter_factory,
            stop_event=self.stop_event,
            pause_event=self.pause_event,
            rng=random.Random(self._rng.random()),
            on_state_change=self.on_state_change,
        )
        worker.runtime.set_status(WorkerStatus.READY)
        self._workers[profile.worker_id] = worker
        self.metrics.global_stats.workers_registered = len(self._workers)
        if self.heartbeat is not None:
            self.heartbeat.register(profile.worker_id, location_key=location.key,
                                    runner=location.runner.value)
        return worker

    def create_many(self, profiles: Iterable[WorkerProfile],
                    locations: dict[str, Location]) -> list[Worker]:
        """Instantiate a batch of workers."""
        created: list[Worker] = []
        for profile in profiles:
            location = locations.get(profile.location_key)
            if location is None:
                LOGGER.error("Worker %s references unknown location %s; skipped",
                             profile.worker_id, profile.location_key)
                continue
            created.append(self.create(profile, location))
        LOGGER.info("Worker pool built with %d worker(s)", len(created))
        return created

    # ------------------------------------------------------------------ views
    def get(self, worker_id: str) -> Worker:
        """Return a worker by id."""
        try:
            return self._workers[worker_id]
        except KeyError as exc:
            raise KeyError(f"Unknown worker: {worker_id}") from exc

    def all(self) -> list[Worker]:
        """Every worker, sorted by id."""
        return [self._workers[key] for key in sorted(self._workers)]

    def by_location(self, location_key: str) -> list[Worker]:
        """Workers assigned to a location."""
        return [worker for worker in self.all()
                if worker.profile.location_key == location_key]

    def __len__(self) -> int:
        return len(self._workers)

    def __contains__(self, worker_id: object) -> bool:
        return worker_id in self._workers

    # ---------------------------------------------------------------- control
    def stop_worker(self, worker_id: str) -> None:
        """Ask one worker to stop after its current step."""
        worker = self.get(worker_id)
        worker.stop_event.set()
        worker.runtime.set_status(WorkerStatus.STOPPING)

    def stop_location(self, location_key: str) -> int:
        """Ask every worker in a location to stop."""
        count = 0
        for worker in self.by_location(location_key):
            worker.stop_event.set()
            worker.runtime.set_status(WorkerStatus.STOPPING)
            count += 1
        return count

    def stop_all(self) -> int:
        """Ask every worker to stop (emergency stop)."""
        self.stop_event.set()
        for worker in self.all():
            worker.stop_event.set()
            worker.runtime.set_status(WorkerStatus.STOPPING)
        return len(self._workers)

    def rows(self, limit: int = 0) -> list[dict[str, Any]]:
        """Live-monitor rows for every worker."""
        rows = [worker.runtime.to_row() for worker in self.all()]
        return rows[:limit] if limit else rows

    def status_counts(self) -> dict[str, int]:
        """Worker counts grouped by status."""
        counts: dict[str, int] = {}
        for worker in self.all():
            counts[worker.runtime.status.value] = \
                counts.get(worker.runtime.status.value, 0) + 1
        return counts
