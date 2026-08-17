"""Controller integration tests: planning, concurrency, stop controls, reports."""

from __future__ import annotations

import asyncio
import random

import pytest

from tests.conftest import FakeSiteAdapter
from app.core.controller import AuthorizationError, Controller, ControllerState
from app.database.repository import Repository
from app.locations.location import Location
from app.locations.location_manager import LocationManager
from app.utils.config import ConfigPaths, save_settings, save_yaml_document


@pytest.fixture
def paths(tmp_path, settings) -> ConfigPaths:
    """A configuration directory with three locations and no explicit workers."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_paths = ConfigPaths.under(config_dir)
    save_settings(settings, config_paths.settings)
    manager = LocationManager([
        Location(key="canada", country="Canada", workers=2, max_concurrent_workers=2),
        Location(key="united_states", country="United States", workers=4,
                 max_concurrent_workers=3),
        Location(key="nigeria", country="Nigeria", workers=2, max_concurrent_workers=2),
    ])
    manager.save(config_paths.locations)
    manager.save_schedules(config_paths.schedules)
    save_yaml_document(config_paths.workers, {"workers": []})
    (tmp_path / "posts.txt").write_text(
        "\n".join(f"Post {index:03d}" for index in range(1, 41)), encoding="utf-8")
    (tmp_path / "replies.txt").write_text(
        "\n".join(f"Reply {index:03d}" for index in range(1, 41)), encoding="utf-8")
    return config_paths


def _controller(settings, paths, *, tracker: dict | None = None,
                fail_on=(), rate_limit_on=()) -> Controller:
    """Controller wired to the fake adapter, with a shared post/reply store."""
    store = tracker if tracker is not None else {"posts": [], "replies": []}
    live: dict[str, int] = {"active": 0, "peak": 0, "per_location_peak": {}}

    async def factory(worker, record):
        adapter = FakeSiteAdapter(dry_run=worker.settings.dry_run, store=store,
                                  fail_on=fail_on, rate_limit_on=rate_limit_on,
                                  rng=random.Random(len(store["posts"]) + 1))
        live["active"] += 1
        live["peak"] = max(live["peak"], live["active"])
        key = worker.location.key
        counts = live.setdefault("per_location", {})
        counts[key] = counts.get(key, 0) + 1
        live["per_location_peak"][key] = max(live["per_location_peak"].get(key, 0),
                                             counts[key])
        await asyncio.sleep(0.01)

        async def close() -> None:
            live["active"] -= 1
            counts[key] -= 1

        return adapter, close

    controller = Controller(settings, config_paths=paths, adapter_factory=factory)
    controller.live_probe = live  # type: ignore[attr-defined]
    return controller


async def test_preflight_blocks_unauthorized_runs(settings, paths):
    settings.website.authorized_test_mode = False
    controller = _controller(settings, paths)
    problems = controller.preflight()
    assert any("Authorized test mode" in problem for problem in problems)
    with pytest.raises(AuthorizationError):
        await controller.prepare()
    await controller.shutdown()


async def test_run_plan_respects_location_allocation(settings, paths):
    settings.concurrency.max_global_workers = 4
    controller = _controller(settings, paths)
    await controller.prepare(label="allocation")
    per_location: dict[str, int] = {}
    for profile in controller._plan:  # noqa: SLF001 - inspecting the built plan
        per_location[profile.location_key] = per_location.get(profile.location_key, 0) + 1
    assert sum(per_location.values()) == 4
    assert set(per_location) <= {"canada", "united_states", "nigeria"}
    assert per_location["united_states"] >= per_location["canada"]
    await controller.shutdown()


async def test_full_run_produces_activity_and_persists(settings, paths):
    settings.concurrency.max_global_workers = 6
    settings.session.sessions_per_worker = 1
    tracker = {"posts": [], "replies": []}
    controller = _controller(settings, paths, tracker=tracker)

    await controller.start(label="integration", wait=True)
    status = controller.status()

    assert status["state"] == ControllerState.COMPLETED
    assert status["global"]["completed"] == 6
    assert status["global"]["posts"] == len(tracker["posts"]) > 0
    assert status["global"]["replies"] == len(tracker["replies"]) > 0

    repository = Repository(controller.database)
    summary = repository.overall_summary(controller.test_run_id)
    assert summary["sessions"] == 6
    assert summary["posts"] == status["global"]["posts"]
    assert summary["workers"] == 6
    geographic = repository.geographic_summary(controller.test_run_id)
    assert len(geographic) >= 3
    await controller.shutdown()


async def test_concurrency_ceilings_are_never_exceeded(settings, paths):
    settings.concurrency.max_global_workers = 4
    settings.session.sessions_per_worker = 2
    controller = _controller(settings, paths)
    await controller.start(label="concurrency", wait=True)

    probe = controller.live_probe  # type: ignore[attr-defined]
    assert probe["peak"] <= 4
    assert probe["per_location_peak"].get("canada", 0) <= 2
    assert probe["per_location_peak"].get("united_states", 0) <= 3
    snapshot = controller.scheduler.snapshot()["concurrency"]
    assert snapshot["global_peak"] <= 4
    await controller.shutdown()


async def test_dry_run_reports_findings_without_submitting(settings, paths):
    tracker = {"posts": [], "replies": []}
    controller = _controller(settings, paths, tracker=tracker)
    result = await controller.run_dry(workers=2)

    assert tracker["posts"] == []
    assert tracker["replies"] == []
    assert result["findings"]
    finding = result["findings"][0]
    assert finding["avatars_detected"]
    assert finding["forums_detected"]
    assert finding["would_create_post"] is True

    run = Repository(controller.database).get_test_run(result["test_run_id"])
    assert run["dry_run"] is True
    await controller.shutdown()


async def test_stop_all_halts_everything(settings, paths):
    settings.concurrency.max_global_workers = 4
    settings.session.sessions_per_worker = 5
    settings.session.max_think_time_seconds = 0.5
    settings.session.min_think_time_seconds = 0.5
    controller = _controller(settings, paths)

    await controller.start(label="stop-all")
    await asyncio.sleep(0.3)
    result = await controller.stop_all(reason="test")

    assert controller.state == ControllerState.STOPPED
    assert result["dropped"] + result["cancelled"] > 0
    assert controller.scheduler.stats()["running"] == 0

    run = Repository(controller.database).get_test_run(controller.test_run_id)
    assert run["status"] == "STOPPED"
    assert run["ended_at"]
    await controller.shutdown()


async def test_pause_and_resume(settings, paths):
    settings.concurrency.max_global_workers = 2
    settings.session.sessions_per_worker = 4
    settings.session.max_think_time_seconds = 0.2
    settings.session.min_think_time_seconds = 0.2
    controller = _controller(settings, paths)

    await controller.start(label="pause")
    await controller.pause()
    assert controller.state == ControllerState.PAUSED
    dispatched = controller.scheduler.stats()["dispatched"]
    await asyncio.sleep(0.3)
    assert controller.scheduler.stats()["dispatched"] - dispatched <= 1

    await controller.resume()
    assert controller.state == ControllerState.RUNNING
    await controller.wait()
    assert controller.scheduler.stats()["completed"] == 8
    await controller.shutdown()


async def test_stop_location_only_stops_that_location(settings, paths):
    settings.concurrency.max_global_workers = 6
    settings.session.sessions_per_worker = 3
    settings.session.max_think_time_seconds = 0.2
    settings.session.min_think_time_seconds = 0.2
    controller = _controller(settings, paths)

    await controller.start(label="stop-location")
    await controller.stop_location("canada")
    await controller.wait()

    repository = Repository(controller.database)
    rows = {row["location"]: row for row in
            repository.geographic_summary(controller.test_run_id)}
    assert rows["united_states"]["sessions"] > 0
    await controller.shutdown()


async def test_forum_weighting_is_applied(settings, paths):
    settings.concurrency.max_global_workers = 6
    settings.forums.weights = {"general": 100.0, "confessions": 0.0,
                               "questions": 0.0, "random": 0.0}
    controller = _controller(settings, paths)
    await controller.start(label="forums", wait=True)

    distribution = controller.forum_selector.distribution()
    assert distribution.get("general", 0) == 1.0
    forums = Repository(controller.database).forum_summary(controller.test_run_id)
    assert any(row["forum"] == "general" and row["visits"] > 0 for row in forums)
    await controller.shutdown()


async def test_errors_are_logged_to_the_database(settings, paths):
    settings.concurrency.max_global_workers = 2
    controller = _controller(settings, paths, fail_on=["CREATE_POST"])
    await controller.start(label="errors", wait=True)

    executions = Repository(controller.database).executions(controller.test_run_id,
                                                            action="CREATE_POST")
    assert executions and all(row["status"] == "FAILED" for row in executions)
    await controller.shutdown()


async def test_reports_export_after_a_run(settings, paths, tmp_path):
    settings.concurrency.max_global_workers = 2
    settings.reporting.auto_export_on_stop = True
    controller = _controller(settings, paths)
    await controller.start(label="reports", wait=True)

    files = controller.reports.list_reports(controller.test_run_id)
    assert any(path.name == "index.html" for path in files)
    assert any(path.suffix == ".csv" for path in files)
    assert any(path.suffix == ".json" for path in files)
    await controller.shutdown()


async def test_rate_limit_stops_the_run(settings, paths):
    settings.concurrency.max_global_workers = 2
    settings.safety.stop_on_rate_limit_streak = 1
    settings.safety.backoff_initial_seconds = 1.0
    settings.safety.backoff_max_seconds = 1.0
    controller = _controller(settings, paths, rate_limit_on=["OPEN_SITE"])
    await controller.start(label="rate-limit", wait=True)

    errors = Repository(controller.database).errors(controller.test_run_id)
    assert any(error["error_type"] == "RateLimited" for error in errors)
    assert controller.state in {ControllerState.STOPPED, ControllerState.COMPLETED}
    await controller.shutdown()
