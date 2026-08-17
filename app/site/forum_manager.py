"""Site-side forum discovery and navigation."""

from __future__ import annotations

from app.browser.interaction import Interaction
from app.browser.navigator import Navigator
from app.browser.selectors import SelectorSet
from app.site.base_adapter import ActionResult, AdapterError, ForumRef
from app.utils.logger import get_logger
from app.utils.time_utils import duration_ms, utc_now
from app.utils.validation import validate_identifier

LOGGER = get_logger("site.forum")


class SiteForumManager:
    """Discovers the forums a site exposes and enters them."""

    def __init__(self, navigator: Navigator, interaction: Interaction,
                 selectors: SelectorSet) -> None:
        self.navigator = navigator
        self.interaction = interaction
        self.selectors = selectors
        self.available: list[ForumRef] = []

    async def discover(self, *, timeout_ms: int = 10_000) -> list[ForumRef]:
        """Return every forum link visible on the current page."""
        attribute = self.selectors.attribute("forums.forum_key_attribute")
        elements = await self.interaction.collect(
            self.selectors.candidates("forums.forum_links"),
            attributes=(attribute, "href", "title"),
            timeout_ms=timeout_ms,
        )
        forums: list[ForumRef] = []
        for element in elements:
            raw_key = (element.attribute(attribute)
                       or element.text
                       or element.attribute("href", "").rstrip("/").rsplit("/", 1)[-1])
            if not raw_key:
                continue
            try:
                key = validate_identifier(raw_key, field="forum key")
            except Exception:
                key = raw_key.strip().lower().replace(" ", "_")
            forums.append(ForumRef(
                key=key,
                name=element.text or key.replace("_", " ").title(),
                url=self.navigator.resolve(element.attribute("href", "")),
                index=element.index,
            ))
        self.available = forums
        LOGGER.info("Discovered %d forum(s): %s", len(forums),
                    ", ".join(forum.key for forum in forums))
        return forums

    def find(self, key: str) -> ForumRef | None:
        """Return a discovered forum by key (case-insensitive)."""
        target = key.strip().lower()
        for forum in self.available:
            if forum.key.lower() == target or forum.name.strip().lower() == target:
                return forum
        return None

    async def enter(self, forum: ForumRef, *, timeout_ms: int = 15_000) -> ActionResult:
        """Enter *forum* by clicking its link, falling back to direct navigation."""
        started = utc_now()
        clicked = False
        if forum.index >= 0:
            result = await self.interaction.click(
                self.selectors.candidates("forums.forum_links"),
                index=forum.index, action="ENTER_FORUM", timeout_ms=timeout_ms)
            clicked = result.ok

        status: int | None = None
        if not clicked:
            if not forum.url:
                raise AdapterError(f"Cannot enter forum {forum.key!r}: no link or URL",
                                   action="ENTER_FORUM")
            navigation = await self.navigator.goto(forum.url)
            status = navigation.status
            if not navigation.ok:
                return ActionResult(action="ENTER_FORUM", ok=False,
                                    duration_ms=round(duration_ms(started), 2),
                                    target_id=forum.key, http_status=status,
                                    error=navigation.error or "navigation failed")

        ready = await self.navigator.wait_for_any(
            self.selectors.candidates("forums.forum_ready"), timeout_ms=timeout_ms)
        elapsed = round(duration_ms(started), 2)
        if not ready:
            return ActionResult(action="ENTER_FORUM", ok=False, duration_ms=elapsed,
                                target_id=forum.key, http_status=status,
                                selector=self.selectors.first("forums.forum_ready"),
                                error="forum post list never appeared")
        LOGGER.info("Entered forum %s in %.0fms", forum.key, elapsed)
        return ActionResult(action="ENTER_FORUM", ok=True, duration_ms=elapsed,
                            target_id=forum.key, detail=forum.describe(),
                            http_status=status,
                            metadata={"url": self.navigator.current_url})

    async def browse(self, depth: int = 1, *, scroll_pixels: int = 700) -> ActionResult:
        """Scroll through the current forum feed *depth* times."""
        started = utc_now()
        scrolled = 0
        for _ in range(max(1, depth)):
            result = await self.interaction.scroll(scroll_pixels)
            if result.ok:
                scrolled += 1
        elapsed = round(duration_ms(started), 2)
        return ActionResult(action="BROWSE_FORUM", ok=scrolled > 0, duration_ms=elapsed,
                            detail=f"{scrolled} scroll(s)",
                            metadata={"url": self.navigator.current_url})
