"""Compare two test runs to detect performance regressions.

One run tells you how the site behaved today.  Two runs tell you whether a
change made it worse — which is the question a load test is usually asked in a
release pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.database.repository import Repository
from app.utils.logger import get_logger

LOGGER = get_logger("reporting.comparison")

#: Metrics where a higher number is worse.
LOWER_IS_BETTER = ("avg_response_ms", "p95_response_ms", "p99_response_ms",
                   "error_rate", "errors", "failed_actions")


@dataclass
class MetricDelta:
    """One metric compared across two runs."""

    metric: str
    baseline: float
    candidate: float
    unit: str = ""
    lower_is_better: bool = True
    tolerance: float = 0.10

    @property
    def delta(self) -> float:
        """Absolute change from baseline to candidate."""
        return round(self.candidate - self.baseline, 3)

    @property
    def change_ratio(self) -> float:
        """Relative change; 0.25 means 25% worse than the baseline."""
        if self.baseline == 0:
            return 0.0 if self.candidate == 0 else 1.0
        return round((self.candidate - self.baseline) / abs(self.baseline), 4)

    @property
    def regressed(self) -> bool:
        """True when the candidate is worse than the baseline beyond tolerance."""
        if self.lower_is_better:
            return self.change_ratio > self.tolerance
        return self.change_ratio < -self.tolerance

    @property
    def improved(self) -> bool:
        """True when the candidate is better beyond tolerance."""
        if self.lower_is_better:
            return self.change_ratio < -self.tolerance
        return self.change_ratio > self.tolerance

    def to_row(self) -> dict[str, Any]:
        """Row for the comparison table."""
        if self.regressed:
            verdict = "REGRESSED"
        elif self.improved:
            verdict = "IMPROVED"
        else:
            verdict = "UNCHANGED"
        return {
            "metric": self.metric,
            "baseline": round(self.baseline, 2),
            "candidate": round(self.candidate, 2),
            "delta": self.delta,
            "change": f"{self.change_ratio * 100:+.1f}%",
            "verdict": verdict,
            "unit": self.unit,
        }


@dataclass
class Comparison:
    """The result of comparing a candidate run against a baseline."""

    baseline_id: str
    candidate_id: str
    metrics: list[MetricDelta] = field(default_factory=list)
    tolerance: float = 0.10

    @property
    def regressions(self) -> list[MetricDelta]:
        """Metrics that got worse beyond tolerance."""
        return [metric for metric in self.metrics if metric.regressed]

    @property
    def passed(self) -> bool:
        """True when nothing regressed."""
        return not self.regressions

    def to_dict(self) -> dict[str, Any]:
        """Serialisable comparison for reports and the API."""
        return {
            "baseline": self.baseline_id,
            "candidate": self.candidate_id,
            "tolerance": self.tolerance,
            "verdict": "PASS" if self.passed else "REGRESSED",
            "metrics": [metric.to_row() for metric in self.metrics],
            "regressions": [metric.metric for metric in self.regressions],
        }

    def summary_line(self) -> str:
        """One-line summary for the console."""
        if self.passed:
            return (f"No regression: {self.candidate_id} is within "
                    f"{self.tolerance:.0%} of {self.baseline_id} on every metric.")
        names = ", ".join(metric.metric for metric in self.regressions)
        return (f"REGRESSED against {self.baseline_id}: {names} "
                f"(tolerance {self.tolerance:.0%}).")


def compare(repository: Repository, baseline_id: str, candidate_id: str, *,
            tolerance: float = 0.10) -> Comparison:
    """Compare *candidate_id* against *baseline_id*."""
    baseline = _snapshot(repository, baseline_id)
    candidate = _snapshot(repository, candidate_id)

    comparison = Comparison(baseline_id=baseline_id, candidate_id=candidate_id,
                            tolerance=tolerance)
    for metric, unit in (("avg_response_ms", "ms"), ("p95_response_ms", "ms"),
                         ("p99_response_ms", "ms"), ("error_rate", "fraction"),
                         ("sessions", "count"), ("posts", "count"),
                         ("replies", "count"), ("throughput_per_minute", "req/min")):
        comparison.metrics.append(MetricDelta(
            metric=metric,
            baseline=float(baseline.get(metric, 0.0)),
            candidate=float(candidate.get(metric, 0.0)),
            unit=unit,
            lower_is_better=metric in LOWER_IS_BETTER,
            tolerance=tolerance,
        ))
    LOGGER.info("Comparison %s -> %s: %s", baseline_id, candidate_id,
                comparison.summary_line())
    return comparison


def _snapshot(repository: Repository, test_run_id: str) -> dict[str, float]:
    """Flatten a run into the handful of numbers worth comparing."""
    summary = repository.overall_summary(test_run_id)
    performance = repository.performance_summary(test_run_id)
    run = repository.get_test_run(test_run_id) or {}

    page = {}
    for name in ("page_load_ms", "open_site_ms", "enter_forum_ms"):
        if performance.get(name, {}).get("count"):
            page = performance[name]
            break

    total_actions = summary["successful_actions"] + summary["failed_actions"]
    duration_minutes = max(float(run.get("duration_seconds", 0.0)) / 60.0, 1 / 60)
    return {
        "avg_response_ms": summary["avg_response_ms"],
        "p95_response_ms": page.get("p95", 0.0),
        "p99_response_ms": page.get("p99", 0.0),
        "error_rate": (summary["failed_actions"] / total_actions) if total_actions else 0.0,
        "sessions": summary["sessions"],
        "posts": summary["posts"],
        "replies": summary["replies"],
        "throughput_per_minute": round(summary["executions"] / duration_minutes, 2),
    }
