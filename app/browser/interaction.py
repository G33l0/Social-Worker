"""Element interaction helpers built on top of the selector catalogue.

Every helper accepts a *list* of candidate selectors and tries them in order,
so a small markup change on the target site does not break a whole test run.
Each interaction is timed and returns a structured result that the metrics
layer can persist.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Sequence

from app.browser.session import BrowserSession
from app.utils.logger import BROWSER_LOGGER, get_logger
from app.utils.time_utils import duration_ms, utc_now

LOGGER = get_logger(BROWSER_LOGGER)


class InteractionError(RuntimeError):
    """Raised when an element cannot be found or acted upon."""

    def __init__(self, message: str, *, selector: str = "", action: str = "") -> None:
        super().__init__(message)
        self.selector = selector
        self.action = action


@dataclass
class ElementInfo:
    """A lightweight description of an element found on the page."""

    index: int
    text: str = ""
    attributes: dict[str, str] = field(default_factory=dict)
    selector: str = ""

    def attribute(self, name: str, default: str = "") -> str:
        """Return an attribute value."""
        return self.attributes.get(name, default)


@dataclass
class InteractionResult:
    """Outcome of one interaction."""

    action: str
    ok: bool
    selector: str = ""
    duration_ms: float = 0.0
    detail: str = ""
    error: str = ""


class Interaction:
    """Click / type / read helpers with candidate-selector fallback."""

    def __init__(self, session: BrowserSession, *, default_timeout_ms: int = 10_000,
                 typing_delay_ms: tuple[int, int] = (5, 25),
                 human_typing: bool = True) -> None:
        self.session = session
        self.default_timeout_ms = default_timeout_ms
        self.typing_delay_ms = typing_delay_ms
        self.human_typing = human_typing

    # ----------------------------------------------------------------- lookup
    async def find_selector(self, candidates: Sequence[str], *,
                            timeout_ms: int | None = None) -> str:
        """Return the first candidate selector that matches at least one node."""
        page = self.session.page
        per_candidate = max(250, int((timeout_ms or self.default_timeout_ms)
                                     / max(1, len(candidates))))
        for selector in candidates:
            try:
                await page.wait_for_selector(selector, timeout=per_candidate,
                                             state="attached")
                return selector
            except Exception:
                continue
        return ""

    async def count(self, candidates: Sequence[str], *,
                    timeout_ms: int | None = None) -> tuple[str, int]:
        """Return ``(selector, count)`` for the first matching candidate."""
        selector = await self.find_selector(candidates, timeout_ms=timeout_ms)
        if not selector:
            return "", 0
        try:
            return selector, await self.session.page.locator(selector).count()
        except Exception:  # pragma: no cover
            return selector, 0

    async def collect(self, candidates: Sequence[str], *,
                      attributes: Sequence[str] = (), limit: int = 200,
                      timeout_ms: int | None = None) -> list[ElementInfo]:
        """Return descriptions of every element matching the first candidate."""
        selector, total = await self.count(candidates, timeout_ms=timeout_ms)
        if not selector or total == 0:
            return []
        locator = self.session.page.locator(selector)
        elements: list[ElementInfo] = []
        for index in range(min(total, limit)):
            node = locator.nth(index)
            try:
                text = (await node.inner_text(timeout=2_000)).strip()
            except Exception:
                text = ""
            values: dict[str, str] = {}
            for name in attributes:
                try:
                    value = await node.get_attribute(name)
                except Exception:
                    value = None
                if value is not None:
                    values[name] = value
            elements.append(ElementInfo(index=index, text=text[:500],
                                        attributes=values, selector=selector))
        return elements

    async def exists(self, candidates: Sequence[str], *, timeout_ms: int = 1_500) -> bool:
        """True when any candidate matches."""
        return bool(await self.find_selector(candidates, timeout_ms=timeout_ms))

    async def text_of(self, candidates: Sequence[str], *,
                      timeout_ms: int | None = None) -> str:
        """Inner text of the first matching element."""
        selector = await self.find_selector(candidates, timeout_ms=timeout_ms)
        if not selector:
            return ""
        try:
            return (await self.session.page.locator(selector).first
                    .inner_text(timeout=2_000)).strip()
        except Exception:
            return ""

    # ---------------------------------------------------------------- actions
    async def click(self, candidates: Sequence[str], *, index: int = 0,
                    timeout_ms: int | None = None,
                    action: str = "click") -> InteractionResult:
        """Click the *index*-th element matching the first viable candidate."""
        started = utc_now()
        selector = await self.find_selector(candidates, timeout_ms=timeout_ms)
        if not selector:
            return self._failure(action, candidates, started,
                                 "no candidate selector matched")
        try:
            locator = self.session.page.locator(selector).nth(index)
            await locator.click(timeout=timeout_ms or self.default_timeout_ms)
        except Exception as exc:
            return InteractionResult(action=action, ok=False, selector=selector,
                                     duration_ms=round(duration_ms(started), 2),
                                     error=str(exc))
        return InteractionResult(action=action, ok=True, selector=selector,
                                 duration_ms=round(duration_ms(started), 2),
                                 detail=f"index={index}")

    async def click_random(self, candidates: Sequence[str], *,
                           timeout_ms: int | None = None,
                           action: str = "click_random") -> tuple[InteractionResult, int]:
        """Click a randomly chosen element from the matching set."""
        selector, total = await self.count(candidates, timeout_ms=timeout_ms)
        if not selector or total == 0:
            started = utc_now()
            return self._failure(action, candidates, started, "no elements found"), -1
        index = random.randrange(total)
        result = await self.click([selector], index=index, timeout_ms=timeout_ms,
                                  action=action)
        return result, index

    async def fill(self, candidates: Sequence[str], value: str, *,
                   timeout_ms: int | None = None, human_typing: bool | None = None,
                   action: str = "fill") -> InteractionResult:
        """Type *value* into the first matching input."""
        started = utc_now()
        selector = await self.find_selector(candidates, timeout_ms=timeout_ms)
        if not selector:
            return self._failure(action, candidates, started,
                                 "no candidate selector matched")
        try:
            locator = self.session.page.locator(selector).first
            await locator.click(timeout=timeout_ms or self.default_timeout_ms)
            await locator.fill("", timeout=timeout_ms or self.default_timeout_ms)
            typed = self.human_typing if human_typing is None else human_typing
            if typed and self.typing_delay_ms[1] > 0:
                delay = random.randint(*self.typing_delay_ms)
                await locator.type(value, delay=delay,
                                   timeout=timeout_ms or self.default_timeout_ms)
            else:
                await locator.fill(value, timeout=timeout_ms or self.default_timeout_ms)
        except Exception as exc:
            return InteractionResult(action=action, ok=False, selector=selector,
                                     duration_ms=round(duration_ms(started), 2),
                                     error=str(exc))
        return InteractionResult(action=action, ok=True, selector=selector,
                                 duration_ms=round(duration_ms(started), 2),
                                 detail=f"{len(value)} chars")

    async def select_option(self, candidates: Sequence[str], value: str, *,
                            timeout_ms: int | None = None) -> InteractionResult:
        """Choose *value* in a ``<select>`` element."""
        started = utc_now()
        selector = await self.find_selector(candidates, timeout_ms=timeout_ms)
        if not selector:
            return self._failure("select_option", candidates, started, "not found")
        try:
            await self.session.page.locator(selector).first.select_option(value)
        except Exception as exc:
            return InteractionResult(action="select_option", ok=False, selector=selector,
                                     duration_ms=round(duration_ms(started), 2),
                                     error=str(exc))
        return InteractionResult(action="select_option", ok=True, selector=selector,
                                 duration_ms=round(duration_ms(started), 2), detail=value)

    async def scroll(self, pixels: int = 600) -> InteractionResult:
        """Scroll the page, simulating a visitor reading the feed."""
        started = utc_now()
        try:
            await self.session.page.mouse.wheel(0, pixels)
        except Exception as exc:
            return InteractionResult(action="scroll", ok=False,
                                     duration_ms=round(duration_ms(started), 2),
                                     error=str(exc))
        return InteractionResult(action="scroll", ok=True,
                                 duration_ms=round(duration_ms(started), 2),
                                 detail=f"{pixels}px")

    async def attribute_of(self, candidates: Sequence[str], name: str, *,
                           index: int = 0, timeout_ms: int | None = None) -> str:
        """Read an attribute from the *index*-th matching element."""
        selector = await self.find_selector(candidates, timeout_ms=timeout_ms)
        if not selector:
            return ""
        try:
            value = await self.session.page.locator(selector).nth(index).get_attribute(name)
            return value or ""
        except Exception:
            return ""

    def _failure(self, action: str, candidates: Sequence[str], started: Any,
                 message: str) -> InteractionResult:
        selector = candidates[0] if candidates else ""
        LOGGER.warning("Interaction %s failed (%s): %s", action, selector, message)
        return InteractionResult(action=action, ok=False, selector=selector,
                                 duration_ms=round(duration_ms(started), 2),
                                 error=message)
