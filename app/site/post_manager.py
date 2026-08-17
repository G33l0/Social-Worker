"""Site-side reading and creation of forum posts."""

from __future__ import annotations

from app.browser.interaction import Interaction
from app.browser.navigator import Navigator
from app.browser.selectors import SelectorSet
from app.site.base_adapter import ActionResult, AdapterError, PostRef
from app.utils.logger import get_logger
from app.utils.time_utils import duration_ms, utc_now

LOGGER = get_logger("site.post")


class SitePostManager:
    """Lists the initial posts in a forum and publishes new ones."""

    def __init__(self, navigator: Navigator, interaction: Interaction,
                 selectors: SelectorSet, *, dry_run: bool = False) -> None:
        self.navigator = navigator
        self.interaction = interaction
        self.selectors = selectors
        self.dry_run = dry_run
        self.available: list[PostRef] = []

    async def list_initial_posts(self, limit: int = 50, *,
                                 timeout_ms: int = 10_000) -> list[PostRef]:
        """Return the top-level posts currently visible in the forum."""
        id_attribute = self.selectors.attribute("posts.post_id_attribute")
        time_attribute = self.selectors.attribute("posts.post_timestamp_attribute")
        elements = await self.interaction.collect(
            self.selectors.candidates("posts.post_items"),
            attributes=(id_attribute, time_attribute, "href", "id"),
            limit=limit, timeout_ms=timeout_ms,
        )
        posts: list[PostRef] = []
        for element in elements:
            post_id = (element.attribute(id_attribute)
                       or element.attribute("id")
                       or f"index-{element.index}")
            lines = [line.strip() for line in element.text.splitlines() if line.strip()]
            posts.append(PostRef(
                post_id=post_id,
                title=lines[0] if lines else post_id,
                excerpt=" ".join(lines[1:])[:280],
                url=self.navigator.resolve(element.attribute("href", "")),
                index=element.index,
                created_at=element.attribute(time_attribute),
            ))
        self.available = posts
        LOGGER.info("Found %d initial post(s)", len(posts))
        return posts

    async def open_post(self, post: PostRef, *, timeout_ms: int = 15_000) -> ActionResult:
        """Open a post's detail page so its replies become visible."""
        started = utc_now()
        status: int | None = None
        opened = False
        link_candidates = self.selectors.candidates("posts.post_link")
        if post.index >= 0 and await self.interaction.exists(link_candidates,
                                                             timeout_ms=1_500):
            result = await self.interaction.click(link_candidates, index=post.index,
                                                  action="READ_POST",
                                                  timeout_ms=timeout_ms)
            opened = result.ok
        if not opened and post.url:
            navigation = await self.navigator.goto(post.url)
            status = navigation.status
            opened = navigation.ok

        elapsed = round(duration_ms(started), 2)
        if not opened:
            return ActionResult(action="READ_POST", ok=False, duration_ms=elapsed,
                                target_id=post.post_id, http_status=status,
                                error="could not open post")
        await self.navigator.wait_for_any(
            self.selectors.candidates("posts.post_body"), timeout_ms=timeout_ms)
        return ActionResult(action="READ_POST", ok=True, duration_ms=elapsed,
                            target_id=post.post_id, detail=post.describe(),
                            http_status=status,
                            metadata={"url": self.navigator.current_url})

    async def create(self, body: str, *, title: str = "",
                     timeout_ms: int = 20_000) -> ActionResult:
        """Publish a post.  In dry-run mode the form is filled but not submitted."""
        started = utc_now()
        input_candidates = self.selectors.candidates("posts.new_post_input")
        if not await self.interaction.exists(input_candidates, timeout_ms=3_000):
            raise AdapterError("No post composer found in this forum",
                               action="CREATE_POST",
                               selector=self.selectors.first("posts.new_post_input"))

        if title:
            title_candidates = self.selectors.candidates("posts.new_post_title_input")
            if await self.interaction.exists(title_candidates, timeout_ms=1_000):
                await self.interaction.fill(title_candidates, title,
                                            action="FILL_POST_TITLE")

        filled = await self.interaction.fill(input_candidates, body,
                                             action="FILL_POST_BODY",
                                             timeout_ms=timeout_ms)
        if not filled.ok:
            return ActionResult(action="CREATE_POST", ok=False,
                                duration_ms=round(duration_ms(started), 2),
                                selector=filled.selector, error=filled.error)

        if self.dry_run:
            elapsed = round(duration_ms(started), 2)
            LOGGER.info("DRY RUN: post composed but not submitted (%d chars)", len(body))
            return ActionResult(action="CREATE_POST", ok=True, duration_ms=elapsed,
                                dry_run=True, detail="composed, submission disabled",
                                metadata={"characters": len(body),
                                          "url": self.navigator.current_url})

        submitted = await self.interaction.click(
            self.selectors.candidates("posts.new_post_submit"),
            action="SUBMIT_POST", timeout_ms=timeout_ms)
        if not submitted.ok:
            return ActionResult(action="CREATE_POST", ok=False,
                                duration_ms=round(duration_ms(started), 2),
                                selector=submitted.selector, error=submitted.error)

        await self.navigator.wait_for_any(
            self.selectors.candidates("posts.post_success_flag")
            + self.selectors.candidates("posts.post_items"),
            timeout_ms=timeout_ms)
        elapsed = round(duration_ms(started), 2)
        post_id = await self.interaction.attribute_of(
            self.selectors.candidates("posts.post_items"),
            self.selectors.attribute("posts.post_id_attribute"), index=0)
        LOGGER.info("Created post in %.0fms (id=%s)", elapsed, post_id or "unknown")
        return ActionResult(action="CREATE_POST", ok=True, duration_ms=elapsed,
                            target_id=post_id, detail=f"{len(body)} chars",
                            metadata={"characters": len(body),
                                      "url": self.navigator.current_url})
