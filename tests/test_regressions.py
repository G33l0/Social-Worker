"""Regression tests for defects found in the post-build audit.

Each test names the bug it pins down, so a future change that reintroduces the
behaviour fails here rather than in production.
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta

import pytest

from tests.conftest import FakeSiteAdapter
from app.core.forum_manager import ForumSelector
from app.core.metrics_manager import MetricsManager
from app.core.thresholds import evaluate
from app.database.repository import Repository
from app.locations.location import Location
from app.locations.location_manager import LocationManager
from app.scheduler.scenarios import ScenarioLibrary
from app.site.base_adapter import ForumRef
from app.utils.config import ForumConfig, ThresholdConfig
from app.workers.heartbeat import HeartbeatMonitor
from app.workers.worker import Worker
from app.workers.worker_state import WorkerProfile


# --------------------------------------------------------------- forum weights
def test_all_zero_forum_weights_do_not_crash_the_run():
    """Was: validate_weights raised, killing the step that selected a forum."""
    selector = ForumSelector(ForumConfig(weights={"general": 0.0, "random": 0.0}))
    forums = [ForumRef(key="general", index=0), ForumRef(key="random", index=1)]
    selector.register(forums)
    assert selector.select("SW-00001", forums=forums) is None


def test_unweighted_forums_fall_back_to_an_even_split():
    selector = ForumSelector(ForumConfig(weights={"other": 10.0}))
    forums = [ForumRef(key="general", index=0), ForumRef(key="random", index=1)]
    selector.register(forums)
    assert selector.select("SW-00001", forums=forums) is not None


# ------------------------------------------------------ per-location ceilings
def test_default_ceiling_applies_only_to_implicit_locations():
    """Was: concurrency.default_max_workers_per_location was never read."""
    manager = LocationManager([
        Location(key="implicit", workers=3),
        Location(key="explicit", workers=3, max_concurrent_workers=7),
    ])
    adjusted = manager.apply_default_ceiling(25)
    assert adjusted == 1
    assert manager.get("implicit").max_concurrent_workers == 25
    assert manager.get("explicit").max_concurrent_workers == 7


# ------------------------------------------------------------- worker pacing
def _worker(settings, location, content_manager, forum_selector, metrics,
            session_manager, adapter, scenario="scenario_06", **profile_overrides):
    data = {"worker_id": "SW-00001", "location_key": location.key,
            "scenario": scenario, "post_probability": 1.0, "reply_probability": 1.0}
    data.update(profile_overrides)

    async def factory(worker, record):
        async def _close() -> None:
            return None

        return adapter, _close

    return Worker(WorkerProfile(**data), settings=settings, location=location,
                  scenario=ScenarioLibrary().get(scenario),
                  content_manager=content_manager, forum_selector=forum_selector,
                  metrics=metrics, session_manager=session_manager,
                  adapter_factory=factory)


def test_randomized_delay_off_gives_deterministic_pacing(
        settings, location, content_manager, forum_selector, metrics, session_manager):
    """Was: posting.randomized_delay was configurable but never read."""
    settings.posting.randomized_delay = False
    worker = _worker(settings, location, content_manager, forum_selector, metrics,
                     session_manager, FakeSiteAdapter())
    assert worker._interval(10.0, 40.0, randomized=False) == 40.0
    spread = {worker._interval(10.0, 40.0, randomized=True) for _ in range(20)}
    assert len(spread) > 1


async def test_browse_depth_follows_the_session_configuration(
        settings, location, content_manager, forum_selector, metrics, session_manager):
    """Was: session.browse_pages_min/max were never used."""
    settings.session.apply(browse_pages_min=3, browse_pages_max=3)
    depths: list[int] = []

    class RecordingAdapter(FakeSiteAdapter):
        async def browse_forum(self, depth: int = 1):
            depths.append(depth)
            return await super().browse_forum(depth)

    worker = _worker(settings, location, content_manager, forum_selector, metrics,
                     session_manager, RecordingAdapter(), scenario="scenario_03")
    await worker.run_session()
    assert depths and all(depth == 3 for depth in depths)


async def test_behaviour_categories_reach_content_selection(
        settings, location, content_manager, forum_selector, metrics, session_manager):
    """Was: posting.categories / replies.categories were never read."""
    content_manager.post_library.add("Category specific body", category="questions")
    content_manager.build_rotators()
    settings.posting.categories = ["questions"]
    from app.utils.config import ContentSelectionMode

    settings.posting.content_selection_mode = ContentSelectionMode.CATEGORY_BASED
    content_manager.posting = settings.posting
    content_manager.build_rotators()

    adapter = FakeSiteAdapter()
    worker = _worker(settings, location, content_manager, forum_selector, metrics,
                     session_manager, adapter, scenario="scenario_04")
    await worker.run_session()
    assert adapter.store["posts"][0]["body"] == "Category specific body"


# ------------------------------------------------------------- heartbeats
def test_finished_workers_are_not_reported_as_stale():
    """Was: completed workers tripped the stale-heartbeat warning."""
    monitor = HeartbeatMonitor(timeout_seconds=0.0)
    monitor.register("SW-00001")
    monitor.beat("SW-00001", status="COMPLETED")
    monitor.register("SW-00002")
    monitor.beat("SW-00002", status="ACTIVE")
    monitor._beats["SW-00002"].last_seen -= timedelta(seconds=120)
    stale = {beat.worker_id for beat in monitor.stale()}
    assert stale == {"SW-00002"}


# --------------------------------------------------------------- telemetry
async def test_buffer_overflow_flush_is_awaited_not_dropped(database, test_run_id):
    """Was: the out-of-band flush task had no strong reference and could vanish."""
    manager = MetricsManager(database, test_run_id, flush_interval=60.0, buffer_limit=5)
    await manager.start()
    for index in range(12):
        manager.record_event("action", worker_id="SW-00001", action=f"STEP{index}",
                             result="OK")
    await manager.stop()
    assert len(Repository(database).events(test_run_id)) == 12


# --------------------------------------------------------------- thresholds
async def test_threshold_verdict_passes_and_fails(database, metrics, session_manager,
                                                  test_run_id):
    record = await session_manager.start(worker_id="SW-00001", location_key="canada",
                                         scenario="scenario_06")
    metrics.record_execution("OPEN_SITE", worker_id="SW-00001",
                             session_id=record.session_id, location="canada",
                             duration_ms=120.0)
    metrics.record_metric("page_load_ms", 150.0, worker_id="SW-00001",
                          session_id=record.session_id, location="canada")
    record.record_action(ok=True, duration=120.0)
    await session_manager.finish(record)
    await metrics.flush()

    repository = Repository(database)
    generous = evaluate(repository, test_run_id,
                        ThresholdConfig(max_p95_response_ms=1_000.0))
    assert generous.passed is True
    assert generous.label == "PASS"

    strict = evaluate(repository, test_run_id,
                      ThresholdConfig(max_p95_response_ms=10.0))
    assert strict.passed is False
    assert "p95_response_ms" in [check.name for check in strict.failures]

    disabled = evaluate(repository, test_run_id, ThresholdConfig(enabled=False))
    assert disabled.skipped is True
    assert disabled.label == "NOT EVALUATED"


# ------------------------------------------------------------------ robots
def test_robots_check_blocks_a_disallowed_path(monkeypatch):
    """Was: safety.honour_robots_txt was configurable but never enforced."""
    from app.utils import robots

    class _Response:
        def __init__(self, body: str) -> None:
            self._body = body.encode()

        def read(self) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *_exc) -> None:
            return None

    class _Opener:
        def __init__(self, body: str) -> None:
            self._body = body

        def open(self, *_args, **_kwargs):
            return _Response(self._body)

    monkeypatch.setattr(robots.urllib.request, "build_opener",
                        lambda *_a, **_k: _Opener("User-agent: *\nDisallow: /\n"))
    verdict = robots.check("https://example.test/forum")
    assert verdict.checked is True
    assert verdict.allowed is False
    assert "robots.txt" in verdict.as_problem()

    monkeypatch.setattr(robots.urllib.request, "build_opener",
                        lambda *_a, **_k: _Opener("User-agent: *\nAllow: /\n"))
    assert robots.check("https://example.test/forum").allowed is True


def test_robots_failure_is_not_fatal(monkeypatch):
    from app.utils import robots

    class _Opener:
        def open(self, *_args, **_kwargs):
            raise OSError("connection refused")

    monkeypatch.setattr(robots.urllib.request, "build_opener",
                        lambda *_a, **_k: _Opener())
    verdict = robots.check("https://example.test/")
    assert verdict.allowed is True
    assert verdict.checked is False


# ------------------------------------------------------------- live monitor
async def test_live_monitor_exits_on_the_stop_event():
    """Was: only Ctrl+C left the monitor, which tore down the whole console."""
    from cli.dashboard import live_monitor

    stop = asyncio.Event()
    task = asyncio.create_task(live_monitor(lambda: {"state": "RUNNING"},
                                            interval=0.05, clear_screen=False,
                                            stop_event=stop))
    await asyncio.sleep(0.12)
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert task.done() and not task.cancelled()


# --------------------------------------------------------------- CLI threads
async def test_blocking_menu_handlers_do_not_stall_the_event_loop():
    """Was: synchronous wizards ran on the loop and froze a live run."""
    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    beat = asyncio.create_task(heartbeat())
    await asyncio.to_thread(time.sleep, 0.15)   # what _dispatch now does
    beat.cancel()
    assert ticks > 3
