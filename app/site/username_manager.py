"""Discovery and selection of the website-provided temporary usernames."""

from __future__ import annotations

import random
from typing import Sequence

from app.browser.interaction import Interaction
from app.browser.selectors import SelectorSet
from app.site.base_adapter import ActionResult, AdapterError, Username
from app.utils.logger import get_logger
from app.utils.time_utils import duration_ms, utc_now

LOGGER = get_logger("site.username")


class UsernameManager:
    """Reads the temporary usernames offered by the site and picks one."""

    def __init__(self, interaction: Interaction, selectors: SelectorSet, *,
                 rng: random.Random | None = None) -> None:
        self.interaction = interaction
        self.selectors = selectors
        self._rng = rng or random.Random()
        self.available: list[Username] = []

    async def discover(self, *, timeout_ms: int = 10_000) -> list[Username]:
        """Return the username choices the website is currently offering."""
        attribute = self.selectors.attribute("identity.username_label_attribute")
        elements = await self.interaction.collect(
            self.selectors.candidates("identity.username_options"),
            attributes=(attribute, "value", "id"),
            timeout_ms=timeout_ms,
        )
        usernames = [
            Username(
                index=element.index,
                value=(element.attribute(attribute)
                       or element.text
                       or element.attribute("value")
                       or f"user-{element.index + 1}"),
            )
            for element in elements
        ]
        self.available = usernames
        LOGGER.info("Discovered %d username option(s)", len(usernames))
        return usernames

    async def refresh(self) -> bool:
        """Ask the site for a fresh batch of usernames, when it offers that."""
        candidates = self.selectors.candidates("identity.username_refresh")
        if not await self.interaction.exists(candidates, timeout_ms=1_000):
            return False
        result = await self.interaction.click(candidates, action="REFRESH_USERNAMES")
        if result.ok:
            await self.discover()
        return result.ok

    async def select(self, username: Username | None = None, *,
                     strategy: str = "random") -> ActionResult:
        """Select one of the website-provided usernames."""
        started = utc_now()
        if not self.available:
            await self.discover()
        if not self.available:
            raise AdapterError(
                "The website did not offer any username options",
                action="SELECT_USERNAME",
                selector=self.selectors.first("identity.username_options"),
            )

        chosen = username or self._choose(self.available, strategy)
        result = await self.interaction.click(
            self.selectors.candidates("identity.username_options"),
            index=chosen.index, action="SELECT_USERNAME",
        )
        elapsed = round(duration_ms(started), 2)
        if not result.ok:
            return ActionResult(action="SELECT_USERNAME", ok=False, duration_ms=elapsed,
                                error=result.error, selector=result.selector,
                                metadata={"options": len(self.available)})
        LOGGER.info("Selected username %s", chosen.describe())
        return ActionResult(
            action="SELECT_USERNAME", ok=True, duration_ms=elapsed,
            target_id=chosen.value, detail=chosen.describe(), selector=result.selector,
            metadata={"options": len(self.available), "index": chosen.index},
        )

    def _choose(self, usernames: Sequence[Username], strategy: str) -> Username:
        if strategy == "first":
            return usernames[0]
        if strategy == "last":
            return usernames[-1]
        return self._rng.choice(list(usernames))
