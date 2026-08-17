"""Scheduler, job queue and stop-control tests."""

from __future__ import annotations

import asyncio

from app.locations.allocation import ConcurrencyController
from app.locations.location import Location, TestSchedule, TestWindow
from app.locations.location_manager import LocationManager
from app.scheduler.jobs import JobPriority, JobQueue, JobStatus, build_job
from app.scheduler.scenarios import ScenarioLibrary, StepAction
from app.scheduler.scheduler import SchedulerState, SessionScheduler


def _jobs(count: int, location: str = "canada", worker_prefix: str = "SW"):
    return [build_job(f"{worker_prefix}-{index:05d}", location, "scenario_01",
                      test_run_id="TEST-RUN-0001", sequence=index)
            for index in range(1, count + 1)]


async def test_queue_orders_by_priority():
    queue = JobQueue()
    low = build_job("SW-00001", "canada", "scenario_01", sequence=1)
    low.priority = JobPriority.LOW
    high = build_job("SW-00002", "canada", "scenario_01", sequence=2)
    high.priority = JobPriority.HIGH
    await queue.put(low)
    await queue.put(high)
    assert (await queue.get()).worker_id == "SW-00002"
    assert (await queue.get()).worker_id == "SW-00001"


async def test_queue_skips_cancelled_jobs():
    queue = JobQueue()
    jobs = _jobs(2)
    for job in jobs:
        await queue.put(job)
    queue.cancel(jobs[0].job_id)
    remaining = await queue.get()
    assert remaining.job_id == jobs[1].job_id
    assert jobs[0].status is JobStatus.CANCELLED


async def test_scheduler_runs_every_job():
    seen: list[str] = []

    async def runner(job):
        seen.append(job.worker_id)
        await asyncio.sleep(0.005)
        return {"ok": True}

    scheduler = SessionScheduler(concurrency=ConcurrencyController(5), runner=runner)
    await scheduler.submit(_jobs(8))
    stats = await scheduler.run()
    assert stats["completed"] == 8
    assert len(seen) == 8
    assert scheduler.state is SchedulerState.STOPPED


async def test_scheduler_respects_concurrency_ceilings():
    peak = 0
    active = 0

    async def runner(job):
        nonlocal peak, active
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return {}

    controller = ConcurrencyController(3, {"canada": 2, "usa": 2})
    scheduler = SessionScheduler(concurrency=controller, runner=runner)
    await scheduler.submit(_jobs(6, "canada") + _jobs(6, "usa", worker_prefix="SU"))
    await scheduler.run()
    assert peak <= 3
    assert controller.snapshot()["locations"]["canada"]["peak"] <= 2


async def test_stop_all_cancels_queued_and_running_jobs():
    started = asyncio.Event()

    async def runner(job):
        started.set()
        await asyncio.sleep(5)
        return {}

    scheduler = SessionScheduler(concurrency=ConcurrencyController(2), runner=runner)
    await scheduler.submit(_jobs(20))
    task = asyncio.create_task(scheduler.run())
    await asyncio.wait_for(started.wait(), timeout=2)
    result = await scheduler.stop_all()
    await asyncio.wait_for(task, timeout=5)
    assert result["dropped"] > 0
    assert result["cancelled"] >= 1
    assert scheduler.state is SchedulerState.STOPPED


async def test_pause_and_resume_gate_dispatch():
    dispatched: list[str] = []

    async def runner(job):
        dispatched.append(job.worker_id)
        return {}

    scheduler = SessionScheduler(concurrency=ConcurrencyController(2), runner=runner)
    await scheduler.submit(_jobs(6))
    scheduler.state = SchedulerState.RUNNING
    scheduler.pause()
    task = asyncio.create_task(scheduler.run())
    scheduler.pause()
    await asyncio.sleep(0.05)
    paused_count = len(dispatched)
    scheduler.resume()
    await asyncio.wait_for(task, timeout=5)
    assert paused_count <= 1
    assert len(dispatched) == 6


async def test_stop_location_only_affects_that_location():
    handled: list[str] = []

    async def runner(job):
        handled.append(job.location_key)
        await asyncio.sleep(0.01)
        return {}

    scheduler = SessionScheduler(concurrency=ConcurrencyController(2), runner=runner)
    await scheduler.stop_location("canada")
    await scheduler.submit(_jobs(4, "canada") + _jobs(4, "usa", worker_prefix="SU"))
    stats = await scheduler.run()
    assert "canada" not in handled
    assert handled.count("usa") == 4
    assert stats["skipped"] == 4


async def test_stop_worker_skips_that_worker():
    handled: list[str] = []

    async def runner(job):
        handled.append(job.worker_id)
        return {}

    scheduler = SessionScheduler(concurrency=ConcurrencyController(2), runner=runner)
    await scheduler.stop_worker("SW-00002")
    await scheduler.submit(_jobs(3))
    await scheduler.run()
    assert "SW-00002" not in handled
    assert len(handled) == 2


async def test_scheduler_skips_locations_outside_their_schedule():
    handled: list[str] = []

    async def runner(job):
        handled.append(job.location_key)
        return {}

    closed = TestSchedule(name="closed", days=["mon"],
                          windows=[TestWindow(start="00:00", end="00:01")],
                          enabled=False)
    manager = LocationManager([Location(key="canada", workers=1, schedule="closed")],
                              [closed])
    scheduler = SessionScheduler(concurrency=ConcurrencyController(2), runner=runner,
                                 location_manager=manager)
    await scheduler.submit(_jobs(3))
    stats = await scheduler.run()
    assert handled == []
    assert stats["skipped"] == 3


async def test_failed_jobs_are_counted_not_swallowed():
    async def runner(job):
        raise RuntimeError("boom")

    scheduler = SessionScheduler(concurrency=ConcurrencyController(2), runner=runner)
    await scheduler.submit(_jobs(3))
    stats = await scheduler.run()
    assert stats["failed"] == 3
    assert stats["completed"] == 0


def test_builtin_scenarios_cover_the_spec():
    library = ScenarioLibrary()
    assert library.names() == [f"scenario_0{index}" for index in range(1, 8)]
    assert not library.get("scenario_01").uses(StepAction.CREATE_POST)
    assert library.get("scenario_04").uses(StepAction.CREATE_POST)
    assert library.get("scenario_05").uses(StepAction.CREATE_REPLY)
    full = library.get("scenario_06")
    assert full.uses(StepAction.CREATE_POST) and full.uses(StepAction.CREATE_REPLY)
    assert library.get("scenario_02").allow_posting is False


def test_custom_scenarios_round_trip(tmp_path):
    from app.scheduler.scenarios import Scenario, Step

    library = ScenarioLibrary()
    library.add(Scenario(name="scenario_custom", description="visit and browse",
                         steps=[Step(action=StepAction.OPEN_SITE),
                                Step(action=StepAction.BROWSE_FORUM, repeat=3)]))
    path = library.save(tmp_path / "scenarios.yaml")
    reloaded = ScenarioLibrary.load(path)
    assert "scenario_custom" in reloaded
    assert reloaded.get("scenario_custom").steps[1].repeat == 3
