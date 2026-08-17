"""Session scheduler.

Pulls jobs off the queue and dispatches them to the worker pool while
respecting - in this order - the pause/stop state, the location schedule, the
global request/session rate limits, and the global + per-location + per-worker
concurrency ceilings.
"""

from __future__ import annotations

import asyncio
import random
from enum import Enum
from typing import Any, Awaitable, Callable, Iterable

from app.locations.allocation import ConcurrencyController, RateLimiter
from app.locations.location_manager import LocationManager
from app.scheduler.jobs import Job, JobQueue, JobStatus
from app.utils.logger import get_logger

LOGGER = get_logger("scheduler")

JobRunner = Callable[[Job], Awaitable[dict[str, Any]]]


class SchedulerState(str, Enum):
    """Scheduler lifecycle."""

    IDLE = "IDLE"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"


class SessionScheduler:
    """Dispatches session jobs under every configured safety ceiling."""

    def __init__(self, *, concurrency: ConcurrencyController,
                 runner: JobRunner,
                 location_manager: LocationManager | None = None,
                 rate_limiter: RateLimiter | None = None,
                 ramp_up_seconds: float = 0.0,
                 jitter_seconds: float = 0.0,
                 respect_schedules: bool = True) -> None:
        self.concurrency = concurrency
        self.runner = runner
        self.location_manager = location_manager
        self.rate_limiter = rate_limiter
        self.ramp_up_seconds = max(0.0, ramp_up_seconds)
        self.jitter_seconds = max(0.0, jitter_seconds)
        self.respect_schedules = respect_schedules

        self.queue = JobQueue()
        self.state = SchedulerState.IDLE
        self._resume = asyncio.Event()
        self._resume.set()
        self._stop = asyncio.Event()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._worker_tasks: dict[str, set[asyncio.Task[Any]]] = {}
        self._location_tasks: dict[str, set[asyncio.Task[Any]]] = {}
        self._stopped_locations: set[str] = set()
        self._stopped_workers: set[str] = set()
        self._dispatched = 0
        self._completed = 0
        self._failed = 0
        self._skipped = 0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ queue
    async def submit(self, jobs: Iterable[Job]) -> int:
        """Enqueue *jobs*; returns the number queued."""
        count = 0
        for job in jobs:
            await self.queue.put(job)
            count += 1
        LOGGER.info("Queued %d job(s); %d pending", count, self.queue.pending)
        return count

    # ------------------------------------------------------------------ state
    def pause(self) -> None:
        """Stop dispatching new jobs; running sessions finish normally."""
        if self.state in {SchedulerState.IDLE, SchedulerState.RUNNING}:
            self.state = SchedulerState.PAUSED
        self._resume.clear()
        LOGGER.info("Scheduler paused")

    def resume(self) -> None:
        """Resume dispatching (safe to call whether or not a pause is pending)."""
        self._resume.set()
        if self.state is SchedulerState.PAUSED:
            self.state = SchedulerState.RUNNING
        LOGGER.info("Scheduler resumed")

    def stop(self) -> None:
        """Request a graceful stop (queue is drained, running jobs finish)."""
        self.state = SchedulerState.STOPPING
        self._stop.set()
        self._resume.set()
        LOGGER.info("Scheduler stop requested")

    async def stop_all(self, *, cancel_running: bool = True) -> dict[str, int]:
        """Emergency stop: drain the queue and cancel in-flight sessions."""
        self.stop()
        dropped = await self.queue.drain()
        cancelled = 0
        if cancel_running:
            for task in list(self._tasks):
                if not task.done():
                    task.cancel()
                    cancelled += 1
            if self._tasks:
                await asyncio.gather(*list(self._tasks), return_exceptions=True)
        self.state = SchedulerState.STOPPED
        LOGGER.warning("STOP ALL executed: %d queued job(s) dropped, %d running "
                       "session(s) cancelled", dropped, cancelled)
        return {"dropped": dropped, "cancelled": cancelled}

    async def stop_location(self, location_key: str) -> dict[str, int]:
        """Stop every queued and running session for one location."""
        self._stopped_locations.add(location_key)
        cancelled_jobs = self.queue.cancel_location(location_key)
        tasks = self._location_tasks.get(location_key, set())
        cancelled_tasks = 0
        for task in list(tasks):
            if not task.done():
                task.cancel()
                cancelled_tasks += 1
        if tasks:
            await asyncio.gather(*list(tasks), return_exceptions=True)
        LOGGER.warning("Stopped location %s (%d queued, %d running)",
                       location_key, cancelled_jobs, cancelled_tasks)
        return {"queued_cancelled": cancelled_jobs, "running_cancelled": cancelled_tasks}

    async def stop_worker(self, worker_id: str) -> dict[str, int]:
        """Stop every queued and running session for one worker."""
        self._stopped_workers.add(worker_id)
        cancelled_jobs = self.queue.cancel_worker(worker_id)
        tasks = self._worker_tasks.get(worker_id, set())
        cancelled_tasks = 0
        for task in list(tasks):
            if not task.done():
                task.cancel()
                cancelled_tasks += 1
        if tasks:
            await asyncio.gather(*list(tasks), return_exceptions=True)
        LOGGER.warning("Stopped worker %s (%d queued, %d running)",
                       worker_id, cancelled_jobs, cancelled_tasks)
        return {"queued_cancelled": cancelled_jobs, "running_cancelled": cancelled_tasks}

    def resume_location(self, location_key: str) -> None:
        """Re-enable a previously stopped location."""
        self._stopped_locations.discard(location_key)

    # --------------------------------------------------------------- dispatch
    async def run(self) -> dict[str, int]:
        """Run the dispatch loop until the queue drains or a stop is requested."""
        if self.state is not SchedulerState.PAUSED:
            self.state = SchedulerState.RUNNING
        self._stop.clear()
        total = self.queue.pending
        ramp_gap = (self.ramp_up_seconds / total) if total and self.ramp_up_seconds else 0.0
        LOGGER.info("Scheduler starting: %d job(s), ramp-up %.1fs", total,
                    self.ramp_up_seconds)

        while not self._stop.is_set():
            if self.queue.pending == 0:
                break
            await self._resume.wait()
            if self._stop.is_set():
                break
            try:
                job = await asyncio.wait_for(self.queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue

            if self._should_skip(job):
                job.mark(JobStatus.SKIPPED)
                self._skipped += 1
                self.queue.task_done()
                continue

            if self.rate_limiter is not None:
                allowed = await self.rate_limiter.wait_for_slot(timeout=60.0)
                if not allowed:
                    LOGGER.warning("Rate limit prevented dispatch of %s; requeued",
                                   job.job_id)
                    await self.queue.put(job)
                    self.queue.task_done()
                    await asyncio.sleep(1.0)
                    continue

            task = asyncio.create_task(self._execute(job), name=job.job_id)
            self._register(task, job)
            self._dispatched += 1
            self.queue.task_done()

            if ramp_gap:
                await asyncio.sleep(ramp_gap)
            elif self.jitter_seconds:
                await asyncio.sleep(random.uniform(0.0, self.jitter_seconds))

        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
        if self.state is not SchedulerState.STOPPED:
            self.state = SchedulerState.STOPPED
        LOGGER.info("Scheduler finished: dispatched=%d completed=%d failed=%d skipped=%d",
                    self._dispatched, self._completed, self._failed, self._skipped)
        return self.stats()

    def _should_skip(self, job: Job) -> bool:
        if job.worker_id in self._stopped_workers:
            return True
        if job.location_key in self._stopped_locations:
            return True
        if self.respect_schedules and self.location_manager is not None:
            try:
                if not self.location_manager.is_active(job.location_key):
                    LOGGER.info("Location %s outside its schedule; skipping %s",
                                job.location_key, job.job_id)
                    return True
            except KeyError:
                LOGGER.warning("Job %s references unknown location %s",
                               job.job_id, job.location_key)
                return True
        return False

    def _register(self, task: asyncio.Task[Any], job: Job) -> None:
        self._tasks.add(task)
        self._worker_tasks.setdefault(job.worker_id, set()).add(task)
        self._location_tasks.setdefault(job.location_key, set()).add(task)

        def _cleanup(finished: asyncio.Task[Any]) -> None:
            self._tasks.discard(finished)
            self._worker_tasks.get(job.worker_id, set()).discard(finished)
            self._location_tasks.get(job.location_key, set()).discard(finished)

        task.add_done_callback(_cleanup)

    async def _execute(self, job: Job) -> dict[str, Any]:
        job.mark(JobStatus.DISPATCHED)
        try:
            async with self.concurrency.slot(job.location_key, job.worker_id):
                job.mark(JobStatus.RUNNING)
                result = await self.runner(job)
            job.result = result or {}
            job.mark(JobStatus.COMPLETED)
            async with self._lock:
                self._completed += 1
            return job.result
        except asyncio.CancelledError:
            job.mark(JobStatus.CANCELLED, error="cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed silently
            job.mark(JobStatus.FAILED, error=str(exc))
            async with self._lock:
                self._failed += 1
            LOGGER.exception("Job %s failed: %s", job.job_id, exc)
            return {"error": str(exc)}

    # ------------------------------------------------------------------ stats
    def stats(self) -> dict[str, int]:
        """Dispatch counters."""
        return {
            "queued": self.queue.pending,
            "dispatched": self._dispatched,
            "completed": self._completed,
            "failed": self._failed,
            "skipped": self._skipped,
            "running": len([task for task in self._tasks if not task.done()]),
        }

    def snapshot(self) -> dict[str, Any]:
        """Full scheduler state for the live dashboard."""
        return {
            "state": self.state.value,
            "stats": self.stats(),
            "jobs": self.queue.counts(),
            "concurrency": self.concurrency.snapshot(),
            "rate_limit": self.rate_limiter.snapshot() if self.rate_limiter else {},
            "stopped_locations": sorted(self._stopped_locations),
            "stopped_workers": sorted(self._stopped_workers),
        }
