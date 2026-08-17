"""Worker session-execution tests (driven by the fake site adapter)."""

from __future__ import annotations

import asyncio

import pytest

from tests.conftest import FakeSiteAdapter
from app.scheduler.scenarios import ScenarioLibrary
from app.utils.config import ReplyTargetMode
from app.workers.worker import Worker
from app.workers.worker_state import WorkerProfile, WorkerStatus


def _profile(**overrides) -> WorkerProfile:
    data = {
        "worker_id": "SW-00001",
        "location_key": "canada",
        "scenario": "scenario_06",
        "min_post_interval_seconds": 0.0,
        "max_post_interval_seconds": 0.0,
        "min_reply_interval_seconds": 0.0,
        "max_reply_interval_seconds": 0.0,
        "post_probability": 1.0,
        "reply_probability": 1.0,
    }
    data.update(overrides)
    return WorkerProfile(**data)


def _worker(settings, location, content_manager, forum_selector, metrics,
            session_manager, *, adapter: FakeSiteAdapter, profile=None,
            scenario: str = "scenario_06") -> Worker:
    library = ScenarioLibrary()

    async def factory(worker, record):
        adapter.dry_run = worker.settings.dry_run
        return adapter, _noop

    async def _noop() -> None:
        return None

    return Worker(
        profile or _profile(scenario=scenario),
        settings=settings, location=location, scenario=library.get(scenario),
        content_manager=content_manager, forum_selector=forum_selector,
        metrics=metrics, session_manager=session_manager, adapter_factory=factory,
    )


async def test_full_session_creates_a_post_and_a_reply(
        settings, location, content_manager, forum_selector, metrics, session_manager):
    adapter = FakeSiteAdapter()
    worker = _worker(settings, location, content_manager, forum_selector, metrics,
                     session_manager, adapter=adapter)
    summary = await worker.run_session()

    assert summary["status"] == "COMPLETED"
    assert summary["posts_created"] == 1
    assert summary["replies_created"] == 1
    assert summary["username"].startswith("Anonymous_")
    assert summary["avatar"]
    assert len(adapter.store["posts"]) == 1
    assert len(adapter.store["replies"]) == 1
    assert worker.runtime.status is WorkerStatus.COMPLETED


async def test_identity_comes_from_the_site_not_the_worker(
        settings, location, content_manager, forum_selector, metrics, session_manager):
    adapter = FakeSiteAdapter()
    worker = _worker(settings, location, content_manager, forum_selector, metrics,
                     session_manager, adapter=adapter, scenario="scenario_02")
    summary = await worker.run_session()
    assert "GET_AVATARS" in adapter.calls
    assert "GET_USERNAMES" in adapter.calls
    assert summary["username"] in {f"Anonymous_{index + 10}" for index in range(5)}


async def test_dry_run_never_submits(
        settings, location, content_manager, forum_selector, metrics, session_manager):
    settings.dry_run = True
    adapter = FakeSiteAdapter(dry_run=True)
    worker = _worker(settings, location, content_manager, forum_selector, metrics,
                     session_manager, adapter=adapter)
    summary = await worker.run_session()

    assert summary["posts_created"] == 0
    assert summary["replies_created"] == 0
    assert adapter.store["posts"] == []
    assert adapter.store["replies"] == []
    findings = summary["dry_run_findings"]
    assert findings["would_create_post"] is True
    assert findings["would_reply"] is True
    assert findings["initial_posts_detected"] >= 1
    assert findings["avatars_detected"]
    assert findings["usernames_detected"]


async def test_posting_disabled_scenario_does_not_post(
        settings, location, content_manager, forum_selector, metrics, session_manager):
    adapter = FakeSiteAdapter()
    worker = _worker(settings, location, content_manager, forum_selector, metrics,
                     session_manager, adapter=adapter, scenario="scenario_05")
    summary = await worker.run_session()
    assert summary["posts_created"] == 0
    assert summary["replies_created"] == 1


async def test_worker_level_posting_flag_is_respected(
        settings, location, content_manager, forum_selector, metrics, session_manager):
    adapter = FakeSiteAdapter()
    worker = _worker(settings, location, content_manager, forum_selector, metrics,
                     session_manager, adapter=adapter,
                     profile=_profile(posting_enabled=False, replying_enabled=False))
    summary = await worker.run_session()
    assert summary["posts_created"] == 0
    assert summary["replies_created"] == 0


async def test_session_limits_cap_activity(
        settings, location, content_manager, forum_selector, metrics, session_manager):
    adapter = FakeSiteAdapter()
    worker = _worker(settings, location, content_manager, forum_selector, metrics,
                     session_manager, adapter=adapter, scenario="scenario_07",
                     profile=_profile(scenario="scenario_07", max_posts_per_session=1,
                                      max_replies_per_session=2))
    summary = await worker.run_session()
    assert summary["posts_created"] == 1
    assert summary["replies_created"] == 2


async def test_daily_quota_blocks_further_posts(
        settings, location, content_manager, forum_selector, metrics, session_manager):
    adapter = FakeSiteAdapter()
    worker = _worker(settings, location, content_manager, forum_selector, metrics,
                     session_manager, adapter=adapter,
                     profile=_profile(max_posts_per_day=0))
    summary = await worker.run_session()
    assert summary["posts_created"] == 0


async def test_failures_are_recorded_and_tolerated(
        settings, location, content_manager, forum_selector, metrics, session_manager):
    adapter = FakeSiteAdapter(fail_on=["CREATE_POST"])
    worker = _worker(settings, location, content_manager, forum_selector, metrics,
                     session_manager, adapter=adapter)
    summary = await worker.run_session()
    assert summary["posts_created"] == 0
    assert summary["failed_actions"] >= 1
    assert summary["replies_created"] == 1  # the session keeps going

    await metrics.flush()
    from app.database.repository import Repository

    executions = Repository(metrics.database).executions("TEST-RUN-0001",
                                                         action="CREATE_POST")
    assert executions and executions[0]["status"] == "FAILED"


async def test_consecutive_errors_abort_the_session(
        settings, location, content_manager, forum_selector, metrics, session_manager):
    settings.safety.max_consecutive_worker_errors = 1
    adapter = FakeSiteAdapter(fail_on=["OPEN_SITE", "SELECT_AVATAR", "SELECT_USERNAME",
                                       "ENTER_FORUM"])
    worker = _worker(settings, location, content_manager, forum_selector, metrics,
                     session_manager, adapter=adapter)
    summary = await worker.run_session()
    assert summary["status"] == "ABORTED"


async def test_rate_limit_triggers_backoff_and_is_recorded(
        settings, location, content_manager, forum_selector, metrics, session_manager):
    settings.safety.backoff_initial_seconds = 1.0
    settings.safety.backoff_max_seconds = 1.0
    adapter = FakeSiteAdapter(rate_limit_on=["OPEN_SITE"])
    worker = _worker(settings, location, content_manager, forum_selector, metrics,
                     session_manager, adapter=adapter)
    summary = await worker.run_session()

    assert summary["status"] == "RATE_LIMITED"
    assert worker.runtime.rate_limit_hits == 1
    await metrics.flush()
    from app.database.repository import Repository

    errors = Repository(metrics.database).errors("TEST-RUN-0001")
    assert any(error["error_type"] == "RateLimited" for error in errors)


async def test_stop_event_ends_the_session_early(
        settings, location, content_manager, forum_selector, metrics, session_manager):
    adapter = FakeSiteAdapter()
    worker = _worker(settings, location, content_manager, forum_selector, metrics,
                     session_manager, adapter=adapter)
    worker.stop_event.set()
    summary = await worker.run_session()
    assert summary["status"] == "STOPPED"
    assert adapter.store["posts"] == []


async def test_reply_target_mode_selects_the_oldest_post(
        settings, location, content_manager, forum_selector, metrics, session_manager):
    settings.replies.target_selection_mode = ReplyTargetMode.OLDEST
    adapter = FakeSiteAdapter(initial_posts=4)
    worker = _worker(settings, location, content_manager, forum_selector, metrics,
                     session_manager, adapter=adapter, scenario="scenario_05")
    await worker.run_session()
    assert adapter.store["replies"][0]["post_id"].endswith("-0")


async def test_telemetry_is_persisted(
        settings, location, content_manager, forum_selector, metrics, session_manager):
    adapter = FakeSiteAdapter()
    worker = _worker(settings, location, content_manager, forum_selector, metrics,
                     session_manager, adapter=adapter)
    await worker.run_session()
    await metrics.flush()

    from app.database.repository import Repository

    repository = Repository(metrics.database)
    summary = repository.overall_summary("TEST-RUN-0001")
    assert summary["sessions"] == 1
    assert summary["posts"] == 1
    assert summary["replies"] == 1
    events = repository.events("TEST-RUN-0001")
    assert any(event["event_type"] == "session_start" for event in events)
    assert any(event["event_type"] == "session_end" for event in events)


async def test_content_rotation_avoids_repeats_across_sessions(
        settings, location, content_manager, forum_selector, metrics, session_manager):
    used: list[str] = []
    for index in range(3):
        adapter = FakeSiteAdapter()
        worker = _worker(settings, location, content_manager, forum_selector, metrics,
                         session_manager, adapter=adapter, scenario="scenario_04")
        await worker.run_session()
        used.append(adapter.store["posts"][0]["body"])
    assert len(set(used)) == 3
