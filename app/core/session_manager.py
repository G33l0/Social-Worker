"""Visitor-session bookkeeping and persistence."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.database.database import Database
from app.database.models import Session as SessionRow
from app.utils.ids import new_session_id
from app.utils.logger import get_logger
from app.utils.time_utils import duration_ms, utc_now

LOGGER = get_logger("sessions")


@dataclass
class SessionRecord:
    """In-memory counters for one visitor session."""

    session_id: str
    test_run_id: str
    worker_id: str
    location_key: str
    scenario: str
    dry_run: bool = False
    status: str = "RUNNING"
    username: str = ""
    avatar: str = ""
    started_at: Any = field(default_factory=utc_now)
    ended_at: Any = None
    pages_visited: int = 0
    forums_visited: int = 0
    posts_created: int = 0
    replies_created: int = 0
    successful_actions: int = 0
    failed_actions: int = 0
    response_ms_total: float = 0.0
    response_samples: int = 0

    @property
    def duration_ms(self) -> float:
        """Elapsed session time in milliseconds."""
        return duration_ms(self.started_at, self.ended_at)

    @property
    def avg_response_ms(self) -> float:
        """Mean response time observed during this session."""
        return round(self.response_ms_total / self.response_samples, 2) \
            if self.response_samples else 0.0

    def record_action(self, *, ok: bool, duration: float = 0.0) -> None:
        """Count one site interaction."""
        if ok:
            self.successful_actions += 1
        else:
            self.failed_actions += 1
        if duration:
            self.response_ms_total += duration
            self.response_samples += 1

    def to_dict(self) -> dict[str, Any]:
        """Serialisable summary returned to the scheduler."""
        return {
            "session_id": self.session_id,
            "worker_id": self.worker_id,
            "location": self.location_key,
            "scenario": self.scenario,
            "status": self.status,
            "username": self.username,
            "avatar": self.avatar,
            "duration_ms": round(self.duration_ms, 2),
            "pages_visited": self.pages_visited,
            "forums_visited": self.forums_visited,
            "posts_created": self.posts_created,
            "replies_created": self.replies_created,
            "successful_actions": self.successful_actions,
            "failed_actions": self.failed_actions,
            "avg_response_ms": self.avg_response_ms,
            "dry_run": self.dry_run,
        }


class SessionManager:
    """Creates, tracks and persists visitor sessions."""

    def __init__(self, database: Database, test_run_id: str) -> None:
        self.database = database
        self.test_run_id = test_run_id
        self.active: dict[str, SessionRecord] = {}
        self.completed: int = 0

    async def start(self, *, worker_id: str, location_key: str, scenario: str,
                    session_id: str = "", dry_run: bool = False) -> SessionRecord:
        """Open a session row and return its in-memory record."""
        record = SessionRecord(
            session_id=session_id or new_session_id(),
            test_run_id=self.test_run_id,
            worker_id=worker_id,
            location_key=location_key,
            scenario=scenario,
            dry_run=dry_run,
        )
        self.active[record.session_id] = record

        def _write(session: Any) -> None:
            session.add(SessionRow(
                session_id=record.session_id,
                test_run_id=record.test_run_id,
                worker_id=record.worker_id,
                location_key=record.location_key,
                scenario=record.scenario,
                status=record.status,
                started_at=record.started_at,
                dry_run=record.dry_run,
            ))

        await self.database.run(_write)
        LOGGER.info("Session %s started", record.session_id,
                    extra={"worker_id": worker_id, "location": location_key})
        return record

    async def finish(self, record: SessionRecord, *, status: str = "COMPLETED") -> None:
        """Close a session and persist its final counters."""
        record.ended_at = utc_now()
        record.status = status
        self.active.pop(record.session_id, None)
        self.completed += 1

        def _write(session: Any) -> None:
            row = session.query(SessionRow).filter_by(
                session_id=record.session_id).one_or_none()
            if row is None:  # pragma: no cover - defensive
                return
            row.status = record.status
            row.username = record.username
            row.avatar = record.avatar
            row.ended_at = record.ended_at
            row.duration_ms = record.duration_ms
            row.pages_visited = record.pages_visited
            row.forums_visited = record.forums_visited
            row.posts_created = record.posts_created
            row.replies_created = record.replies_created
            row.successful_actions = record.successful_actions
            row.failed_actions = record.failed_actions
            row.avg_response_ms = record.avg_response_ms

        await self.database.run(_write)
        LOGGER.info("Session %s finished (%s) in %.0fms", record.session_id,
                    status, record.duration_ms,
                    extra={"worker_id": record.worker_id,
                           "location": record.location_key,
                           "result": status,
                           "duration_ms": record.duration_ms})

    def snapshot(self) -> dict[str, Any]:
        """Live session counters."""
        return {
            "active": len(self.active),
            "completed": self.completed,
            "sessions": [record.to_dict() for record in self.active.values()],
        }
