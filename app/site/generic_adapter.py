"""Generic adapter for anonymous/temporary-identity forum sites.

Works against any site whose markup is described in ``config/selectors.yaml``.
The mock site shipped in ``mock_site/`` matches the default selectors, so the
whole platform can be exercised locally before pointing it at the operator's
own website.
"""

from __future__ import annotations

import random
from typing import Sequence

from app.browser.interaction import Interaction
from app.browser.navigator import Navigator
from app.browser.selectors import SelectorSet
from app.browser.session import BrowserSession
from app.site.avatar_manager import AvatarManager
from app.site.base_adapter import (
    ActionResult, AdapterError, Avatar, BaseSiteAdapter, ForumRef, PostRef,
    RateLimitedError, SiteIdentity, Username,
)
from app.site.forum_manager import SiteForumManager
from app.site.post_manager import SitePostManager
from app.site.reply_manager import SiteReplyManager
from app.site.username_manager import UsernameManager
from app.utils.config import ReplyTargetMode
from app.utils.logger import get_logger
from app.utils.time_utils import duration_ms, utc_now

LOGGER = get_logger("site.adapter")


class GenericForumAdapter(BaseSiteAdapter):
    """Playwright-driven adapter for the site under authorized test."""

    name = "generic"
    capabilities = ("avatars", "usernames", "forums", "posts", "replies")

    def __init__(self, session: BrowserSession, base_url: str, *,
                 selectors: SelectorSet | None = None, dry_run: bool = False,
                 rate_limit_status_codes: Sequence[int] = (429, 503),
                 typing_delay_ms: tuple[int, int] = (5, 25),
                 human_typing: bool = True,
                 rng: random.Random | None = None) -> None:
        super().__init__(dry_run=dry_run)
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.selectors = selectors or SelectorSet()
        self.rate_limit_status_codes = tuple(rate_limit_status_codes)
        self._rng = rng or random.Random()

        self.navigator = Navigator(session, base_url)
        self.interaction = Interaction(session, typing_delay_ms=typing_delay_ms,
                                       human_typing=human_typing)
        self.avatars = AvatarManager(self.interaction, self.selectors, rng=self._rng)
        self.usernames = UsernameManager(self.interaction, self.selectors, rng=self._rng)
        self.forums = SiteForumManager(self.navigator, self.interaction, self.selectors)
        self.posts = SitePostManager(self.navigator, self.interaction, self.selectors,
                                     dry_run=dry_run)
        self.replies = SiteReplyManager(self.navigator, self.interaction, self.selectors,
                                        dry_run=dry_run, rng=self._rng)
        self.current_post: PostRef | None = None

    # -------------------------------------------------------------- lifecycle
    async def initialize_session(self) -> ActionResult:
        """Open the site's landing page and wait for the identity chooser."""
        started = utc_now()
        navigation = await self.navigator.goto("")
        self._raise_if_rate_limited(navigation.status)
        if not navigation.ok:
            return ActionResult(action="OPEN_SITE", ok=False,
                                duration_ms=navigation.duration_ms,
                                http_status=navigation.status,
                                error=navigation.error or "landing page failed to load",
                                metadata=navigation.timing)
        ready = await self.navigator.wait_for_any(
            self.selectors.candidates("identity.avatar_options")
            + self.selectors.candidates("forums.forum_links"),
            timeout_ms=10_000)
        elapsed = round(duration_ms(started), 2)
        return ActionResult(
            action="OPEN_SITE", ok=True, duration_ms=elapsed,
            http_status=navigation.status,
            detail=self.navigator.current_url,
            selector=ready,
            metadata={"page_load_ms": navigation.duration_ms, **navigation.timing},
        )

    async def wait_for_ready(self) -> ActionResult:
        """Wait for the page's network activity to settle."""
        started = utc_now()
        waited = await self.navigator.wait_for_load("networkidle", timeout_ms=10_000)
        return ActionResult(action="WAIT_FOR_LOAD", ok=True,
                            duration_ms=round(duration_ms(started), 2),
                            detail=f"settled in {waited:.0f}ms",
                            metadata={"wait_ms": waited})

    async def end_session(self) -> ActionResult:
        """Finish the visit; the browser session itself is closed by the worker."""
        started = utc_now()
        summary = self.session.diagnostics.summary()
        LOGGER.info("Ending session for %s: %s", self.session.worker_id, summary)
        return ActionResult(action="END_SESSION", ok=True,
                            duration_ms=round(duration_ms(started), 2),
                            detail=self.identity.describe(), metadata=summary)

    # --------------------------------------------------------------- identity
    async def get_available_avatars(self) -> list[Avatar]:
        """Avatars offered by the website."""
        return await self.avatars.discover()

    async def select_avatar(self, avatar: Avatar | None = None) -> ActionResult:
        """Select one of the website-provided avatars."""
        result = await self.avatars.select(avatar)
        if result.ok:
            index = int(result.metadata.get("index", 0))
            self.identity.avatar = Avatar(index=index, identifier=result.target_id,
                                          label=result.detail)
        return result

    async def get_available_usernames(self) -> list[Username]:
        """Usernames offered by the website."""
        return await self.usernames.discover()

    async def select_username(self, username: Username | None = None) -> ActionResult:
        """Select one of the website-provided usernames."""
        result = await self.usernames.select(username)
        if result.ok:
            index = int(result.metadata.get("index", 0))
            self.identity.username = Username(index=index, value=result.target_id)
            enter = self.selectors.candidates("identity.enter_button")
            if await self.interaction.exists(enter, timeout_ms=1_500):
                await self.interaction.click(enter, action="CONFIRM_IDENTITY")
                await self.navigator.wait_for_load("domcontentloaded", timeout_ms=8_000)
        return result

    # ----------------------------------------------------------------- forums
    async def get_forums(self) -> list[ForumRef]:
        """Forums exposed by the site."""
        forums = await self.forums.discover()
        if not forums:
            LOGGER.warning("No forums discovered at %s", self.navigator.current_url)
        return forums

    async def enter_forum(self, forum: ForumRef) -> ActionResult:
        """Enter *forum*."""
        result = await self.forums.enter(forum)
        if result.ok:
            self.current_forum = forum
            self.current_post = None
        self._raise_if_rate_limited(result.http_status)
        return result

    async def browse_forum(self, depth: int = 1) -> ActionResult:
        """Browse the current forum feed."""
        return await self.forums.browse(depth)

    async def return_to_forum_list(self) -> ActionResult:
        """Go back to the landing page so another forum can be chosen."""
        started = utc_now()
        navigation = await self.navigator.goto("")
        self.current_post = None
        return ActionResult(action="SWITCH_FORUM", ok=navigation.ok,
                            duration_ms=round(duration_ms(started), 2),
                            http_status=navigation.status, error=navigation.error)

    # ------------------------------------------------------------------ posts
    async def get_initial_posts(self, limit: int = 50) -> list[PostRef]:
        """Initial posts in the current forum."""
        return await self.posts.list_initial_posts(limit=limit)

    async def read_post(self, post: PostRef) -> ActionResult:
        """Open a post so its replies are visible."""
        result = await self.posts.open_post(post)
        if result.ok:
            self.current_post = post
        self._raise_if_rate_limited(result.http_status)
        return result

    async def create_post(self, body: str, *, title: str = "") -> ActionResult:
        """Publish a post in the current forum."""
        if self.current_forum is None:
            raise AdapterError("Cannot post before entering a forum",
                               action="CREATE_POST")
        result = await self.posts.create(body, title=title)
        self._check_rate_limit_banner_sync()
        return result

    async def create_reply(self, post: PostRef, body: str) -> ActionResult:
        """Reply to *post*."""
        result = await self.replies.create(post, body)
        self._check_rate_limit_banner_sync()
        return result

    def select_reply_target(self, posts: Sequence[PostRef], *,
                            mode: ReplyTargetMode = ReplyTargetMode.RANDOM,
                            keywords: Sequence[str] = (),
                            specific_post_id: str = "") -> PostRef | None:
        """Choose which initial post to reply to."""
        return self.replies.select_target(posts, mode=mode, keywords=keywords,
                                          specific_post_id=specific_post_id)

    # ------------------------------------------------------------ rate limits
    def _raise_if_rate_limited(self, status: int | None) -> None:
        if status is not None and status in self.rate_limit_status_codes:
            raise RateLimitedError(
                f"Target responded HTTP {status}; backing off as configured",
                status=status)

    def _check_rate_limit_banner_sync(self) -> None:
        """Raise when a throttling response was observed on this session."""
        throttled = self.session.diagnostics.rate_limited(self.rate_limit_status_codes)
        if throttled:
            latest = throttled[-1]
            raise RateLimitedError(
                f"Target responded HTTP {latest.status} for {latest.url}; backing off",
                status=latest.status)

    def describe_identity(self) -> SiteIdentity:
        """The identity the site handed to this session."""
        return self.identity
