"""Acceptance thresholds: turn a finished run into a pass/fail verdict.

A load test that only reports numbers leaves the operator to eyeball whether
the run was acceptable.  These checks encode the acceptance criteria
(`p95 < 2s`, `error rate < 2%`, ...) so a run can gate a release, and so a
scheduled run can fail loudly in CI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.database.repository import Repository
from app.utils.config import ThresholdConfig
from app.utils.logger import get_logger

LOGGER = get_logger("thresholds")


@dataclass
class ThresholdResult:
    """One evaluated acceptance criterion."""

    name: str
    passed: bool
    observed: float
    limit: float
    comparison: str = "<="
    unit: str = ""
    detail: str = ""

    def to_row(self) -> dict[str, Any]:
        """Row for the report tables."""
        return {
            "check": self.name,
            "result": "PASS" if self.passed else "FAIL",
            "observed": round(self.observed, 2),
            "limit": round(self.limit, 2),
            "comparison": self.comparison,
            "unit": self.unit,
            "detail": self.detail,
        }


@dataclass
class Verdict:
    """The overall outcome of a test run."""

    test_run_id: str
    passed: bool = True
    checks: list[ThresholdResult] = field(default_factory=list)
    skipped: bool = False

    @property
    def failures(self) -> list[ThresholdResult]:
        """Only the criteria that failed."""
        return [check for check in self.checks if not check.passed]

    @property
    def label(self) -> str:
        """``PASS`` / ``FAIL`` / ``NOT EVALUATED``."""
        if self.skipped:
            return "NOT EVALUATED"
        return "PASS" if self.passed else "FAIL"

    def to_dict(self) -> dict[str, Any]:
        """Serialisable verdict for reports and the controller API."""
        return {
            "test_run_id": self.test_run_id,
            "verdict": self.label,
            "passed": self.passed,
            "checks": [check.to_row() for check in self.checks],
            "failed_checks": [check.name for check in self.failures],
        }

    def summary_line(self) -> str:
        """One-line summary for the console and logs."""
        if self.skipped:
            return "Acceptance thresholds are disabled for this run."
        if self.passed:
            return f"PASS - all {len(self.checks)} acceptance check(s) met."
        return (f"FAIL - {len(self.failures)} of {len(self.checks)} acceptance "
                f"check(s) breached: " + ", ".join(check.name for check in self.failures))


def evaluate(repository: Repository, test_run_id: str,
             config: ThresholdConfig) -> Verdict:
    """Evaluate *config* against the recorded results of *test_run_id*."""
    verdict = Verdict(test_run_id=test_run_id)
    if not config.enabled:
        verdict.skipped = True
        return verdict

    summary = repository.overall_summary(test_run_id)
    performance = repository.performance_summary(test_run_id)
    errors = repository.error_breakdown(test_run_id)

    total_actions = summary["successful_actions"] + summary["failed_actions"]
    error_rate = (summary["failed_actions"] / total_actions) if total_actions else 0.0

    def add(name: str, observed: float, limit: float, *, unit: str = "",
            comparison: str = "<=", detail: str = "") -> None:
        if limit <= 0 and comparison == "<=":
            return
        passed = observed <= limit if comparison == "<=" else observed >= limit
        verdict.checks.append(ThresholdResult(name=name, passed=passed,
                                              observed=observed, limit=limit,
                                              comparison=comparison, unit=unit,
                                              detail=detail))

    if config.max_error_rate > 0:
        add("error_rate", error_rate, config.max_error_rate, unit="fraction",
            detail=f"{summary['failed_actions']} failed of {total_actions} action(s)")
    add("avg_response_ms", summary["avg_response_ms"], config.max_avg_response_ms,
        unit="ms")

    navigation = _navigation_percentiles(performance)
    add("p95_response_ms", navigation.get("p95", 0.0), config.max_p95_response_ms,
        unit="ms", detail=f"{navigation.get('count', 0)} sample(s)")
    add("p99_response_ms", navigation.get("p99", 0.0), config.max_p99_response_ms,
        unit="ms")
    add("post_submit_p95_ms", performance.get("create_post_ms", {}).get("p95", 0.0),
        config.max_post_submit_p95_ms, unit="ms")
    add("reply_submit_p95_ms", performance.get("create_reply_ms", {}).get("p95", 0.0),
        config.max_reply_submit_p95_ms, unit="ms")

    rate_limits = sum(row["count"] for row in errors
                      if row["error_type"] == "RateLimited")
    verdict.checks.append(ThresholdResult(
        name="rate_limit_events", passed=rate_limits <= config.max_rate_limit_events,
        observed=float(rate_limits), limit=float(config.max_rate_limit_events),
        unit="events", detail="target throttled the test client" if rate_limits else ""))

    failed_sessions = _failed_sessions(repository, test_run_id)
    verdict.checks.append(ThresholdResult(
        name="failed_sessions", passed=failed_sessions <= config.max_failed_sessions,
        observed=float(failed_sessions), limit=float(config.max_failed_sessions),
        unit="sessions"))

    if config.min_completed_sessions > 0:
        add("completed_sessions", float(summary["sessions"]),
            float(config.min_completed_sessions), unit="sessions", comparison=">=")

    if config.fail_on_javascript_errors:
        js_errors = sum(row["count"] for row in errors
                        if "javascript" in row["error_type"].lower())
        verdict.checks.append(ThresholdResult(
            name="javascript_errors", passed=js_errors == 0, observed=float(js_errors),
            limit=0.0, unit="errors"))

    verdict.passed = all(check.passed for check in verdict.checks)
    LOGGER.info("Acceptance verdict for %s: %s", test_run_id, verdict.summary_line())
    return verdict


def _navigation_percentiles(performance: dict[str, Any]) -> dict[str, float]:
    """Percentiles for page-level response time, preferring real page loads."""
    for name in ("page_load_ms", "open_site_ms", "enter_forum_ms"):
        stats = performance.get(name)
        if stats and stats.get("count"):
            return stats
    return next(iter(performance.values()), {})


def _failed_sessions(repository: Repository, test_run_id: str) -> int:
    """Sessions that ended in any non-successful state."""
    rows = repository.sessions(test_run_id)
    return sum(1 for row in rows
               if row["status"] not in {"COMPLETED", "STOPPED", "CANCELLED"})
