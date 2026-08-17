"""Location, allocation and concurrency-control tests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from app.locations.allocation import (
    ConcurrencyController, RateLimiter, allocate_workers,
)
from app.locations.location import (
    DEFAULT_LOCATIONS, Location, RunnerType, TestSchedule, TestWindow,
)
from app.locations.location_manager import LocationManager


def _locations(**counts: int) -> list[Location]:
    return [Location(key=key, workers=value, max_concurrent_workers=value)
            for key, value in counts.items()]


def test_allocation_fits_under_the_global_limit():
    allocation = allocate_workers(_locations(usa=20, canada=10, uk=10), 100)
    assert allocation.per_location == {"usa": 20, "canada": 10, "uk": 10}
    assert allocation.total == 40
    assert allocation.unallocated == 60


def test_allocation_scales_down_proportionally():
    allocation = allocate_workers(_locations(usa=20, canada=10, uk=10, nigeria=10), 25)
    assert allocation.total == 25
    assert allocation["usa"] > allocation["canada"]
    assert all(count >= 1 for count in allocation.per_location.values())


def test_allocation_never_exceeds_the_location_ceiling():
    location = Location(key="canada", workers=50, max_concurrent_workers=5)
    allocation = allocate_workers([location], 100)
    assert allocation["canada"] == 5


def test_disabled_locations_get_no_workers():
    enabled = Location(key="usa", workers=5, max_concurrent_workers=5)
    disabled = Location(key="japan", workers=5, max_concurrent_workers=5, enabled=False)
    allocation = allocate_workers([enabled, disabled], 10)
    assert "japan" not in allocation.per_location


def test_location_manager_defaults_and_validation():
    manager = LocationManager([Location.model_validate(item)
                               for item in DEFAULT_LOCATIONS])
    assert "united_states" in manager.keys()
    warnings = manager.validate(global_limit=10)
    assert any("scaled down" in warning for warning in warnings)


def test_location_manager_round_trip(tmp_path):
    manager = LocationManager(_locations(usa=3, canada=2))
    path = manager.save(tmp_path / "locations.yaml")
    reloaded = LocationManager.load(path, tmp_path / "schedules.yaml")
    assert reloaded.keys() == ["canada", "usa"]
    assert reloaded.get("usa").workers == 3


def test_proxy_runner_requires_a_proxy_url():
    with pytest.raises(ValueError):
        Location(key="uk", runner=RunnerType.PROXY)


def test_remote_runner_requires_an_endpoint():
    with pytest.raises(ValueError):
        Location(key="uk", runner=RunnerType.REMOTE_AGENT)


def test_schedule_windows_and_wraparound():
    day = TestSchedule(name="business", days=["mon", "tue", "wed", "thu", "fri"],
                       windows=[TestWindow(start="09:00", end="17:00")])
    monday_noon = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)  # a Monday
    sunday_noon = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
    assert day.is_active(monday_noon) is True
    assert day.is_active(sunday_noon) is False

    overnight = TestSchedule(name="offpeak",
                             windows=[TestWindow(start="22:00", end="04:00")])
    assert overnight.is_active(datetime(2026, 8, 17, 23, 30, tzinfo=timezone.utc)) is True
    assert overnight.is_active(datetime(2026, 8, 17, 2, 30, tzinfo=timezone.utc)) is True
    assert overnight.is_active(datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)) is False


def test_location_manager_reports_schedule_activity():
    manager = LocationManager(
        [Location(key="usa", workers=1, schedule="offpeak")],
        [TestSchedule(name="offpeak", windows=[TestWindow(start="22:00", end="04:00")])])
    assert manager.schedule_for("usa") is not None
    assert isinstance(manager.is_active("usa"), bool)
    manager.set_enabled("usa", False)
    assert manager.is_active("usa") is False


async def test_concurrency_controller_enforces_the_global_limit():
    controller = ConcurrencyController(3, {"usa": 10, "canada": 10})
    peak = 0
    active = 0
    lock = asyncio.Lock()

    async def session(index: int) -> None:
        nonlocal peak, active
        async with controller.slot("usa" if index % 2 else "canada", f"SW-{index:05d}"):
            async with lock:
                active += 1
                peak = max(peak, active)
            await asyncio.sleep(0.02)
            async with lock:
                active -= 1

    await asyncio.gather(*(session(index) for index in range(12)))
    assert peak <= 3
    assert controller.snapshot()["global_peak"] <= 3
    assert controller.active == 0


async def test_concurrency_controller_enforces_the_location_limit():
    controller = ConcurrencyController(20, {"canada": 2, "usa": 5})
    peak = {"canada": 0, "usa": 0}
    active = {"canada": 0, "usa": 0}

    async def session(location: str, index: int) -> None:
        async with controller.slot(location, f"SW-{location}-{index}"):
            active[location] += 1
            peak[location] = max(peak[location], active[location])
            await asyncio.sleep(0.02)
            active[location] -= 1

    await asyncio.gather(
        *(session("canada", index) for index in range(8)),
        *(session("usa", index) for index in range(8)),
    )
    assert peak["canada"] <= 2
    assert peak["usa"] <= 5


async def test_per_worker_concurrency_limit():
    controller = ConcurrencyController(10, {"usa": 10}, per_worker_limit=1)
    concurrent = 0
    peak = 0

    async def session() -> None:
        nonlocal concurrent, peak
        async with controller.slot("usa", "SW-00001"):
            concurrent += 1
            peak = max(peak, concurrent)
            await asyncio.sleep(0.01)
            concurrent -= 1

    await asyncio.gather(*(session() for _ in range(4)))
    assert peak == 1


async def test_rate_limiter_blocks_beyond_the_window():
    limiter = RateLimiter(per_minute=2)
    assert limiter.try_acquire() is True
    assert limiter.try_acquire() is True
    assert limiter.try_acquire() is False
    snapshot = limiter.snapshot()
    assert snapshot["last_minute"] == 2
    assert await limiter.wait_for_slot(timeout=0.2) is False
