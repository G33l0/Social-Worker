"""Navigation with response-time measurement."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

from app.browser.session import BrowserSession
from app.utils.logger import BROWSER_LOGGER, get_logger
from app.utils.time_utils import duration_ms, utc_now

LOGGER = get_logger(BROWSER_LOGGER)


class NavigationError(RuntimeError):
    """Raised when a navigation fails or times out."""


@dataclass
class NavigationResult:
    """Outcome of a single navigation."""

    url: str
    ok: bool
    status: int | None = None
    duration_ms: float = 0.0
    timing: dict[str, float] = field(default_factory=dict)
    error: str = ""

    @property
    def rate_limited(self) -> bool:
        """True when the target signalled throttling."""
        return self.status in (429, 503)


class Navigator:
    """Page navigation helpers that record timing for every request."""

    def __init__(self, session: BrowserSession, base_url: str = "", *,
                 wait_until: str = "domcontentloaded",
                 collect_timing: bool = True) -> None:
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.wait_until = wait_until
        self.collect_timing = collect_timing
        self.pages_visited = 0

    def resolve(self, url: str) -> str:
        """Turn a relative path into an absolute URL against the base."""
        if url.startswith(("http://", "https://")):
            return url
        if not self.base_url:
            return url
        return urljoin(self.base_url + "/", url.lstrip("/"))

    async def goto(self, url: str = "", *, wait_until: str | None = None,
                   timeout_ms: int | None = None) -> NavigationResult:
        """Navigate to *url* and measure how long the target took to answer."""
        target = self.resolve(url) if url else self.base_url
        started = utc_now()
        kwargs: dict[str, Any] = {"wait_until": wait_until or self.wait_until}
        if timeout_ms:
            kwargs["timeout"] = timeout_ms
        try:
            response = await self.session.page.goto(target, **kwargs)
        except Exception as exc:
            elapsed = duration_ms(started)
            LOGGER.error("Navigation to %s failed after %.0fms: %s", target, elapsed, exc)
            return NavigationResult(url=target, ok=False, duration_ms=elapsed,
                                    error=str(exc))
        elapsed = duration_ms(started)
        status = response.status if response is not None else None
        timing = await self.session.navigation_timing() if self.collect_timing else {}
        self.pages_visited += 1
        result = NavigationResult(
            url=target,
            ok=status is None or status < 400,
            status=status,
            duration_ms=round(elapsed, 2),
            timing=timing,
        )
        if not result.ok:
            result.error = f"HTTP {status}"
        LOGGER.info("Navigated to %s (status=%s, %.0fms)", target, status, elapsed)
        return result

    async def reload(self) -> NavigationResult:
        """Reload the current page."""
        started = utc_now()
        try:
            response = await self.session.page.reload(wait_until=self.wait_until)
        except Exception as exc:
            return NavigationResult(url=self.current_url, ok=False,
                                    duration_ms=duration_ms(started), error=str(exc))
        status = response.status if response is not None else None
        self.pages_visited += 1
        return NavigationResult(url=self.current_url, ok=status is None or status < 400,
                                status=status, duration_ms=round(duration_ms(started), 2))

    async def back(self) -> NavigationResult:
        """Navigate back in history."""
        started = utc_now()
        try:
            response = await self.session.page.go_back(wait_until=self.wait_until)
        except Exception as exc:
            return NavigationResult(url=self.current_url, ok=False,
                                    duration_ms=duration_ms(started), error=str(exc))
        status = response.status if response is not None else None
        return NavigationResult(url=self.current_url, ok=True, status=status,
                                duration_ms=round(duration_ms(started), 2))

    async def wait_for_load(self, state: str = "networkidle",
                            timeout_ms: int | None = None) -> float:
        """Wait for a load state; returns the wait duration in milliseconds."""
        started = utc_now()
        try:
            kwargs: dict[str, Any] = {"state": state}
            if timeout_ms:
                kwargs["timeout"] = timeout_ms
            await self.session.page.wait_for_load_state(**kwargs)
        except Exception as exc:
            LOGGER.debug("wait_for_load(%s) timed out: %s", state, exc)
        return round(duration_ms(started), 2)

    async def wait_for_selector(self, selector: str, *, timeout_ms: int | None = None,
                                state: str = "visible") -> bool:
        """Wait for *selector*; returns False instead of raising on timeout."""
        try:
            kwargs: dict[str, Any] = {"state": state}
            if timeout_ms:
                kwargs["timeout"] = timeout_ms
            await self.session.page.wait_for_selector(selector, **kwargs)
            return True
        except Exception:
            return False

    async def wait_for_any(self, selectors: list[str], *,
                           timeout_ms: int | None = None) -> str:
        """Wait for the first of *selectors* to appear; returns it (or "")."""
        for selector in selectors:
            if await self.wait_for_selector(selector, timeout_ms=timeout_ms or 3_000):
                return selector
        return ""

    @property
    def current_url(self) -> str:
        """URL of the live page."""
        try:
            return self.session.page.url
        except Exception:  # pragma: no cover
            return ""

    async def title(self) -> str:
        """Title of the live page."""
        try:
            return await self.session.page.title()
        except Exception:  # pragma: no cover
            return ""
