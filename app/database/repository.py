"""Query helpers used by the reporting layer and the live dashboard."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from app.database.database import Database
from app.database.models import (
    ErrorRecord, Event, Execution, Forum, Location, Metric, Session, TestRun, Worker,
)


def _rows(result: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in result.mappings().all()]


class Repository:
    """Read-side access to a single test run."""

    def __init__(self, database: Database) -> None:
        self.database = database

    # ------------------------------------------------------------------ runs
    def list_test_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        def _query(session: OrmSession) -> list[dict[str, Any]]:
            stmt = select(TestRun).order_by(TestRun.started_at.desc()).limit(limit)
            return [
                {
                    "test_run_id": run.test_run_id,
                    "label": run.label,
                    "status": run.status,
                    "target_url": run.target_url,
                    "environment": run.environment,
                    "dry_run": run.dry_run,
                    "started_at": run.started_at.isoformat() if run.started_at else "",
                    "ended_at": run.ended_at.isoformat() if run.ended_at else "",
                    "duration_seconds": run.duration_seconds,
                }
                for run in session.scalars(stmt)
            ]

        return self.database.read(_query)

    def get_test_run(self, test_run_id: str) -> dict[str, Any] | None:
        def _query(session: OrmSession) -> dict[str, Any] | None:
            run = session.scalars(
                select(TestRun).where(TestRun.test_run_id == test_run_id)
            ).one_or_none()
            if run is None:
                return None
            return {
                "test_run_id": run.test_run_id,
                "label": run.label,
                "status": run.status,
                "target_url": run.target_url,
                "environment": run.environment,
                "scenario": run.scenario,
                "dry_run": run.dry_run,
                "authorized": run.authorized,
                "authorization_reference": run.authorization_reference,
                "started_at": run.started_at.isoformat() if run.started_at else "",
                "ended_at": run.ended_at.isoformat() if run.ended_at else "",
                "duration_seconds": run.duration_seconds,
                "config_snapshot": run.config_snapshot,
                "notes": run.notes,
            }

        return self.database.read(_query)

    def latest_test_run_id(self) -> str | None:
        runs = self.list_test_runs(limit=1)
        return runs[0]["test_run_id"] if runs else None

    # -------------------------------------------------------------- summaries
    def overall_summary(self, test_run_id: str) -> dict[str, Any]:
        def _query(session: OrmSession) -> dict[str, Any]:
            sessions = session.execute(
                select(
                    func.count(Session.id),
                    func.coalesce(func.sum(Session.posts_created), 0),
                    func.coalesce(func.sum(Session.replies_created), 0),
                    func.coalesce(func.sum(Session.pages_visited), 0),
                    func.coalesce(func.sum(Session.successful_actions), 0),
                    func.coalesce(func.sum(Session.failed_actions), 0),
                    func.coalesce(func.avg(Session.duration_ms), 0.0),
                ).where(Session.test_run_id == test_run_id)
            ).one()
            errors = session.execute(
                select(func.count(ErrorRecord.id)).where(
                    ErrorRecord.test_run_id == test_run_id)
            ).scalar_one()
            response = session.execute(
                select(func.coalesce(func.avg(Execution.duration_ms), 0.0),
                       func.count(Execution.id)).where(
                    Execution.test_run_id == test_run_id)
            ).one()
            workers = session.execute(
                select(func.count(Worker.id)).where(Worker.test_run_id == test_run_id)
            ).scalar_one()
            return {
                "test_run_id": test_run_id,
                "sessions": sessions[0],
                "posts": sessions[1],
                "replies": sessions[2],
                "pages_visited": sessions[3],
                "successful_actions": sessions[4],
                "failed_actions": sessions[5],
                "avg_session_duration_ms": round(float(sessions[6]), 2),
                "errors": errors,
                "avg_response_ms": round(float(response[0]), 2),
                "executions": response[1],
                "workers": workers,
            }

        return self.database.read(_query)

    def geographic_summary(self, test_run_id: str) -> list[dict[str, Any]]:
        def _query(session: OrmSession) -> list[dict[str, Any]]:
            session_stmt = (
                select(
                    Session.location_key,
                    func.count(Session.id).label("sessions"),
                    func.coalesce(func.sum(Session.posts_created), 0).label("posts"),
                    func.coalesce(func.sum(Session.replies_created), 0).label("replies"),
                    func.coalesce(func.avg(Session.duration_ms), 0.0).label("avg_session_ms"),
                )
                .where(Session.test_run_id == test_run_id)
                .group_by(Session.location_key)
            )
            per_location = {
                row.location_key: {
                    "location": row.location_key,
                    "sessions": row.sessions,
                    "posts": int(row.posts),
                    "replies": int(row.replies),
                    "avg_session_duration_ms": round(float(row.avg_session_ms), 2),
                }
                for row in session.execute(session_stmt)
            }

            response_stmt = (
                select(Execution.location_key,
                       func.coalesce(func.avg(Execution.duration_ms), 0.0),
                       func.count(Execution.id))
                .where(Execution.test_run_id == test_run_id)
                .group_by(Execution.location_key)
            )
            for key, avg_ms, count in session.execute(response_stmt):
                entry = per_location.setdefault(key, {
                    "location": key, "sessions": 0, "posts": 0, "replies": 0,
                    "avg_session_duration_ms": 0.0,
                })
                entry["avg_response_ms"] = round(float(avg_ms), 2)
                entry["requests"] = count

            error_stmt = (
                select(ErrorRecord.location_key, func.count(ErrorRecord.id))
                .where(ErrorRecord.test_run_id == test_run_id)
                .group_by(ErrorRecord.location_key)
            )
            for key, count in session.execute(error_stmt):
                entry = per_location.setdefault(key, {
                    "location": key, "sessions": 0, "posts": 0, "replies": 0,
                    "avg_session_duration_ms": 0.0,
                })
                entry["errors"] = count

            configured_stmt = select(Location).where(Location.test_run_id == test_run_id)
            for location in session.scalars(configured_stmt):
                entry = per_location.setdefault(location.key, {
                    "location": location.key, "sessions": 0, "posts": 0, "replies": 0,
                    "avg_session_duration_ms": 0.0,
                })
                entry.setdefault("errors", 0)
                entry["country"] = location.country
                entry["allocated_workers"] = location.allocated_workers
                entry["max_concurrent_workers"] = location.max_concurrent_workers

            for entry in per_location.values():
                entry.setdefault("errors", 0)
                entry.setdefault("avg_response_ms", 0.0)
                entry.setdefault("requests", 0)
                entry.setdefault("country", "")
                entry.setdefault("allocated_workers", 0)
                entry.setdefault("max_concurrent_workers", 0)

            return sorted(per_location.values(), key=lambda item: item["location"])

        return self.database.read(_query)

    def worker_summary(self, test_run_id: str) -> list[dict[str, Any]]:
        def _query(session: OrmSession) -> list[dict[str, Any]]:
            stmt = select(Worker).where(Worker.test_run_id == test_run_id).order_by(
                Worker.worker_id)
            return [
                {
                    "worker_id": worker.worker_id,
                    "location": worker.location_key,
                    "scenario": worker.scenario,
                    "status": worker.status,
                    "sessions_completed": worker.sessions_completed,
                    "posts": worker.posts_created,
                    "replies": worker.replies_created,
                    "errors": worker.errors,
                    "posting_enabled": worker.posting_enabled,
                    "replying_enabled": worker.replying_enabled,
                    "last_heartbeat": worker.last_heartbeat.isoformat()
                    if worker.last_heartbeat else "",
                }
                for worker in session.scalars(stmt)
            ]

        return self.database.read(_query)

    def forum_summary(self, test_run_id: str) -> list[dict[str, Any]]:
        def _query(session: OrmSession) -> list[dict[str, Any]]:
            stmt = select(Forum).where(Forum.test_run_id == test_run_id).order_by(Forum.key)
            return [
                {
                    "forum": forum.key,
                    "name": forum.name,
                    "url": forum.url,
                    "weight": forum.weight,
                    "visits": forum.visits,
                    "posts": forum.posts,
                    "replies": forum.replies,
                    "errors": forum.errors,
                    "avg_load_ms": round(forum.avg_load_ms, 2),
                }
                for forum in session.scalars(stmt)
            ]

        return self.database.read(_query)

    def executions(self, test_run_id: str, action: str | None = None,
                   limit: int = 5000) -> list[dict[str, Any]]:
        def _query(session: OrmSession) -> list[dict[str, Any]]:
            stmt = select(Execution).where(Execution.test_run_id == test_run_id)
            if action:
                stmt = stmt.where(Execution.action == action)
            stmt = stmt.order_by(Execution.created_at).limit(limit)
            return [
                {
                    "created_at": row.created_at.isoformat() if row.created_at else "",
                    "worker_id": row.worker_id,
                    "location": row.location_key,
                    "session_id": row.session_id,
                    "action": row.action,
                    "forum": row.forum_key,
                    "target_id": row.target_id,
                    "content_ref": row.content_ref,
                    "status": row.status,
                    "http_status": row.http_status,
                    "duration_ms": round(row.duration_ms, 2),
                    "url": row.url,
                    "dry_run": row.dry_run,
                    "detail": row.detail,
                }
                for row in session.scalars(stmt)
            ]

        return self.database.read(_query)

    def sessions(self, test_run_id: str, limit: int = 5000) -> list[dict[str, Any]]:
        def _query(session: OrmSession) -> list[dict[str, Any]]:
            stmt = (select(Session).where(Session.test_run_id == test_run_id)
                    .order_by(Session.started_at).limit(limit))
            return [
                {
                    "session_id": row.session_id,
                    "worker_id": row.worker_id,
                    "location": row.location_key,
                    "scenario": row.scenario,
                    "status": row.status,
                    "username": row.username,
                    "avatar": row.avatar,
                    "started_at": row.started_at.isoformat() if row.started_at else "",
                    "ended_at": row.ended_at.isoformat() if row.ended_at else "",
                    "duration_ms": round(row.duration_ms, 2),
                    "pages_visited": row.pages_visited,
                    "forums_visited": row.forums_visited,
                    "posts_created": row.posts_created,
                    "replies_created": row.replies_created,
                    "successful_actions": row.successful_actions,
                    "failed_actions": row.failed_actions,
                    "avg_response_ms": round(row.avg_response_ms, 2),
                    "dry_run": row.dry_run,
                }
                for row in session.scalars(stmt)
            ]

        return self.database.read(_query)

    def errors(self, test_run_id: str, limit: int = 2000) -> list[dict[str, Any]]:
        def _query(session: OrmSession) -> list[dict[str, Any]]:
            stmt = (select(ErrorRecord).where(ErrorRecord.test_run_id == test_run_id)
                    .order_by(ErrorRecord.created_at).limit(limit))
            return [
                {
                    "created_at": row.created_at.isoformat() if row.created_at else "",
                    "worker_id": row.worker_id,
                    "location": row.location_key,
                    "session_id": row.session_id,
                    "error_type": row.error_type,
                    "action": row.action,
                    "message": row.message,
                    "url": row.url,
                    "selector": row.selector,
                    "http_status": row.http_status,
                    "screenshot_path": row.screenshot_path,
                    "html_path": row.html_path,
                }
                for row in session.scalars(stmt)
            ]

        return self.database.read(_query)

    def events(self, test_run_id: str, limit: int = 10_000) -> list[dict[str, Any]]:
        def _query(session: OrmSession) -> list[dict[str, Any]]:
            stmt = (select(Event).where(Event.test_run_id == test_run_id)
                    .order_by(Event.created_at).limit(limit))
            return [
                {
                    "created_at": row.created_at.isoformat() if row.created_at else "",
                    "event_type": row.event_type,
                    "worker_id": row.worker_id,
                    "location": row.location_key,
                    "session_id": row.session_id,
                    "action": row.action,
                    "result": row.result,
                    "duration_ms": round(row.duration_ms, 2),
                    "payload": row.payload,
                }
                for row in session.scalars(stmt)
            ]

        return self.database.read(_query)

    def performance_summary(self, test_run_id: str) -> dict[str, Any]:
        """Percentile view of every recorded duration metric."""
        def _query(session: OrmSession) -> dict[str, Any]:
            stmt = select(Metric.name, Metric.value).where(
                Metric.test_run_id == test_run_id)
            buckets: dict[str, list[float]] = {}
            for name, value in session.execute(stmt):
                buckets.setdefault(name, []).append(float(value))
            return {name: _percentiles(values) for name, values in sorted(buckets.items())}

        return self.database.read(_query)

    def error_breakdown(self, test_run_id: str) -> list[dict[str, Any]]:
        def _query(session: OrmSession) -> list[dict[str, Any]]:
            stmt = (select(ErrorRecord.error_type, func.count(ErrorRecord.id))
                    .where(ErrorRecord.test_run_id == test_run_id)
                    .group_by(ErrorRecord.error_type)
                    .order_by(func.count(ErrorRecord.id).desc()))
            return [{"error_type": name, "count": count}
                    for name, count in session.execute(stmt)]

        return self.database.read(_query)

    def content_usage(self, test_run_id: str, action: str) -> list[dict[str, Any]]:
        def _query(session: OrmSession) -> list[dict[str, Any]]:
            stmt = (select(Execution.content_ref, func.count(Execution.id))
                    .where(Execution.test_run_id == test_run_id,
                           Execution.action == action,
                           Execution.content_ref != "")
                    .group_by(Execution.content_ref)
                    .order_by(func.count(Execution.id).desc()))
            return [{"content_ref": ref, "uses": count}
                    for ref, count in session.execute(stmt)]

        return self.database.read(_query)


def _percentiles(values: list[float]) -> dict[str, float]:
    """Return count/min/avg/p50/p90/p95/p99/max for *values*."""
    if not values:
        return {"count": 0, "min": 0.0, "avg": 0.0, "p50": 0.0, "p90": 0.0,
                "p95": 0.0, "p99": 0.0, "max": 0.0}
    ordered = sorted(values)
    size = len(ordered)

    def pct(fraction: float) -> float:
        index = min(size - 1, max(0, int(round(fraction * (size - 1)))))
        return round(ordered[index], 2)

    return {
        "count": size,
        "min": round(ordered[0], 2),
        "avg": round(sum(ordered) / size, 2),
        "p50": pct(0.50),
        "p90": pct(0.90),
        "p95": pct(0.95),
        "p99": pct(0.99),
        "max": round(ordered[-1], 2),
    }
