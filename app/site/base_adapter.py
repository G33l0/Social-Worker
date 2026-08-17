"""Website adapter contract.

The rest of Social Worker talks to the target site exclusively through this
interface, so supporting another authorized site means writing one adapter --
no changes to the worker, scheduler or reporting code.

The adapter is also the single place where site-specific selectors live.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Sequence


@dataclass
class Avatar:
    """An avatar offered by the website."""

    index: int
    identifier: str = ""
    label: str = ""

    def describe(self) -> str:
        """Label used in dry-run output."""
        return self.label or self.identifier or f"Avatar {self.index + 1}"


@dataclass
class Username:
    """A username offered by the website."""

    index: int
    value: str = ""

    def describe(self) -> str:
        """Label used in dry-run output."""
        return self.value or f"Username {self.index + 1}"


@dataclass
class ForumRef:
    """A forum discovered on the website."""

    key: str
    name: str = ""
    url: str = ""
    index: int = 0

    def describe(self) -> str:
        return self.name or self.key


@dataclass
class PostRef:
    """An initial post visible in a forum."""

    post_id: str
    title: str = ""
    excerpt: str = ""
    url: str = ""
    index: int = 0
    created_at: str = ""

    def describe(self) -> str:
        return self.title or self.post_id


@dataclass
class ActionResult:
    """Uniform result type returned by every adapter action."""

    action: str
    ok: bool
    duration_ms: float = 0.0
    target_id: str = ""
    detail: str = ""
    error: str = ""
    http_status: int | None = None
    dry_run: bool = False
    selector: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        """True when the action did not succeed."""
        return not self.ok

    def to_dict(self) -> dict[str, Any]:
        """Serialisable view for telemetry."""
        return {
            "action": self.action,
            "ok": self.ok,
            "duration_ms": round(self.duration_ms, 2),
            "target_id": self.target_id,
            "detail": self.detail,
            "error": self.error,
            "http_status": self.http_status,
            "dry_run": self.dry_run,
            "selector": self.selector,
            "metadata": self.metadata,
        }


@dataclass
class SiteIdentity:
    """The identity the website handed to this visitor session."""

    avatar: Avatar | None = None
    username: Username | None = None

    @property
    def is_complete(self) -> bool:
        """True once both an avatar and a username have been selected."""
        return self.avatar is not None and self.username is not None

    def describe(self) -> str:
        avatar = self.avatar.describe() if self.avatar else "-"
        username = self.username.describe() if self.username else "-"
        return f"{username} / {avatar}"


class RateLimitedError(RuntimeError):
    """Raised when the target site signals throttling (HTTP 429 and friends).

    Social Worker treats this as a stop signal for the affected worker: the
    condition is recorded, the worker backs off, and no attempt is made to work
    around the limit.
    """

    def __init__(self, message: str = "Target site is rate limiting requests",
                 *, status: int | None = 429, retry_after: float = 0.0) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class AdapterError(RuntimeError):
    """Raised when the site does not expose an element the workflow needs."""

    def __init__(self, message: str, *, action: str = "", selector: str = "") -> None:
        super().__init__(message)
        self.action = action
        self.selector = selector


class BaseSiteAdapter(ABC):
    """Interface every website adapter implements."""

    name = "base"

    def __init__(self, *, dry_run: bool = False) -> None:
        self.dry_run = dry_run
        self.identity = SiteIdentity()
        self.current_forum: ForumRef | None = None

    # -------------------------------------------------------------- lifecycle
    @abstractmethod
    async def initialize_session(self) -> ActionResult:
        """Open the website and wait for it to be ready."""

    @abstractmethod
    async def end_session(self) -> ActionResult:
        """Leave the site and release any resources held by the adapter."""

    # --------------------------------------------------------------- identity
    @abstractmethod
    async def get_available_avatars(self) -> list[Avatar]:
        """Return the avatars the website offers this visitor."""

    @abstractmethod
    async def select_avatar(self, avatar: Avatar | None = None) -> ActionResult:
        """Select one of the website-provided avatars."""

    @abstractmethod
    async def get_available_usernames(self) -> list[Username]:
        """Return the usernames the website offers this visitor."""

    @abstractmethod
    async def select_username(self, username: Username | None = None) -> ActionResult:
        """Select one of the website-provided usernames."""

    # ----------------------------------------------------------------- forums
    @abstractmethod
    async def get_forums(self) -> list[ForumRef]:
        """Return the forums available on the site."""

    @abstractmethod
    async def enter_forum(self, forum: ForumRef) -> ActionResult:
        """Enter a forum and wait for its post list."""

    @abstractmethod
    async def browse_forum(self, depth: int = 1) -> ActionResult:
        """Scroll/paginate through the current forum like a reader would."""

    # ------------------------------------------------------------------ posts
    @abstractmethod
    async def get_initial_posts(self, limit: int = 50) -> list[PostRef]:
        """Return the initial (top-level) posts in the current forum."""

    @abstractmethod
    async def read_post(self, post: PostRef) -> ActionResult:
        """Open a post so its replies are visible."""

    @abstractmethod
    async def create_post(self, body: str, *, title: str = "") -> ActionResult:
        """Publish a new post (no-op when ``dry_run`` is set)."""

    @abstractmethod
    async def create_reply(self, post: PostRef, body: str) -> ActionResult:
        """Reply to *post* (no-op when ``dry_run`` is set)."""

    # ---------------------------------------------------------------- helpers
    def supports(self, capability: str) -> bool:
        """True when the adapter implements an optional capability."""
        return capability in getattr(self, "capabilities", ())

    def describe(self) -> dict[str, Any]:
        """Adapter state, used by dry-run reporting."""
        return {
            "adapter": self.name,
            "dry_run": self.dry_run,
            "identity": self.identity.describe(),
            "current_forum": self.current_forum.describe() if self.current_forum else "",
        }


def pick_index(items: Sequence[Any], strategy: str = "random",
               rng: Any = None) -> int:
    """Choose an index from *items* using ``random`` / ``first`` / ``last``."""
    if not items:
        return -1
    strategy = (strategy or "random").lower()
    if strategy == "first":
        return 0
    if strategy == "last":
        return len(items) - 1
    import random as _random

    generator = rng or _random
    return generator.randrange(len(items))
