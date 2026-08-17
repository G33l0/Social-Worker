"""Telemetry collection, buffering and persistence.

Workers call the synchronous ``record_*`` methods from the event loop; records
are buffered in memory and flushed to the database on a background task so disk
IO never stalls a running session.  The manager also maintains the live
aggregates the dashboard renders.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from app.database.database import Database
from app.database.models import ErrorRecord, Event, Execution, Metric
from app.utils.ids import new_event_id
from app.utils.logger import get_logger
from app.utils.time_utils import utc_now

LOGGER = get_logger("metrics")


@dataclass
class LocationStats:
    """Live counters for one geographic test location."""

    active_workers: int = 0
    completed_sessions: int = 0
    posts: int = 0
    replies: int = 0
    errors: int = 0
    requests: int = 0
    response_ms_total: float = 0.0
    response_samples: int = 0
    session_ms_total: float = 0.0

    @property
    def avg_response_ms(self) -> float:
        """Mean response time observed from this location."""
        return round(self.response_ms_total / self.response_samples, 2) \
            if self.response_samples else 0.0

    @property
    def avg_session_ms(self) -> float:
        """Mean session duration for this location."""
        return round(self.session_ms_total / self.completed_sessions, 2) \
            if self.completed_sessions else 0.0

    def to_row(self, name: str) -> dict[str, Any]:
        """Row for the live monitor / geographic report."""
        return {
            "location": name,
            "active": self.active_workers,
            "sessions": self.completed_sessions,
            "posts": self.posts,
            "replies": self.replies,
            "errors": self.errors,
            "requests": self.requests,
            "avg_response_ms": self.avg_response_ms,
            "avg_session_ms": self.avg_session_ms,
        }


@dataclass
class GlobalStats:
    """Live counters for the whole test run."""

    workers_registered: int = 0
    active_workers: int = 0
    completed_sessions: int = 0
    failed_sessions: int = 0
    posts: int = 0
    replies: int = 0
    errors: int = 0
    rate_limit_events: int = 0
    successful_actions: int = 0
    failed_actions: int = 0
    requests: int = 0
    response_ms_total: float = 0.0
    response_samples: int = 0
    pages_visited: int = 0
    started_at: str = field(default_factory=lambda: utc_now().isoformat())

    @property
    def avg_response_ms(self) -> float:
        """Mean response time across every location."""
        return round(self.response_ms_total / self.response_samples, 2) \
            if self.response_samples else 0.0

    @property
    def error_rate(self) -> float:
        """Failed actions as a fraction of all actions."""
        total = self.successful_actions + self.failed_actions
        return round(self.failed_actions / total, 4) if total else 0.0


class MetricsManager:
    """Buffered writer for events, metrics, executions and errors."""

    def __init__(self, database: Database, test_run_id: str, *,
                 flush_interval: float = 2.0, buffer_limit: int = 250) -> None:
        self.database = database
        self.test_run_id = test_run_id
        self.flush_interval = flush_interval
        self.buffer_limit = buffer_limit

        self.global_stats = GlobalStats()
        self.location_stats: dict[str, LocationStats] = defaultdict(LocationStats)

        self._events: list[dict[str, Any]] = []
        self._metrics: list[dict[str, Any]] = []
        self._executions: list[dict[str, Any]] = []
        self._errors: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()
        self._flusher: asyncio.Task[None] | None = None
        self._running = False

    # -------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        """Start the background flusher."""
        if self._running:
            return
        self._running = True
        self._flusher = asyncio.create_task(self._flush_loop(), name="metrics-flusher")
        LOGGER.info("Metrics manager started for %s", self.test_run_id)

    async def stop(self) -> None:
        """Flush everything and stop the background task."""
        self._running = False
        if self._flusher is not None:
            self._flusher.cancel()
            try:
                await self._flusher
            except asyncio.CancelledError:
                pass
            self._flusher = None
        await self.flush()
        LOGGER.info("Metrics manager stopped for %s", self.test_run_id)

    async def _flush_loop(self) -> None:
        try:
            while self._running:
                await asyncio.sleep(self.flush_interval)
                await self.flush()
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise

    # ---------------------------------------------------------------- recording
    def record_event(self, event_type: str, *, worker_id: str = "",
                     session_id: str = "", location: str = "", action: str = "",
                     result: str = "", duration_ms: float = 0.0,
                     payload: dict[str, Any] | None = None) -> None:
        """Record a telemetry event (one per session step)."""
        self._events.append({
            "event_id": new_event_id(),
            "test_run_id": self.test_run_id,
            "worker_id": worker_id,
            "session_id": session_id,
            "location_key": location,
            "event_type": event_type,
            "action": action,
            "result": result,
            "duration_ms": float(duration_ms),
            "payload": payload or {},
            "created_at": utc_now(),
        })
        self._maybe_flush()

    def record_metric(self, name: str, value: float, *, unit: str = "ms",
                      worker_id: str = "", session_id: str = "", location: str = "",
                      labels: dict[str, Any] | None = None) -> None:
        """Record a numeric measurement."""
        self._metrics.append({
            "test_run_id": self.test_run_id,
            "worker_id": worker_id,
            "session_id": session_id,
            "location_key": location,
            "name": name,
            "value": float(value),
            "unit": unit,
            "labels": labels or {},
            "created_at": utc_now(),
        })
        if unit == "ms" and name.endswith("_ms"):
            self.global_stats.response_ms_total += float(value)
            self.global_stats.response_samples += 1
            if location:
                stats = self.location_stats[location]
                stats.response_ms_total += float(value)
                stats.response_samples += 1
        self._maybe_flush()

    def record_execution(self, action: str, *, worker_id: str = "", session_id: str = "",
                         location: str = "", forum: str = "", target_id: str = "",
                         content_ref: str = "", status: str = "OK",
                         http_status: int | None = None, duration_ms: float = 0.0,
                         url: str = "", detail: str = "", dry_run: bool = False) -> None:
        """Record a single site interaction."""
        self._executions.append({
            "test_run_id": self.test_run_id,
            "session_id": session_id,
            "worker_id": worker_id,
            "location_key": location,
            "action": action,
            "forum_key": forum,
            "target_id": target_id,
            "content_ref": content_ref,
            "status": status,
            "http_status": http_status,
            "duration_ms": float(duration_ms),
            "url": url[:500],
            "detail": detail[:2000],
            "dry_run": dry_run,
            "created_at": utc_now(),
        })

        self.global_stats.requests += 1
        if location:
            self.location_stats[location].requests += 1
        if status == "OK":
            self.global_stats.successful_actions += 1
        else:
            self.global_stats.failed_actions += 1
        if action == "CREATE_POST" and status == "OK":
            self.global_stats.posts += 1
            if location:
                self.location_stats[location].posts += 1
        if action == "CREATE_REPLY" and status == "OK":
            self.global_stats.replies += 1
            if location:
                self.location_stats[location].replies += 1
        if duration_ms:
            self.record_metric(f"{action.lower()}_ms", duration_ms, worker_id=worker_id,
                               session_id=session_id, location=location)
        self._maybe_flush()

    def record_error(self, error_type: str, message: str, *, worker_id: str = "",
                     session_id: str = "", location: str = "", action: str = "",
                     url: str = "", selector: str = "", http_status: int | None = None,
                     screenshot_path: str = "", html_path: str = "",
                     traceback_text: str = "") -> None:
        """Record a failed action together with its debug artefacts."""
        self._errors.append({
            "test_run_id": self.test_run_id,
            "session_id": session_id,
            "worker_id": worker_id,
            "location_key": location,
            "error_type": error_type,
            "action": action,
            "message": message[:2000],
            "url": url[:500],
            "selector": selector[:255],
            "http_status": http_status,
            "screenshot_path": screenshot_path[:500],
            "html_path": html_path[:500],
            "traceback": traceback_text[:8000],
            "created_at": utc_now(),
        })
        self.global_stats.errors += 1
        if location:
            self.location_stats[location].errors += 1
        if error_type == "RateLimited":
            self.global_stats.rate_limit_events += 1
        self._maybe_flush()

    # ------------------------------------------------------------ session hooks
    def session_started(self, location: str) -> None:
        """Mark a session as started (updates the active-worker gauge)."""
        self.global_stats.active_workers += 1
        self.location_stats[location].active_workers += 1

    def session_finished(self, location: str, *, duration_ms: float,
                         success: bool, pages_visited: int = 0) -> None:
        """Mark a session as finished."""
        self.global_stats.active_workers = max(0, self.global_stats.active_workers - 1)
        stats = self.location_stats[location]
        stats.active_workers = max(0, stats.active_workers - 1)
        stats.session_ms_total += duration_ms
        self.global_stats.pages_visited += pages_visited
        if success:
            self.global_stats.completed_sessions += 1
            stats.completed_sessions += 1
        else:
            self.global_stats.failed_sessions += 1
            stats.completed_sessions += 1

    # ------------------------------------------------------------------ flush
    def _maybe_flush(self) -> None:
        pending = (len(self._events) + len(self._metrics) + len(self._executions)
                   + len(self._errors))
        if pending >= self.buffer_limit and self._running:
            asyncio.create_task(self.flush())

    async def flush(self) -> int:
        """Write buffered telemetry to the database; returns rows written."""
        async with self._lock:
            events, self._events = self._events, []
            metrics, self._metrics = self._metrics, []
            executions, self._executions = self._executions, []
            errors, self._errors = self._errors, []

        total = len(events) + len(metrics) + len(executions) + len(errors)
        if total == 0:
            return 0

        def _write(session: Any) -> None:
            if events:
                session.bulk_insert_mappings(Event, events)
            if metrics:
                session.bulk_insert_mappings(Metric, metrics)
            if executions:
                session.bulk_insert_mappings(Execution, executions)
            if errors:
                session.bulk_insert_mappings(ErrorRecord, errors)

        try:
            await self.database.run(_write)
        except Exception as exc:  # pragma: no cover - database failure path
            LOGGER.error("Failed to flush %d telemetry row(s): %s", total, exc)
            return 0
        LOGGER.debug("Flushed %d telemetry row(s)", total)
        return total

    # ----------------------------------------------------------------- reading
    def snapshot(self) -> dict[str, Any]:
        """Live aggregates for the dashboard."""
        return {
            "test_run_id": self.test_run_id,
            "global": {
                "workers_registered": self.global_stats.workers_registered,
                "active": self.global_stats.active_workers,
                "completed": self.global_stats.completed_sessions,
                "failed": self.global_stats.failed_sessions,
                "posts": self.global_stats.posts,
                "replies": self.global_stats.replies,
                "errors": self.global_stats.errors,
                "rate_limit_events": self.global_stats.rate_limit_events,
                "requests": self.global_stats.requests,
                "avg_response_ms": self.global_stats.avg_response_ms,
                "error_rate": self.global_stats.error_rate,
                "pages_visited": self.global_stats.pages_visited,
                "started_at": self.global_stats.started_at,
            },
            "locations": [stats.to_row(name)
                          for name, stats in sorted(self.location_stats.items())],
            "buffered": len(self._events) + len(self._metrics)
            + len(self._executions) + len(self._errors),
        }
