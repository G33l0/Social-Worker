"""Report generation for a completed (or running) test run.

Eight report categories are produced, each available as JSON, CSV and HTML:

1. Overall Test Report      5. Post Report
2. Geographic Report        6. Reply Report
3. Worker Report            7. Error Report
4. Forum Report             8. Performance Report
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Sequence

from app.database.database import Database
from app.database.repository import Repository
from app.reporting.csv_report import write_csv
from app.reporting.html_report import write_html
from app.reporting.json_report import write_json
from app.utils.logger import get_logger
from app.utils.time_utils import timestamp_slug

LOGGER = get_logger("reporting")

REPORT_CATEGORIES = ("overall", "geographic", "worker", "forum", "post", "reply",
                     "error", "performance")


class ReportManager:
    """Builds report payloads and writes them in every requested format."""

    def __init__(self, database: Database, *,
                 output_directory: str | Path = "data/reports",
                 verdict: Any = None) -> None:
        self.database = database
        self.repository = Repository(database)
        self.output_directory = Path(output_directory)
        self.verdict = verdict

    # ----------------------------------------------------------------- build
    def overall(self, test_run_id: str) -> dict[str, Any]:
        """Headline counters plus the run's metadata."""
        run = self.repository.get_test_run(test_run_id) or {"test_run_id": test_run_id}
        summary = self.repository.overall_summary(test_run_id)
        summary["error_rate"] = (
            round(summary["failed_actions"] /
                  max(1, summary["successful_actions"] + summary["failed_actions"]), 4)
        )
        payload: dict[str, Any] = {"test_run": run, "summary": summary}
        verdict = self._verdict_for(test_run_id)
        if verdict is not None:
            payload["verdict"] = verdict["verdict"]
            payload["acceptance_checks"] = verdict["checks"]
        return payload

    def _verdict_for(self, test_run_id: str) -> dict[str, Any] | None:
        """The acceptance verdict for *test_run_id*, when one was evaluated."""
        verdict = self.verdict
        if verdict is None:
            return None
        data = verdict.to_dict() if hasattr(verdict, "to_dict") else dict(verdict)
        return data if data.get("test_run_id") == test_run_id else None

    def geographic(self, test_run_id: str) -> list[dict[str, Any]]:
        """LOCATION | SESSIONS | POSTS | REPLIES | AVG RESPONSE | ERRORS."""
        return self.repository.geographic_summary(test_run_id)

    def workers(self, test_run_id: str) -> list[dict[str, Any]]:
        """Per-worker activity."""
        return self.repository.worker_summary(test_run_id)

    def forums(self, test_run_id: str) -> list[dict[str, Any]]:
        """Per-forum activity."""
        return self.repository.forum_summary(test_run_id)

    def posts(self, test_run_id: str) -> dict[str, Any]:
        """Every post submission plus content-rotation usage."""
        return {
            "executions": self.repository.executions(test_run_id, action="CREATE_POST"),
            "content_usage": self.repository.content_usage(test_run_id, "CREATE_POST"),
        }

    def replies(self, test_run_id: str) -> dict[str, Any]:
        """Every reply submission plus content-rotation usage."""
        return {
            "executions": self.repository.executions(test_run_id, action="CREATE_REPLY"),
            "content_usage": self.repository.content_usage(test_run_id, "CREATE_REPLY"),
        }

    def errors(self, test_run_id: str) -> dict[str, Any]:
        """Errors with their debug artefacts, plus a breakdown by type."""
        return {
            "errors": self.repository.errors(test_run_id),
            "breakdown": self.repository.error_breakdown(test_run_id),
        }

    def performance(self, test_run_id: str) -> dict[str, Any]:
        """Percentiles for every recorded timing metric."""
        return {
            "metrics": self.repository.performance_summary(test_run_id),
            "sessions": self.repository.sessions(test_run_id),
        }

    def build(self, category: str, test_run_id: str) -> Any:
        """Build one report category by name."""
        builders: dict[str, Callable[[str], Any]] = {
            "overall": self.overall,
            "geographic": self.geographic,
            "worker": self.workers,
            "forum": self.forums,
            "post": self.posts,
            "reply": self.replies,
            "error": self.errors,
            "performance": self.performance,
        }
        if category not in builders:
            raise KeyError(f"Unknown report category: {category}")
        return builders[category](test_run_id)

    # ----------------------------------------------------------------- write
    def _run_directory(self, test_run_id: str) -> Path:
        target = self.output_directory / test_run_id
        target.mkdir(parents=True, exist_ok=True)
        return target

    def export(self, category: str, test_run_id: str,
               formats: Sequence[str] = ("json", "csv", "html")) -> list[Path]:
        """Write one report category in each requested format."""
        payload = self.build(category, test_run_id)
        directory = self._run_directory(test_run_id)
        stamp = timestamp_slug()
        written: list[Path] = []

        for fmt in formats:
            name = f"{category}-{stamp}.{fmt}"
            path = directory / name
            if fmt == "json":
                written.append(write_json(payload, path,
                                          metadata={"test_run_id": test_run_id,
                                                    "category": category}))
            elif fmt == "csv":
                written.extend(self._export_csv(category, payload, directory, stamp))
            elif fmt == "html":
                written.append(self._export_html(category, test_run_id, payload, path))
            else:
                raise ValueError(f"Unsupported report format: {fmt}")
        return written

    def _export_csv(self, category: str, payload: Any, directory: Path,
                    stamp: str) -> list[Path]:
        written: list[Path] = []
        if isinstance(payload, list):
            written.append(write_csv(payload, directory / f"{category}-{stamp}.csv"))
        elif isinstance(payload, dict):
            table_written = False
            for key, value in payload.items():
                if isinstance(value, list) and value and isinstance(value[0], dict):
                    written.append(write_csv(
                        value, directory / f"{category}-{key}-{stamp}.csv"))
                    table_written = True
            if not table_written:
                rows = _flatten_mapping(payload)
                written.append(write_csv(rows, directory / f"{category}-{stamp}.csv",
                                         columns=["metric", "value"]))
        return written

    def _export_html(self, category: str, test_run_id: str, payload: Any,
                     path: Path) -> Path:
        title = f"{category.title()} Report"
        subtitle = f"Test run {test_run_id}"
        summary: dict[str, Any] = {}
        sections: list[tuple[str, Any]] = []

        if category == "overall" and isinstance(payload, dict):
            summary = {
                "Sessions": payload["summary"]["sessions"],
                "Posts": payload["summary"]["posts"],
                "Replies": payload["summary"]["replies"],
                "Errors": payload["summary"]["errors"],
                "Avg response": f"{payload['summary']['avg_response_ms']} ms",
                "Workers": payload["summary"]["workers"],
            }
            if payload.get("verdict"):
                summary = {"Verdict": payload["verdict"], **summary}
            sections = [("Test run", payload["test_run"]),
                        ("Summary", payload["summary"])]
            if payload.get("acceptance_checks"):
                sections.append(("Acceptance checks", payload["acceptance_checks"]))
        elif isinstance(payload, list):
            sections = [(title, payload)]
        elif isinstance(payload, dict):
            for key, value in payload.items():
                sections.append((key.replace("_", " ").title(), value))

        notice = ("Dry run: no posts or replies were submitted to the target site."
                  if _is_dry_run(payload) else "")
        return write_html(f"Social Worker {title}", sections, path,
                          summary=summary, subtitle=subtitle, notice=notice)

    def export_all(self, test_run_id: str,
                   formats: Sequence[str] = ("json", "csv", "html"),
                   categories: Sequence[str] = REPORT_CATEGORIES) -> list[Path]:
        """Write every report category for a test run."""
        written: list[Path] = []
        for category in categories:
            try:
                written.extend(self.export(category, test_run_id, formats))
            except Exception as exc:  # pragma: no cover - one bad category must not
                LOGGER.error("Failed to export %s report: %s", category, exc)
        index = self._write_index(test_run_id, written)
        written.append(index)
        LOGGER.info("Wrote %d report file(s) to %s", len(written),
                    self._run_directory(test_run_id))
        return written

    def _write_index(self, test_run_id: str, files: Sequence[Path]) -> Path:
        overall = self.overall(test_run_id)
        geographic = self.geographic(test_run_id)
        performance = self.repository.performance_summary(test_run_id)
        summary = overall["summary"]

        cards = {
            "Sessions": summary["sessions"],
            "Posts": summary["posts"],
            "Replies": summary["replies"],
            "Errors": summary["errors"],
            "Avg response": f"{summary['avg_response_ms']} ms",
            "Workers": summary["workers"],
        }
        if overall.get("verdict"):
            cards = {"Verdict": overall["verdict"], **cards}

        sections: list[tuple[str, Any]] = [("Test run", overall["test_run"])]
        if overall.get("acceptance_checks"):
            sections.append(("Acceptance checks", overall["acceptance_checks"]))
        sections += [
            ("Geographic summary", geographic),
            ("Response time percentiles",
             [{"metric": name, **stats} for name, stats in performance.items()]),
            ("Files", [{"file": path.name} for path in files]),
        ]
        return write_html(
            "Social Worker Test Report",
            sections,
            self._run_directory(test_run_id) / "index.html",
            summary=cards,
            subtitle=f"Test run {test_run_id}",
            charts=self._charts(test_run_id, geographic, performance),
            verdict=overall.get("verdict", ""),
        )

    def _charts(self, test_run_id: str, geographic: list[dict[str, Any]],
                performance: dict[str, Any]) -> list[dict[str, Any]]:
        """Chart specifications rendered inline as SVG by the HTML writer."""
        charts: list[dict[str, Any]] = []
        if geographic:
            charts.append({
                "kind": "bars",
                "title": "Average response time by location",
                "unit": "ms",
                "series": [{"label": row["location"],
                            "value": float(row.get("avg_response_ms", 0.0))}
                           for row in geographic],
            })
            charts.append({
                "kind": "grouped_bars",
                "title": "Activity by location",
                "unit": "count",
                "groups": ["sessions", "posts", "replies", "errors"],
                "series": [{"label": row["location"],
                            "values": [row.get("sessions", 0), row.get("posts", 0),
                                       row.get("replies", 0), row.get("errors", 0)]}
                           for row in geographic],
            })
        timeline = self._response_timeline(test_run_id)
        if len(timeline) > 1:
            charts.append({
                "kind": "line",
                "title": "Response time over the run",
                "unit": "ms",
                "points": timeline,
            })
        page = _first_percentiles(performance)
        if page:
            charts.append({
                "kind": "percentiles",
                "title": f"Response time distribution ({page[0]})",
                "unit": "ms",
                "values": page[1],
            })
        return charts

    def _response_timeline(self, test_run_id: str, buckets: int = 24
                           ) -> list[dict[str, float]]:
        """Average response time and throughput per time bucket."""
        executions = self.repository.executions(test_run_id)
        stamped = [row for row in executions if row.get("created_at")]
        if len(stamped) < 2:
            return []
        from datetime import datetime

        times = [datetime.fromisoformat(row["created_at"]) for row in stamped]
        start, end = min(times), max(times)
        span = (end - start).total_seconds()
        if span <= 0:
            return []
        width = span / buckets
        totals = [[0.0, 0] for _ in range(buckets)]
        for row, moment in zip(stamped, times):
            index = min(buckets - 1, int((moment - start).total_seconds() / width))
            totals[index][0] += float(row.get("duration_ms", 0.0))
            totals[index][1] += 1
        return [
            {"t": round(index * width, 1),
             "value": round(total / count, 2) if count else 0.0,
             "count": count}
            for index, (total, count) in enumerate(totals)
        ]

    def list_reports(self, test_run_id: str) -> list[Path]:
        """Existing report files for a test run."""
        directory = self.output_directory / test_run_id
        if not directory.exists():
            return []
        return sorted(path for path in directory.iterdir() if path.is_file())


def _flatten_mapping(payload: dict[str, Any], prefix: str = "") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key, value in payload.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            rows.extend(_flatten_mapping(value, prefix=f"{name}."))
        else:
            rows.append({"metric": name, "value": value})
    return rows


def _first_percentiles(performance: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """The most representative timing metric for the distribution chart."""
    for name in ("page_load_ms", "open_site_ms", "enter_forum_ms", "create_post_ms"):
        stats = performance.get(name)
        if stats and stats.get("count"):
            return name, stats
    for name, stats in performance.items():
        if stats.get("count"):
            return name, stats
    return None


def _is_dry_run(payload: Any) -> bool:
    if isinstance(payload, dict):
        run = payload.get("test_run")
        if isinstance(run, dict):
            return bool(run.get("dry_run"))
    return False
