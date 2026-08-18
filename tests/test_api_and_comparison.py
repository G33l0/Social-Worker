"""Tests for the control API, run comparison and acceptance verdicts."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeSiteAdapter
from app.api.control_api import create_app, serve
from app.core.controller import Controller
from app.database.repository import Repository
from app.locations.location import Location
from app.locations.location_manager import LocationManager
from app.reporting.comparison import compare
from app.utils.config import ConfigPaths, save_settings, save_yaml_document


@pytest.fixture
def paths(tmp_path, settings) -> ConfigPaths:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_paths = ConfigPaths.under(config_dir)
    save_settings(settings, config_paths.settings)
    LocationManager([Location(key="canada", workers=2, max_concurrent_workers=2)]).save(
        config_paths.locations)
    save_yaml_document(config_paths.workers, {"workers": []})
    (tmp_path / "posts.txt").write_text("\n".join(f"Post {i}" for i in range(20)),
                                        encoding="utf-8")
    (tmp_path / "replies.txt").write_text("\n".join(f"Reply {i}" for i in range(20)),
                                          encoding="utf-8")
    return config_paths


def _controller(settings, paths) -> Controller:
    async def factory(worker, record):
        async def close() -> None:
            return None

        return FakeSiteAdapter(dry_run=worker.settings.dry_run), close

    return Controller(settings, config_paths=paths, adapter_factory=factory)


# ------------------------------------------------------------------ control API
def test_health_needs_no_token_but_status_does(settings, paths):
    controller = _controller(settings, paths)
    client = TestClient(create_app(controller, token="s3cret"))

    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/status").status_code == 401
    assert client.get("/status", headers={"Authorization": "Bearer wrong"}
                      ).status_code == 401
    authorized = client.get("/status", headers={"Authorization": "Bearer s3cret"})
    assert authorized.status_code == 200
    assert authorized.json()["state"] == "IDLE"


def test_api_runs_a_test_and_reports_the_verdict(settings, paths):
    settings.concurrency.max_global_workers = 2
    controller = _controller(settings, paths)
    client = TestClient(create_app(controller))

    started = client.post("/test/start", json={"label": "api-test"})
    assert started.status_code == 200
    test_run_id = started.json()["test_run_id"]

    stopped = client.post("/test/stop")
    assert stopped.status_code == 200

    verdict = client.get("/verdict", params={"test_run_id": test_run_id})
    assert verdict.status_code == 200
    assert verdict.json()["verdict"] in {"PASS", "FAIL"}

    runs = client.get("/runs").json()
    assert any(run["test_run_id"] == test_run_id for run in runs)
    assert client.get(f"/reports/{test_run_id}/geographic").status_code == 200
    assert client.get(f"/reports/{test_run_id}/nonsense").status_code == 404


def test_starting_a_second_run_while_one_is_active_conflicts(settings, paths):
    """A concurrent start must be refused, not silently begun twice."""
    from app.core.controller import ControllerState

    controller = _controller(settings, paths)
    controller.state = ControllerState.RUNNING
    client = TestClient(create_app(controller))
    assert client.post("/test/start", json={}).status_code == 409


def test_agent_registration_and_heartbeat(settings, paths):
    controller = _controller(settings, paths)
    client = TestClient(create_app(controller))

    registered = client.post("/workers/register",
                             json={"worker_id": "SW-90001",
                                   "location_key": "canada", "capacity": 4})
    assert registered.json()["registered"] == "SW-90001"

    beat = client.post("/workers/SW-90001/heartbeat",
                       json={"status": "ACTIVE", "action": "POSTING"})
    assert beat.json()["acknowledged"] is True
    assert beat.json()["stop_requested"] is False

    agents = client.get("/heartbeats").json()
    assert any(agent["worker_id"] == "SW-90001" for agent in agents["agents"])
    assert agents["stale"] == []


def test_api_refuses_to_bind_a_public_address_without_a_token(settings, paths):
    controller = _controller(settings, paths)
    controller.settings.controller.host = "0.0.0.0"
    controller.settings.controller.api_token = ""
    with pytest.raises(ValueError, match="without an API token"):
        serve(controller, run=lambda *_a, **_k: None)


def test_api_binds_a_public_address_when_a_token_is_set(settings, paths):
    controller = _controller(settings, paths)
    controller.settings.controller.host = "0.0.0.0"
    controller.settings.controller.api_token = "s3cret"
    called: dict[str, object] = {}
    serve(controller, run=lambda app, **kwargs: called.update(kwargs))
    assert called["host"] == "0.0.0.0"


def test_api_token_is_not_written_into_the_run_snapshot(settings, paths):
    settings.controller.api_token = "super-secret-token"
    settings.concurrency.max_global_workers = 1
    controller = _controller(settings, paths)
    import asyncio

    async def run() -> str:
        run_id = await controller.prepare(label="secret")
        await controller.shutdown()
        return run_id

    test_run_id = asyncio.get_event_loop().run_until_complete(run()) \
        if False else asyncio.run(run())
    stored = Repository(controller.database).get_test_run(test_run_id)
    assert "super-secret-token" not in str(stored["config_snapshot"])


# ------------------------------------------------------------------ comparison
async def _seed_run(database, session_manager, metrics, location: str,
                    response_ms: float, sessions: int) -> None:
    for index in range(sessions):
        record = await session_manager.start(worker_id=f"SW-0000{index + 1}",
                                             location_key=location,
                                             scenario="scenario_06")
        metrics.record_execution("OPEN_SITE", worker_id=record.worker_id,
                                 session_id=record.session_id, location=location,
                                 duration_ms=response_ms)
        metrics.record_metric("page_load_ms", response_ms, worker_id=record.worker_id,
                              session_id=record.session_id, location=location)
        record.record_action(ok=True, duration=response_ms)
        record.posts_created = 1
        await session_manager.finish(record)
    await metrics.flush()


async def test_comparison_detects_a_regression(database, test_run_id, session_manager,
                                               metrics):
    from app.core.metrics_manager import MetricsManager
    from app.core.session_manager import SessionManager
    from app.database.models import TestRun

    await _seed_run(database, session_manager, metrics, "canada", 100.0, 3)

    def _second_run(session):
        session.add(TestRun(test_run_id="TEST-RUN-0002", label="candidate",
                            status="COMPLETED"))

    database.write(_second_run)
    candidate_sessions = SessionManager(database, "TEST-RUN-0002")
    candidate_metrics = MetricsManager(database, "TEST-RUN-0002", flush_interval=0.05)
    await _seed_run(database, candidate_sessions, candidate_metrics, "canada", 400.0, 3)

    repository = Repository(database)
    result = compare(repository, test_run_id, "TEST-RUN-0002", tolerance=0.10)
    assert result.passed is False
    assert "avg_response_ms" in [metric.metric for metric in result.regressions]
    row = next(row for row in result.to_dict()["metrics"]
               if row["metric"] == "avg_response_ms")
    assert row["verdict"] == "REGRESSED"
    assert "+300.0%" in row["change"]


async def test_comparison_passes_when_runs_match(database, test_run_id,
                                                 session_manager, metrics):
    from app.core.metrics_manager import MetricsManager
    from app.core.session_manager import SessionManager
    from app.database.models import TestRun

    await _seed_run(database, session_manager, metrics, "canada", 120.0, 2)

    def _second_run(session):
        session.add(TestRun(test_run_id="TEST-RUN-0003", label="candidate",
                            status="COMPLETED"))

    database.write(_second_run)
    await _seed_run(database, SessionManager(database, "TEST-RUN-0003"),
                    MetricsManager(database, "TEST-RUN-0003", flush_interval=0.05),
                    "canada", 125.0, 2)

    result = compare(Repository(database), test_run_id, "TEST-RUN-0003",
                     tolerance=0.10)
    assert result.passed is True
    assert "No regression" in result.summary_line()
