"""Discovery and selection of the website-provided avatars.

Social Worker never invents an avatar: it reads whatever the site offers and
picks one of those options.
"""

from __future__ import annotations

import random
from typing import Sequence

from app.browser.interaction import Interaction
from app.browser.selectors import SelectorSet
from app.site.base_adapter import ActionResult, AdapterError, Avatar
from app.utils.logger import get_logger
from app.utils.time_utils import duration_ms, utc_now

LOGGER = get_logger("site.avatar")


class AvatarManager:
    """Reads the avatar choices from the page and selects one."""

    def __init__(self, interaction: Interaction, selectors: SelectorSet, *,
                 rng: random.Random | None = None) -> None:
        self.interaction = interaction
        self.selectors = selectors
        self._rng = rng or random.Random()
        self.available: list[Avatar] = []

    async def discover(self, *, timeout_ms: int = 10_000) -> list[Avatar]:
        """Return every avatar option the website is currently offering."""
        attribute = self.selectors.attribute("identity.avatar_label_attribute")
        elements = await self.interaction.collect(
            self.selectors.candidates("identity.avatar_options"),
            attributes=(attribute, "id", "alt", "title", "value"),
            timeout_ms=timeout_ms,
        )
        avatars = [
            Avatar(
                index=element.index,
                identifier=(element.attribute(attribute)
                            or element.attribute("value")
                            or element.attribute("id")
                            or f"avatar-{element.index + 1}"),
                label=(element.text
                       or element.attribute("alt")
                       or element.attribute("title")
                       or f"Avatar {element.index + 1}"),
            )
            for element in elements
        ]
        self.available = avatars
        LOGGER.info("Discovered %d avatar option(s)", len(avatars))
        return avatars

    async def select(self, avatar: Avatar | None = None, *,
                     strategy: str = "random") -> ActionResult:
        """Select an avatar from the discovered options."""
        started = utc_now()
        if not self.available:
            await self.discover()
        if not self.available:
            raise AdapterError(
                "The website did not offer any avatar options",
                action="SELECT_AVATAR",
                selector=self.selectors.first("identity.avatar_options"),
            )

        chosen = avatar or self._choose(self.available, strategy)
        result = await self.interaction.click(
            self.selectors.candidates("identity.avatar_options"),
            index=chosen.index, action="SELECT_AVATAR",
        )
        elapsed = round(duration_ms(started), 2)
        if not result.ok:
            return ActionResult(action="SELECT_AVATAR", ok=False, duration_ms=elapsed,
                                error=result.error, selector=result.selector,
                                metadata={"options": len(self.available)})
        LOGGER.info("Selected avatar %s", chosen.describe())
        return ActionResult(
            action="SELECT_AVATAR", ok=True, duration_ms=elapsed,
            target_id=chosen.identifier, detail=chosen.describe(),
            selector=result.selector,
            metadata={"options": len(self.available), "index": chosen.index},
        )

    def _choose(self, avatars: Sequence[Avatar], strategy: str) -> Avatar:
        if strategy == "first":
            return avatars[0]
        if strategy == "last":
            return avatars[-1]
        return self._rng.choice(list(avatars))
