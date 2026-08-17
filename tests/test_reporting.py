"""Reporting, metrics and persistence tests."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from app.database.repository import Repository, _percentiles
from app.reporting.report_manager import REPORT_CATEGORIES, ReportManager


@pytest.fixture
async def populated(database, metrics, session_manager, test_run_id):
    """A test run with two sessions, posts, replies and one error."""
    for index, location in enumerate(("canada", "united_states")):
        record = await session_manager.start(worker_id=f"SW-0000{index + 1}",
                                             location_key=location,
                                             scenario="scenario_06")
        metrics.session_started(location)
        metrics.record_execution("OPEN_SITE", worker_id=record.worker_id,
                                 session_id=record.session_id, location=location,
                                 duration_ms=120.0 + index, http_status=200,
                                 url="http://127.0.0.1:8099/")
        metrics.record_execution("CREATE_POST", worker_id=record.worker_id,
                                 session_id=record.session_id, location=location,
                                 forum="general", content_ref=f"POST-00{index + 1}",
                                 duration_ms=300.0 + index)
        metrics.record_execution("CREATE_REPLY", worker_id=record.worker_id,
                                 session_id=record.session_id, location=location,
                                 forum="general", content_ref=f"REPLY-00{index + 1}",
                                 duration_ms=210.0 + index)
        metrics.record_metric("page_load_ms", 90.0 + index, worker_id=record.worker_id,
                              session_id=record.session_id, location=location)
        record.posts_created = 1
        record.replies_created = 1
        record.pages_visited = 4
        record.forums_visited = 2
        record.record_action(ok=True, duration=120.0)
        await session_manager.finish(record)
        metrics.session_finished(location, duration_ms=record.duration_ms,
                                 success=True, pages_visited=4)

    metrics.record_error("AdapterError", "No composer found", worker_id="SW-00001",
                         location="canada", action="CREATE_POST",
                         screenshot_path="data/debug/x.png")
    await metrics.flush()
    return test_run_id


async def test_overall_summary_counts_everything(database, populated):
    repository = Repository(database)
    summary = repository.overall_summary(populated)
    assert summary["sessions"] == 2
    assert summary["posts"] == 2
    assert summary["replies"] == 2
    assert summary["errors"] == 1
    assert summary["avg_response_ms"] > 0


async def test_geographic_summary_has_a_row_per_location(database, populated):
    rows = Repository(database).geographic_summary(populated)
    keys = {row["location"] for row in rows}
    assert {"canada", "united_states"} <= keys
    canada = next(row for row in rows if row["location"] == "canada")
    assert canada["posts"] == 1
    assert canada["replies"] == 1
    assert canada["errors"] == 1
    assert canada["avg_response_ms"] > 0


async def test_performance_percentiles(database, populated):
    performance = Repository(database).performance_summary(populated)
    assert "create_post_ms" in performance
    stats = performance["create_post_ms"]
    assert stats["count"] == 2
    assert stats["p95"] >= stats["p50"]


def test_percentile_helper_edges():
    assert _percentiles([])["count"] == 0
    stats = _percentiles([10.0, 20.0, 30.0, 40.0])
    assert stats["min"] == 10.0 and stats["max"] == 40.0
    assert stats["avg"] == 25.0


async def test_every_report_category_builds(database, populated):
    manager = ReportManager(database)
    for category in REPORT_CATEGORIES:
        payload = manager.build(category, populated)
        assert payload is not None


async def test_export_writes_json_csv_and_html(database, populated, tmp_path):
    manager = ReportManager(database, output_directory=tmp_path / "reports")
    written = manager.export_all(populated)
    assert written

    suffixes = {path.suffix for path in written}
    assert {".json", ".csv", ".html"} <= suffixes

    geographic_json = next(path for path in written
                           if path.name.startswith("geographic") and path.suffix == ".json")
    payload = json.loads(geographic_json.read_text(encoding="utf-8"))
    assert payload["test_run_id"] == populated
    assert any(row["location"] == "canada" for row in payload["data"])

    geographic_csv = next(path for path in written
                          if path.name.startswith("geographic") and path.suffix == ".csv")
    rows = list(csv.DictReader(geographic_csv.read_text(encoding="utf-8").splitlines()))
    assert rows and "avg_response_ms" in rows[0]

    index = next(path for path in written if path.name == "index.html")
    html = index.read_text(encoding="utf-8")
    assert "Social Worker Test Report" in html
    assert populated in html
    assert "<script" not in html.lower()


async def test_error_report_includes_debug_artefacts(database, populated):
    manager = ReportManager(database)
    payload = manager.build("error", populated)
    assert payload["errors"][0]["screenshot_path"].endswith(".png")
    assert payload["breakdown"][0]["error_type"] == "AdapterError"


async def test_content_usage_is_tracked_per_report(database, populated):
    manager = ReportManager(database)
    posts = manager.build("post", populated)
    refs = {row["content_ref"] for row in posts["content_usage"]}
    assert refs == {"POST-001", "POST-002"}


async def test_reports_survive_a_fresh_database_handle(database, populated, tmp_path,
                                                       settings):
    """Persistence check: a new connection sees the same test run."""
    from app.database.database import Database

    reopened = Database(settings.database)
    repository = Repository(reopened)
    runs = repository.list_test_runs()
    assert any(run["test_run_id"] == populated for run in runs)
    assert repository.overall_summary(populated)["sessions"] == 2
    reopened.dispose()


def test_html_writer_escapes_untrusted_text(tmp_path):
    from app.reporting.html_report import write_html

    path = write_html("Report", [("Rows", [{"detail": "<script>alert(1)</script>"}])],
                      tmp_path / "out.html")
    html = Path(path).read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
