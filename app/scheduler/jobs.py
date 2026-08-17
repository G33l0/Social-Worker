"""Job definitions and the priority queue the scheduler dispatches from."""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.utils.ids import new_session_id
from app.utils.time_utils import iso, utc_now


class JobStatus(str, Enum):
    """Lifecycle of a queued session job."""

    PENDING = "PENDING"
    DISPATCHED = "DISPATCHED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SKIPPED = "SKIPPED"


class JobPriority(int, Enum):
    """Lower value dispatches first."""

    HIGH = 0
    NORMAL = 5
    LOW = 9


@dataclass
class Job:
    """One scheduled visitor session."""

    job_id: str
    worker_id: str
    location_key: str
    scenario: str
    test_run_id: str = ""
    session_id: str = field(default_factory=new_session_id)
    forum: str = ""
    priority: JobPriority = JobPriority.NORMAL
    not_before: float = 0.0
    status: JobStatus = JobStatus.PENDING
    attempt: int = 0
    created_at: str = field(default_factory=iso)
    dispatched_at: str = ""
    finished_at: str = ""
    dry_run: bool = False
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def mark(self, status: JobStatus, *, error: str = "") -> "Job":
        """Transition the job to *status*."""
        self.status = status
        if status is JobStatus.DISPATCHED:
            self.dispatched_at = iso()
        if status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED,
                      JobStatus.SKIPPED}:
            self.finished_at = iso()
        if error:
            self.error = error
        return self

    def to_dict(self) -> dict[str, Any]:
        """Serialisable view used by the controller API and reports."""
        return {
            "job_id": self.job_id,
            "worker_id": self.worker_id,
            "location": self.location_key,
            "scenario": self.scenario,
            "session_id": self.session_id,
            "forum": self.forum,
            "priority": self.priority.name,
            "status": self.status.value,
            "attempt": self.attempt,
            "created_at": self.created_at,
            "dispatched_at": self.dispatched_at,
            "finished_at": self.finished_at,
            "dry_run": self.dry_run,
            "error": self.error,
        }


class JobQueue:
    """Async priority queue with cancellation support."""

    def __init__(self) -> None:
        self._queue: asyncio.PriorityQueue[tuple[int, int, Job]] = asyncio.PriorityQueue()
        self._counter = itertools.count()
        self._cancelled: set[str] = set()
        self._all: dict[str, Job] = {}

    def __len__(self) -> int:
        return self._queue.qsize()

    @property
    def pending(self) -> int:
        """Number of jobs still queued."""
        return self._queue.qsize()

    async def put(self, job: Job) -> None:
        """Enqueue *job*."""
        self._all[job.job_id] = job
        await self._queue.put((int(job.priority), next(self._counter), job))

    async def get(self) -> Job:
        """Pop the next job, skipping any that were cancelled."""
        while True:
            _, _, job = await self._queue.get()
            if job.job_id in self._cancelled:
                job.mark(JobStatus.CANCELLED)
                self._queue.task_done()
                continue
            return job

    def task_done(self) -> None:
        """Mark the last popped job as processed."""
        self._queue.task_done()

    def cancel(self, job_id: str) -> None:
        """Cancel a specific queued job."""
        self._cancelled.add(job_id)

    def cancel_worker(self, worker_id: str) -> int:
        """Cancel every queued job belonging to *worker_id*."""
        count = 0
        for job in self._all.values():
            if job.worker_id == worker_id and job.status is JobStatus.PENDING:
                self.cancel(job.job_id)
                count += 1
        return count

    def cancel_location(self, location_key: str) -> int:
        """Cancel every queued job for a location."""
        count = 0
        for job in self._all.values():
            if job.location_key == location_key and job.status is JobStatus.PENDING:
                self.cancel(job.job_id)
                count += 1
        return count

    def cancel_all(self) -> int:
        """Cancel every queued job."""
        count = 0
        for job in self._all.values():
            if job.status is JobStatus.PENDING:
                self.cancel(job.job_id)
                count += 1
        return count

    async def drain(self) -> int:
        """Discard every remaining queued job; returns how many were dropped."""
        dropped = 0
        while not self._queue.empty():
            try:
                _, _, job = self._queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - race guard
                break
            job.mark(JobStatus.CANCELLED)
            self._queue.task_done()
            dropped += 1
        return dropped

    async def join(self) -> None:
        """Wait until every enqueued job has been processed."""
        await self._queue.join()

    def snapshot(self) -> list[dict[str, Any]]:
        """All known jobs as dictionaries."""
        return [job.to_dict() for job in self._all.values()]

    def counts(self) -> dict[str, int]:
        """Job counts grouped by status."""
        counts: dict[str, int] = {status.value: 0 for status in JobStatus}
        for job in self._all.values():
            counts[job.status.value] += 1
        return counts


def build_job(worker_id: str, location_key: str, scenario: str, *,
              test_run_id: str = "", forum: str = "", dry_run: bool = False,
              priority: JobPriority = JobPriority.NORMAL,
              sequence: int | None = None) -> Job:
    """Create a job with a deterministic identifier."""
    stamp = utc_now().strftime("%H%M%S%f")[:-3]
    suffix = f"{sequence:04d}" if sequence is not None else stamp
    return Job(
        job_id=f"JOB-{worker_id}-{suffix}",
        worker_id=worker_id,
        location_key=location_key,
        scenario=scenario,
        test_run_id=test_run_id,
        forum=forum,
        dry_run=dry_run,
        priority=priority,
    )
