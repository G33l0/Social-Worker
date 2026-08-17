"""SQLAlchemy ORM models for Social Worker.

Every row is associated with a ``test_run_id`` (for example
``TEST-2026-08-17-0001``) so an operator can slice any metric, event or error
by the load test that produced it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    Boolean, DateTime as SADateTime, Float, ForeignKey, Index, Integer, JSON, String,
    Text, TypeDecorator, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.utils.time_utils import UTC, utc_now


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC timestamps that survive a SQLite round-trip.

    SQLite has no native timezone support, so values are stored naive (already
    normalised to UTC) and re-tagged as UTC on the way out.  This keeps every
    duration calculation in the reporting layer comparing like with like.
    """

    impl = SADateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):  # noqa: D102, ANN001
        if value is None:
            return None
        if value.tzinfo is None:
            return value
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value, dialect):  # noqa: D102, ANN001
        if value is None:
            return None
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


DateTime = UTCDateTime


class Base(DeclarativeBase):
    """Declarative base for all Social Worker tables."""

    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


def _now() -> datetime:
    return utc_now()


class TestRun(Base):
    """One execution of a load test plan."""

    __tablename__ = "test_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    test_run_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    label: Mapped[str] = mapped_column(String(255), default="")
    target_url: Mapped[str] = mapped_column(String(512), default="")
    environment: Mapped[str] = mapped_column(String(32), default="local")
    status: Mapped[str] = mapped_column(String(32), default="CREATED", index=True)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    authorized: Mapped[bool] = mapped_column(Boolean, default=False)
    authorization_reference: Mapped[str] = mapped_column(String(255), default="")
    scenario: Mapped[str] = mapped_column(String(64), default="")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    notes: Mapped[str] = mapped_column(Text, default="")

    sessions: Mapped[list["Session"]] = relationship(back_populates="test_run",
                                                     cascade="all, delete-orphan")


class Location(Base):
    """A configured geographic test location."""

    __tablename__ = "locations"
    __table_args__ = (UniqueConstraint("test_run_id", "key", name="uq_location_run_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    test_run_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    key: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    region_group: Mapped[str] = mapped_column(String(64), default="custom")
    country: Mapped[str] = mapped_column(String(64), default="")
    region: Mapped[str] = mapped_column(String(64), default="")
    city: Mapped[str] = mapped_column(String(64), default="")
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    locale: Mapped[str] = mapped_column(String(32), default="en-US")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    allocated_workers: Mapped[int] = mapped_column(Integer, default=0)
    max_concurrent_workers: Mapped[int] = mapped_column(Integer, default=0)
    runner: Mapped[str] = mapped_column(String(128), default="local")
    schedule: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Worker(Base):
    """A simulated visitor."""

    __tablename__ = "workers"
    __table_args__ = (UniqueConstraint("test_run_id", "worker_id", name="uq_worker_run_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    worker_id: Mapped[str] = mapped_column(String(32), index=True)
    test_run_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    location_key: Mapped[str] = mapped_column(String(64), index=True, default="")
    scenario: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(32), default="CREATED", index=True)
    current_action: Mapped[str] = mapped_column(String(64), default="")
    posting_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    replying_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    min_post_interval_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    max_post_interval_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    min_reply_interval_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    max_reply_interval_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    max_posts_per_session: Mapped[int] = mapped_column(Integer, default=0)
    max_replies_per_session: Mapped[int] = mapped_column(Integer, default=0)
    sessions_completed: Mapped[int] = mapped_column(Integer, default=0)
    posts_created: Mapped[int] = mapped_column(Integer, default=0)
    replies_created: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    last_heartbeat: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class Session(Base):
    """One visitor session executed by a worker."""

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    test_run_id: Mapped[str] = mapped_column(ForeignKey("test_runs.test_run_id"),
                                             index=True)
    worker_id: Mapped[str] = mapped_column(String(32), index=True)
    location_key: Mapped[str] = mapped_column(String(64), index=True, default="")
    scenario: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(32), default="RUNNING", index=True)
    username: Mapped[str] = mapped_column(String(128), default="")
    avatar: Mapped[str] = mapped_column(String(128), default="")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
    pages_visited: Mapped[int] = mapped_column(Integer, default=0)
    forums_visited: Mapped[int] = mapped_column(Integer, default=0)
    posts_created: Mapped[int] = mapped_column(Integer, default=0)
    replies_created: Mapped[int] = mapped_column(Integer, default=0)
    successful_actions: Mapped[int] = mapped_column(Integer, default=0)
    failed_actions: Mapped[int] = mapped_column(Integer, default=0)
    avg_response_ms: Mapped[float] = mapped_column(Float, default=0.0)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)

    test_run: Mapped["TestRun"] = relationship(back_populates="sessions")


class Forum(Base):
    """A forum discovered on (or configured for) the target site."""

    __tablename__ = "forums"
    __table_args__ = (UniqueConstraint("test_run_id", "key", name="uq_forum_run_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    test_run_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    key: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    url: Mapped[str] = mapped_column(String(512), default="")
    weight: Mapped[float] = mapped_column(Float, default=0.0)
    visits: Mapped[int] = mapped_column(Integer, default=0)
    posts: Mapped[int] = mapped_column(Integer, default=0)
    replies: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    avg_load_ms: Mapped[float] = mapped_column(Float, default=0.0)
    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ContentItem(Base):
    """A post from the operator's content library."""

    __tablename__ = "content"
    __table_args__ = (UniqueConstraint("library", "external_id", name="uq_content_lib_ext"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    library: Mapped[str] = mapped_column(String(32), default="posts", index=True)
    external_id: Mapped[str] = mapped_column(String(64), index=True)
    category: Mapped[str] = mapped_column(String(64), default="general", index=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    checksum: Mapped[str] = mapped_column(String(64), default="", index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    usage_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    source: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ReplyItem(Base):
    """A reply from the operator's reply library."""

    __tablename__ = "replies"
    __table_args__ = (UniqueConstraint("library", "external_id", name="uq_reply_lib_ext"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    library: Mapped[str] = mapped_column(String(32), default="replies", index=True)
    external_id: Mapped[str] = mapped_column(String(64), index=True)
    category: Mapped[str] = mapped_column(String(64), default="general", index=True)
    body: Mapped[str] = mapped_column(Text, default="")
    checksum: Mapped[str] = mapped_column(String(64), default="", index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    usage_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    source: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Execution(Base):
    """A single site interaction (navigate, post, reply, ...)."""

    __tablename__ = "executions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    test_run_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    session_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    worker_id: Mapped[str] = mapped_column(String(32), index=True, default="")
    location_key: Mapped[str] = mapped_column(String(64), index=True, default="")
    action: Mapped[str] = mapped_column(String(64), index=True, default="")
    forum_key: Mapped[str] = mapped_column(String(128), default="")
    target_id: Mapped[str] = mapped_column(String(128), default="")
    content_ref: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(32), default="OK", index=True)
    http_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
    url: Mapped[str] = mapped_column(String(512), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)


class Event(Base):
    """Telemetry emitted by every session step."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    test_run_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    session_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    worker_id: Mapped[str] = mapped_column(String(32), index=True, default="")
    location_key: Mapped[str] = mapped_column(String(64), index=True, default="")
    event_type: Mapped[str] = mapped_column(String(64), index=True, default="")
    action: Mapped[str] = mapped_column(String(64), default="")
    result: Mapped[str] = mapped_column(String(32), default="")
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)


class Metric(Base):
    """A single numeric measurement."""

    __tablename__ = "metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    test_run_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    session_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    worker_id: Mapped[str] = mapped_column(String(32), index=True, default="")
    location_key: Mapped[str] = mapped_column(String(64), index=True, default="")
    name: Mapped[str] = mapped_column(String(64), index=True, default="")
    value: Mapped[float] = mapped_column(Float, default=0.0)
    unit: Mapped[str] = mapped_column(String(16), default="ms")
    labels: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)


class ErrorRecord(Base):
    """A failed action, with debug artefact references."""

    __tablename__ = "errors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    test_run_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    session_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    worker_id: Mapped[str] = mapped_column(String(32), index=True, default="")
    location_key: Mapped[str] = mapped_column(String(64), index=True, default="")
    error_type: Mapped[str] = mapped_column(String(64), index=True, default="")
    action: Mapped[str] = mapped_column(String(64), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(String(512), default="")
    selector: Mapped[str] = mapped_column(String(255), default="")
    http_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    screenshot_path: Mapped[str] = mapped_column(String(512), default="")
    html_path: Mapped[str] = mapped_column(String(512), default="")
    traceback: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)


class SettingRecord(Base):
    """Key/value store for persisted runtime settings."""

    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


Index("ix_events_run_type", Event.test_run_id, Event.event_type)
Index("ix_metrics_run_name", Metric.test_run_id, Metric.name)
Index("ix_exec_run_action", Execution.test_run_id, Execution.action)

ALL_TABLES = (
    TestRun, Location, Worker, Session, Forum, ContentItem, ReplyItem,
    Execution, Event, Metric, ErrorRecord, SettingRecord,
)
