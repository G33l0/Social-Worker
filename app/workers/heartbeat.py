"""Worker heartbeat tracking.

In a distributed deployment each worker agent reports in on an interval; the
controller marks workers whose heartbeat has lapsed as stale so an operator can
see a dead runner immediately instead of at the end of the run.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable

from app.utils.logger import get_logger
from app.utils.time_utils import utc_now

LOGGER = get_logger("workers.heartbeat")


@dataclass
class Heartbeat:
    """Last-seen record for one worker."""

    worker_id: str
    location_key: str = ""
    last_seen: Any = field(default_factory=utc_now)
    status: str = "UNKNOWN"
    action: str = ""
    runner: str = "local"

    def age_seconds(self, now: Any = None) -> float:
        """Seconds since the last heartbeat."""
        return ((now or utc_now()) - self.last_seen).total_seconds()

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "location": self.location_key,
            "status": self.status,
            "action": self.action,
            "runner": self.runner,
            "last_seen": self.last_seen.isoformat(),
            "age_seconds": round(self.age_seconds(), 1),
        }


class HeartbeatMonitor:
    """Tracks worker liveness and reports stale workers."""

    def __init__(self, *, timeout_seconds: float = 45.0,
                 interval_seconds: float = 10.0,
                 on_stale: Callable[[list[Heartbeat]], None] | None = None) -> None:
        self.timeout_seconds = timeout_seconds
        self.interval_seconds = interval_seconds
        self.on_stale = on_stale
        self._beats: dict[str, Heartbeat] = {}
        self._task: asyncio.Task[None] | None = None
        self._running = False

    def register(self, worker_id: str, *, location_key: str = "",
                 runner: str = "local") -> Heartbeat:
        """Register a worker with the monitor."""
        beat = Heartbeat(worker_id=worker_id, location_key=location_key, runner=runner)
        self._beats[worker_id] = beat
        return beat

    def beat(self, worker_id: str, *, status: str = "", action: str = "") -> None:
        """Record a heartbeat for *worker_id*."""
        beat = self._beats.get(worker_id)
        if beat is None:
            beat = self.register(worker_id)
        beat.last_seen = utc_now()
        if status:
            beat.status = status
        if action:
            beat.action = action

    def unregister(self, worker_id: str) -> None:
        """Remove a worker from the monitor."""
        self._beats.pop(worker_id, None)

    #: Statuses that mean the worker is finished, not missing.
    TERMINAL_STATUSES = frozenset({"COMPLETED", "STOPPED", "ERROR", "CREATED", "READY"})

    def stale(self) -> list[Heartbeat]:
        """Workers that are supposed to be working but have stopped reporting.

        A worker that has finished its plan is not stale, so a completed run
        does not fill the log with false "missing worker" warnings.
        """
        now = utc_now()
        return [beat for beat in self._beats.values()
                if beat.status not in self.TERMINAL_STATUSES
                and beat.age_seconds(now) > self.timeout_seconds]

    def snapshot(self) -> list[dict[str, Any]]:
        """All heartbeats as dictionaries."""
        return [beat.to_dict() for beat in sorted(self._beats.values(),
                                                  key=lambda item: item.worker_id)]

    async def start(self) -> None:
        """Start the periodic staleness check."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="heartbeat-monitor")

    async def stop(self) -> None:
        """Stop the periodic check."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        try:
            while self._running:
                await asyncio.sleep(self.interval_seconds)
                stale = self.stale()
                if stale:
                    LOGGER.warning("%d worker(s) have stale heartbeats: %s",
                                   len(stale), ", ".join(beat.worker_id for beat in stale))
                    if self.on_stale is not None:
                        self.on_stale(stale)
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise
