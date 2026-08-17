"""Worker allocation across locations plus the concurrency controllers.

Two independent guards protect the target site:

``ConcurrencyController``   caps simultaneous workers globally and per location
``RateLimiter``             caps request/session throughput per minute/hour/day

The scheduler must acquire a slot from both before a session may start, so the
configured ceilings can never be exceeded, even transiently.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from app.locations.location import Location
from app.utils.logger import get_logger

LOGGER = get_logger("locations.allocation")


@dataclass
class Allocation:
    """Result of distributing a worker budget across locations."""

    per_location: dict[str, int] = field(default_factory=dict)
    total: int = 0
    unallocated: int = 0

    def __getitem__(self, key: str) -> int:
        return self.per_location.get(key, 0)

    def items(self) -> Iterable[tuple[str, int]]:
        return self.per_location.items()


def allocate_workers(locations: Iterable[Location], global_limit: int) -> Allocation:
    """Distribute *global_limit* worker slots across enabled *locations*.

    Each location's configured ``workers`` value is treated as its request.  If
    the sum of requests exceeds the global limit, requests are scaled down
    proportionally (largest-remainder method) so the global cap always holds.
    Every location's own ``max_concurrent_workers`` ceiling is honoured.
    """
    enabled = [location for location in locations if location.enabled and location.workers > 0]
    if not enabled or global_limit <= 0:
        return Allocation(per_location={}, total=0, unallocated=max(0, global_limit))

    requested = {loc.key: min(loc.workers, loc.max_concurrent_workers) for loc in enabled}
    total_requested = sum(requested.values())

    if total_requested <= global_limit:
        return Allocation(per_location=dict(requested), total=total_requested,
                          unallocated=global_limit - total_requested)

    scale = global_limit / total_requested
    base = {key: int(value * scale) for key, value in requested.items()}
    remainder = global_limit - sum(base.values())
    # Largest-remainder distribution of the leftover slots.
    fractional = sorted(
        requested.items(),
        key=lambda item: (item[1] * scale) - int(item[1] * scale),
        reverse=True,
    )
    index = 0
    while remainder > 0 and fractional:
        key = fractional[index % len(fractional)][0]
        if base[key] < requested[key]:
            base[key] += 1
            remainder -= 1
        index += 1
        if index > len(fractional) * 4:  # pragma: no cover - defensive
            break

    allocated = {key: value for key, value in base.items() if value > 0}
    total = sum(allocated.values())
    LOGGER.info("Allocated %d/%d worker slots across %d location(s)",
                total, global_limit, len(allocated))
    return Allocation(per_location=allocated, total=total,
                      unallocated=max(0, global_limit - total))


class ConcurrencyController:
    """Enforces the global and per-location simultaneous-worker ceilings."""

    def __init__(self, global_limit: int, location_limits: Mapping[str, int] | None = None,
                 *, per_worker_limit: int = 1) -> None:
        if global_limit < 1:
            raise ValueError("global_limit must be >= 1")
        self.global_limit = global_limit
        self.per_worker_limit = max(1, per_worker_limit)
        self._location_limits: dict[str, int] = dict(location_limits or {})
        self._global = asyncio.Semaphore(global_limit)
        self._locations: dict[str, asyncio.Semaphore] = {
            key: asyncio.Semaphore(max(1, value))
            for key, value in self._location_limits.items()
        }
        self._workers: dict[str, asyncio.Semaphore] = {}
        self._active_global = 0
        self._active_by_location: dict[str, int] = defaultdict(int)
        self._peak_global = 0
        self._peak_by_location: dict[str, int] = defaultdict(int)
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ config
    def set_location_limit(self, key: str, limit: int) -> None:
        """Register (or raise) the ceiling for a location."""
        limit = max(1, limit)
        self._location_limits[key] = limit
        if key not in self._locations:
            self._locations[key] = asyncio.Semaphore(limit)

    def location_limit(self, key: str) -> int:
        """Effective ceiling for *key* (falls back to the global limit)."""
        return self._location_limits.get(key, self.global_limit)

    def _location_semaphore(self, key: str) -> asyncio.Semaphore:
        semaphore = self._locations.get(key)
        if semaphore is None:
            semaphore = asyncio.Semaphore(self.location_limit(key))
            self._locations[key] = semaphore
        return semaphore

    def _worker_semaphore(self, worker_id: str) -> asyncio.Semaphore:
        semaphore = self._workers.get(worker_id)
        if semaphore is None:
            semaphore = asyncio.Semaphore(self.per_worker_limit)
            self._workers[worker_id] = semaphore
        return semaphore

    # ------------------------------------------------------------------- slots
    async def acquire(self, location_key: str, worker_id: str) -> None:
        """Block until a global, location and worker slot are all available."""
        worker_semaphore = self._worker_semaphore(worker_id)
        location_semaphore = self._location_semaphore(location_key)
        await worker_semaphore.acquire()
        try:
            await location_semaphore.acquire()
        except BaseException:
            worker_semaphore.release()
            raise
        try:
            await self._global.acquire()
        except BaseException:
            location_semaphore.release()
            worker_semaphore.release()
            raise
        async with self._lock:
            self._active_global += 1
            self._active_by_location[location_key] += 1
            self._peak_global = max(self._peak_global, self._active_global)
            self._peak_by_location[location_key] = max(
                self._peak_by_location[location_key],
                self._active_by_location[location_key])

    async def release(self, location_key: str, worker_id: str) -> None:
        """Return a previously acquired slot."""
        async with self._lock:
            self._active_global = max(0, self._active_global - 1)
            self._active_by_location[location_key] = max(
                0, self._active_by_location[location_key] - 1)
        self._global.release()
        self._location_semaphore(location_key).release()
        self._worker_semaphore(worker_id).release()

    class _Slot:
        def __init__(self, controller: "ConcurrencyController", location_key: str,
                     worker_id: str) -> None:
            self._controller = controller
            self._location_key = location_key
            self._worker_id = worker_id

        async def __aenter__(self) -> "ConcurrencyController._Slot":
            await self._controller.acquire(self._location_key, self._worker_id)
            return self

        async def __aexit__(self, *_exc: object) -> None:
            await self._controller.release(self._location_key, self._worker_id)

    def slot(self, location_key: str, worker_id: str) -> "ConcurrencyController._Slot":
        """``async with controller.slot(location, worker):`` helper."""
        return ConcurrencyController._Slot(self, location_key, worker_id)

    # ------------------------------------------------------------------- stats
    @property
    def active(self) -> int:
        """Currently running workers, globally."""
        return self._active_global

    def active_in(self, location_key: str) -> int:
        """Currently running workers in *location_key*."""
        return self._active_by_location.get(location_key, 0)

    def snapshot(self) -> dict[str, object]:
        """Live utilisation snapshot for the dashboard."""
        return {
            "global_limit": self.global_limit,
            "global_active": self._active_global,
            "global_peak": self._peak_global,
            "locations": {
                key: {
                    "limit": self.location_limit(key),
                    "active": self._active_by_location.get(key, 0),
                    "peak": self._peak_by_location.get(key, 0),
                }
                for key in sorted(set(self._location_limits) | set(self._active_by_location))
            },
        }


class RateLimiter:
    """Sliding-window limiter for requests/sessions per minute, hour and day."""

    def __init__(self, *, per_minute: int = 0, per_hour: int = 0, per_day: int = 0) -> None:
        self.per_minute = per_minute
        self.per_hour = per_hour
        self.per_day = per_day
        self._events: deque[float] = deque()
        self._lock = asyncio.Lock()

    def _prune(self, now: float) -> None:
        horizon = now - 86_400
        while self._events and self._events[0] < horizon:
            self._events.popleft()

    def _count_since(self, now: float, window: float) -> int:
        threshold = now - window
        return sum(1 for moment in reversed(self._events) if moment >= threshold)

    async def wait_for_slot(self, *, timeout: float | None = None) -> bool:
        """Block until a slot is free.  Returns False if *timeout* elapses."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            async with self._lock:
                now = time.monotonic()
                self._prune(now)
                delay = self._delay_until_free(now)
                if delay <= 0:
                    self._events.append(now)
                    return True
            if deadline is not None and time.monotonic() + delay > deadline:
                return False
            await asyncio.sleep(min(delay, 1.0))

    def _delay_until_free(self, now: float) -> float:
        checks = (
            (self.per_minute, 60.0),
            (self.per_hour, 3_600.0),
            (self.per_day, 86_400.0),
        )
        delay = 0.0
        for limit, window in checks:
            if limit <= 0:
                continue
            used = self._count_since(now, window)
            if used >= limit:
                oldest_in_window = next(
                    (moment for moment in self._events if moment >= now - window), now)
                delay = max(delay, (oldest_in_window + window) - now)
        return delay

    def try_acquire(self) -> bool:
        """Non-blocking acquisition; True when a slot was taken."""
        now = time.monotonic()
        self._prune(now)
        if self._delay_until_free(now) > 0:
            return False
        self._events.append(now)
        return True

    def snapshot(self) -> dict[str, int]:
        """Current usage counters."""
        now = time.monotonic()
        self._prune(now)
        return {
            "last_minute": self._count_since(now, 60.0),
            "last_hour": self._count_since(now, 3_600.0),
            "last_day": self._count_since(now, 86_400.0),
            "limit_per_minute": self.per_minute,
            "limit_per_hour": self.per_hour,
            "limit_per_day": self.per_day,
        }
