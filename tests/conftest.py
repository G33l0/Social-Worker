"""Shared pytest fixtures.

The fake site adapter lets the whole worker/scheduler/reporting stack be tested
without launching a browser, which keeps the unit suite fast and deterministic.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any, Sequence

import pytest

from app.content.post_library import PostLibrary
from app.content.reply_library import ReplyLibrary
from app.core.content_manager import ContentManager
from app.core.forum_manager import ForumSelector
from app.core.metrics_manager import MetricsManager
from app.core.session_manager import SessionManager
from app.database.database import Database
from app.database.migrations import upgrade
from app.locations.location import Location
from app.site.base_adapter import (
    ActionResult, Avatar, BaseSiteAdapter, ForumRef, PostRef, RateLimitedError, Username,
)
from app.utils.config import ReplyTargetMode, Settings


class FakeSiteAdapter(BaseSiteAdapter):
    """In-memory stand-in for a website under test."""

    name = "fake"

    def __init__(self, *, dry_run: bool = False, forums: Sequence[str] = (),
                 initial_posts: int = 3, fail_on: Sequence[str] = (),
                 rate_limit_on: Sequence[str] = (), rng: random.Random | None = None,
                 store: dict[str, Any] | None = None) -> None:
        super().__init__(dry_run=dry_run)
        self._rng = rng or random.Random(1234)
        self.forum_keys = list(forums or ["general", "confessions", "questions", "random"])
        self.fail_on = set(fail_on)
        self.rate_limit_on = set(rate_limit_on)
        self.store = store if store is not None else {"posts": [], "replies": []}
        self.calls: list[str] = []
        self._posts: dict[str, list[PostRef]] = {
            key: [PostRef(post_id=f"{key}-{index}", title=f"{key} post {index}",
                          index=index, created_at=f"2026-01-0{index + 1}T00:00:00")
                  for index in range(initial_posts)]
            for key in self.forum_keys
        }
        self.current_post: PostRef | None = None

    def _guard(self, action: str) -> None:
        self.calls.append(action)
        if action in self.rate_limit_on:
            raise RateLimitedError(f"{action} throttled", status=429)

    def _result(self, action: str, *, target: str = "", detail: str = "",
                metadata: dict[str, Any] | None = None) -> ActionResult:
        if action in self.fail_on:
            return ActionResult(action=action, ok=False, duration_ms=1.0,
                                error=f"{action} failed (simulated)")
        return ActionResult(action=action, ok=True, duration_ms=1.0, target_id=target,
                            detail=detail, dry_run=self.dry_run,
                            metadata=metadata or {})

    async def initialize_session(self) -> ActionResult:
        self._guard("OPEN_SITE")
        return self._result("OPEN_SITE", metadata={"page_load_ms": 12.0})

    async def wait_for_ready(self) -> ActionResult:
        return self._result("WAIT_FOR_LOAD")

    async def end_session(self) -> ActionResult:
        return self._result("END_SESSION")

    async def get_available_avatars(self) -> list[Avatar]:
        self._guard("GET_AVATARS")
        return [Avatar(index=index, identifier=f"avatar-{index + 1}",
                       label=f"Avatar {index + 1}") for index in range(6)]

    async def select_avatar(self, avatar: Avatar | None = None) -> ActionResult:
        self._guard("SELECT_AVATAR")
        chosen = avatar or Avatar(index=2, identifier="avatar-3", label="Avatar 3")
        self.identity.avatar = chosen
        return self._result("SELECT_AVATAR", target=chosen.identifier,
                            detail=chosen.label, metadata={"index": chosen.index})

    async def get_available_usernames(self) -> list[Username]:
        self._guard("GET_USERNAMES")
        return [Username(index=index, value=f"Anonymous_{index + 10}")
                for index in range(5)]

    async def select_username(self, username: Username | None = None) -> ActionResult:
        self._guard("SELECT_USERNAME")
        chosen = username or Username(index=0, value="Anonymous_10")
        self.identity.username = chosen
        return self._result("SELECT_USERNAME", target=chosen.value,
                            detail=chosen.value, metadata={"index": chosen.index})

    async def get_forums(self) -> list[ForumRef]:
        self._guard("GET_FORUMS")
        return [ForumRef(key=key, name=key.title(), url=f"/forum/{key}", index=index)
                for index, key in enumerate(self.forum_keys)]

    async def enter_forum(self, forum: ForumRef) -> ActionResult:
        self._guard("ENTER_FORUM")
        self.current_forum = forum
        self.current_post = None
        return self._result("ENTER_FORUM", target=forum.key, detail=forum.name)

    async def browse_forum(self, depth: int = 1) -> ActionResult:
        self._guard("BROWSE_FORUM")
        return self._result("BROWSE_FORUM", detail=f"{depth} scroll(s)")

    async def return_to_forum_list(self) -> ActionResult:
        self.current_post = None
        return self._result("SWITCH_FORUM")

    async def get_initial_posts(self, limit: int = 50) -> list[PostRef]:
        self._guard("GET_POSTS")
        key = self.current_forum.key if self.current_forum else self.forum_keys[0]
        return list(self._posts.get(key, []))[:limit]

    async def read_post(self, post: PostRef) -> ActionResult:
        self._guard("READ_POST")
        self.current_post = post
        return self._result("READ_POST", target=post.post_id, detail=post.title)

    async def create_post(self, body: str, *, title: str = "") -> ActionResult:
        self._guard("CREATE_POST")
        if not self.dry_run and "CREATE_POST" not in self.fail_on:
            key = self.current_forum.key if self.current_forum else self.forum_keys[0]
            post = PostRef(post_id=f"{key}-new-{len(self.store['posts'])}", title=title,
                           excerpt=body[:80], index=len(self._posts[key]))
            self._posts[key].append(post)
            self.store["posts"].append({"forum": key, "body": body, "title": title})
        return self._result("CREATE_POST", detail=f"{len(body)} chars")

    async def create_reply(self, post: PostRef, body: str) -> ActionResult:
        self._guard("CREATE_REPLY")
        if not self.dry_run and "CREATE_REPLY" not in self.fail_on:
            self.store["replies"].append({"post_id": post.post_id, "body": body})
        return self._result("CREATE_REPLY", target=post.post_id,
                            detail=f"{len(body)} chars")

    def select_reply_target(self, posts: Sequence[PostRef], *,
                            mode: ReplyTargetMode = ReplyTargetMode.RANDOM,
                            keywords: Sequence[str] = (),
                            specific_post_id: str = "") -> PostRef | None:
        if not posts:
            return None
        if mode is ReplyTargetMode.OLDEST:
            return min(posts, key=lambda post: post.created_at)
        if mode is ReplyTargetMode.NEWEST:
            return max(posts, key=lambda post: post.created_at)
        return self._rng.choice(list(posts))


@pytest.fixture
def settings(tmp_path) -> Settings:
    """Fast, deterministic settings for tests."""
    config = Settings()
    config.website.url = "http://127.0.0.1:8099"
    config.website.authorized_test_mode = True
    config.website.authorization_reference = "TEST-SUITE"
    config.database.url = f"sqlite:///{tmp_path / 'test.db'}"
    config.logging.directory = str(tmp_path / "logs")
    config.logging.console = False
    config.reporting.output_directory = str(tmp_path / "reports")
    config.reporting.debug_directory = str(tmp_path / "debug")
    config.reporting.screenshot_directory = str(tmp_path / "screens")
    config.reporting.auto_export_on_stop = False
    config.content.posts_path = str(tmp_path / "posts.txt")
    config.content.replies_path = str(tmp_path / "replies.txt")
    config.concurrency.ramp_up_seconds = 0.0
    config.concurrency.max_global_workers = 4
    config.session.min_think_time_seconds = 0.0
    config.session.max_think_time_seconds = 0.0
    config.session.min_duration_seconds = 30.0
    config.session.max_duration_seconds = 60.0
    config.posting.min_interval_seconds = 0.0
    config.posting.max_interval_seconds = 0.0
    config.posting.probability = 1.0
    config.replies.min_interval_seconds = 0.0
    config.replies.max_interval_seconds = 0.0
    config.replies.probability = 1.0
    config.browser.screenshot_on_failure = False
    return config


@pytest.fixture
def database(settings) -> Database:
    """An initialised database bound to the temporary settings."""
    database = Database(settings.database)
    upgrade(database)
    yield database
    database.dispose()


@pytest.fixture
def content_manager(settings) -> ContentManager:
    """Content manager pre-loaded with a small library."""
    manager = ContentManager(settings.content, settings.posting, settings.replies)
    manager.post_library = PostLibrary()
    manager.reply_library = ReplyLibrary()
    manager.post_library.add_many([f"Test post {index:03d}" for index in range(1, 21)])
    manager.reply_library.add_many([f"Test reply {index:03d}" for index in range(1, 21)])
    manager.build_rotators()
    return manager


@pytest.fixture
def location() -> Location:
    """A single test location."""
    return Location(key="canada", name="Canada", country="Canada",
                    region_group="north_america", workers=2, max_concurrent_workers=2)


@pytest.fixture
def test_run_id(database) -> str:
    """Insert a test-run row so foreign keys resolve, and return its id."""
    from app.database.models import TestRun

    run_id = "TEST-RUN-0001"

    def _write(session):
        session.add(TestRun(test_run_id=run_id, label="fixture", status="RUNNING",
                            target_url="http://127.0.0.1:8099", environment="local"))

    database.write(_write)
    return run_id


@pytest.fixture
def metrics(database, test_run_id) -> MetricsManager:
    """Metrics manager bound to the fixture test run."""
    return MetricsManager(database, test_run_id, flush_interval=0.05)


@pytest.fixture
def session_manager(database, test_run_id) -> SessionManager:
    """Session manager bound to the fixture test run."""
    return SessionManager(database, test_run_id)


@pytest.fixture
def forum_selector(settings) -> ForumSelector:
    """Forum selector built from the test settings."""
    return ForumSelector(settings.forums, rng=random.Random(7))


@pytest.fixture
def event_loop_policy():
    """Use the default policy (kept explicit for pytest-asyncio)."""
    return asyncio.DefaultEventLoopPolicy()
