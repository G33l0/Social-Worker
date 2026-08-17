"""Core scheduler facade.

The dispatch implementation lives in :mod:`app.scheduler.scheduler`; this module
re-exports it under ``app.core`` so callers can import the scheduler from either
package without two copies of the logic existing.
"""

from __future__ import annotations

from app.scheduler.jobs import Job, JobPriority, JobQueue, JobStatus, build_job
from app.scheduler.scheduler import JobRunner, SchedulerState, SessionScheduler

__all__ = [
    "Job", "JobPriority", "JobQueue", "JobStatus", "build_job",
    "JobRunner", "SchedulerState", "SessionScheduler",
]
