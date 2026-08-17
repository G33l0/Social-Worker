"""Worker profile and runtime state."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.utils.config import ForumSelectionMode
from app.utils.time_utils import iso, utc_now
from app.utils.validation import validate_worker_id


class WorkerStatus(str, Enum):
    """Lifecycle status shown in the live monitor."""

    CREATED = "CREATED"
    READY = "READY"
    STARTING = "STARTING"
    ACTIVE = "ACTIVE"
    WAITING = "WAITING"
    PAUSED = "PAUSED"
    BACKOFF = "BACKOFF"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    COMPLETED = "COMPLETED"
    ERROR = "ERROR"


class WorkerAction(str, Enum):
    """What the worker is doing right now."""

    IDLE = "IDLE"
    OPENING = "OPENING"
    IDENTITY = "IDENTITY"
    FORUM = "FORUM"
    READING = "READING"
    POSTING = "POSTING"
    REPLYING = "REPLYING"
    SLEEP = "SLEEP"
    ENDING = "ENDING"


class WorkerProfile(BaseModel):
    """The operator-configurable profile of one simulated visitor."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    worker_id: str
    location_key: str = ""
    scenario: str = "scenario_06"
    forum_mode: ForumSelectionMode = ForumSelectionMode.WEIGHTED
    forum: str = ""
    posting_enabled: bool = True
    replying_enabled: bool = True
    min_post_interval_seconds: float = Field(900.0, ge=0.0)
    max_post_interval_seconds: float = Field(2_400.0, ge=0.0)
    min_reply_interval_seconds: float = Field(600.0, ge=0.0)
    max_reply_interval_seconds: float = Field(1_800.0, ge=0.0)
    max_posts_per_session: int = Field(5, ge=0)
    max_replies_per_session: int = Field(10, ge=0)
    max_posts_per_day: int = Field(50, ge=0)
    max_replies_per_day: int = Field(100, ge=0)
    post_probability: float = Field(1.0, ge=0.0, le=1.0)
    reply_probability: float = Field(0.65, ge=0.0, le=1.0)
    sessions: int = Field(1, ge=1)
    content_category: str = ""
    reply_category: str = ""
    enabled: bool = True
    notes: str = ""

    @field_validator("worker_id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return validate_worker_id(value)

    @model_validator(mode="after")
    def _check_intervals(self) -> "WorkerProfile":
        if self.min_post_interval_seconds > self.max_post_interval_seconds:
            raise ValueError(f"{self.worker_id}: post interval minimum exceeds maximum")
        if self.min_reply_interval_seconds > self.max_reply_interval_seconds:
            raise ValueError(f"{self.worker_id}: reply interval minimum exceeds maximum")
        return self

    def describe(self) -> dict[str, Any]:
        """Profile card printed by the CLI (spec section 4)."""
        return {
            "Worker ID": self.worker_id,
            "Location": self.location_key or "-",
            "Scenario": self.scenario,
            "Avatar": "Obtained from website",
            "Username": "Obtained from website",
            "Forum": self.forum or self.forum_mode.value,
            "Posting": "Enabled" if self.posting_enabled else "Disabled",
            "Replying": "Enabled" if self.replying_enabled else "Disabled",
            "Post interval": f"{self.min_post_interval_seconds / 60:.0f}-"
                             f"{self.max_post_interval_seconds / 60:.0f} minutes",
            "Reply interval": f"{self.min_reply_interval_seconds / 60:.0f}-"
                              f"{self.max_reply_interval_seconds / 60:.0f} minutes",
            "Maximum posts": self.max_posts_per_session,
            "Maximum replies": self.max_replies_per_session,
        }


class WorkerRuntime:
    """Mutable per-worker runtime state (never persisted directly)."""

    def __init__(self, profile: WorkerProfile) -> None:
        self.profile = profile
        self.status = WorkerStatus.CREATED
        self.action = WorkerAction.IDLE
        self.current_session_id = ""
        self.current_forum = ""
        self.username = ""
        self.avatar = ""
        self.sessions_completed = 0
        self.sessions_failed = 0
        self.posts_created = 0
        self.replies_created = 0
        self.posts_today = 0
        self.replies_today = 0
        self.errors = 0
        self.consecutive_errors = 0
        self.rate_limit_hits = 0
        self.last_post_at: float = 0.0
        self.last_reply_at: float = 0.0
        self.started_at = ""
        self.last_heartbeat = iso()
        self.last_error = ""

    # ---------------------------------------------------------------- updates
    def set_status(self, status: WorkerStatus, action: WorkerAction | None = None) -> None:
        """Update status (and optionally the current action)."""
        self.status = status
        if action is not None:
            self.action = action
        self.beat()

    def set_action(self, action: WorkerAction) -> None:
        """Update the current action."""
        self.action = action
        self.beat()

    def beat(self) -> None:
        """Record a heartbeat timestamp."""
        self.last_heartbeat = iso()

    def note_error(self, message: str) -> None:
        """Record an error against this worker."""
        self.errors += 1
        self.consecutive_errors += 1
        self.last_error = message[:500]

    def note_success(self) -> None:
        """Reset the consecutive-error counter."""
        self.consecutive_errors = 0

    def reset_daily_counters(self) -> None:
        """Clear per-day quotas (called when a new UTC day starts)."""
        self.posts_today = 0
        self.replies_today = 0

    # ------------------------------------------------------------------ views
    @property
    def is_active(self) -> bool:
        """True while the worker holds a live session."""
        return self.status in {WorkerStatus.ACTIVE, WorkerStatus.STARTING,
                               WorkerStatus.WAITING}

    def can_post(self) -> bool:
        """True when posting quotas still allow a post.

        A daily maximum of 0 disables posting entirely for this worker.
        """
        if not self.profile.posting_enabled:
            return False
        return self.posts_today < self.profile.max_posts_per_day

    def can_reply(self) -> bool:
        """True when reply quotas still allow a reply.

        A daily maximum of 0 disables replying entirely for this worker.
        """
        if not self.profile.replying_enabled:
            return False
        return self.replies_today < self.profile.max_replies_per_day

    def to_row(self) -> dict[str, Any]:
        """Row for the live monitor table."""
        return {
            "worker_id": self.profile.worker_id,
            "location": self.profile.location_key,
            "status": self.status.value,
            "action": self.action.value,
            "sessions": self.sessions_completed,
            "posts": self.posts_created,
            "replies": self.replies_created,
            "errors": self.errors,
            "username": self.username,
            "avatar": self.avatar,
            "forum": self.current_forum,
            "last_heartbeat": self.last_heartbeat,
        }

    def snapshot(self) -> dict[str, Any]:
        """Full runtime snapshot (used by the controller API)."""
        data = self.to_row()
        data.update({
            "scenario": self.profile.scenario,
            "session_id": self.current_session_id,
            "posts_today": self.posts_today,
            "replies_today": self.replies_today,
            "consecutive_errors": self.consecutive_errors,
            "rate_limit_hits": self.rate_limit_hits,
            "last_error": self.last_error,
            "started_at": self.started_at,
            "updated_at": utc_now().isoformat(),
        })
        return data
