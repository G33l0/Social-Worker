"""Playwright browser lifecycle management.

One Playwright instance and a small pool of Chromium browsers are shared by all
workers; each visitor session gets its own browser *context* so cookies and
storage never leak between simulated visitors.

Every context announces itself through a ``X-Load-Test`` header and an appended
user-agent token (``SocialWorker-LoadTest/<version>``) so the operator can
identify, throttle or exclude synthetic traffic in their own logs.  Social
Worker does not spoof fingerprints or attempt to look like an unrelated client.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

from app.utils.config import BrowserConfig, WebsiteConfig
from app.utils.logger import get_logger, BROWSER_LOGGER

if TYPE_CHECKING:  # pragma: no cover - typing only
    from playwright.async_api import Browser, BrowserContext, Playwright

LOGGER = get_logger(BROWSER_LOGGER)

APP_VERSION = "1.0.0"


class BrowserUnavailableError(RuntimeError):
    """Raised when Playwright or its browser binaries are not installed."""


@dataclass
class ContextOptions:
    """Per-location context settings."""

    locale: str = "en-US"
    timezone_id: str = "UTC"
    viewport_width: int = 1366
    viewport_height: int = 768
    proxy_url: str = ""
    location_key: str = ""
    identify_as: str = "SocialWorker-LoadTest"
    ignore_https_errors: bool = False


class BrowserManager:
    """Owns the Playwright driver and a pool of browsers."""

    def __init__(self, config: BrowserConfig | None = None,
                 website: WebsiteConfig | None = None) -> None:
        self.config = config or BrowserConfig()
        self.website = website or WebsiteConfig()
        self._playwright: "Playwright | None" = None
        self._browsers: list["Browser"] = []
        self._context_counts: list[int] = []
        self._lock = asyncio.Lock()
        self._started = False

    # ---------------------------------------------------------------- startup
    async def start(self) -> None:
        """Launch Playwright and the first browser."""
        if self._started:
            return
        async with self._lock:
            if self._started:
                return
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:  # pragma: no cover - environment dependent
                raise BrowserUnavailableError(
                    "Playwright is not installed. Run:\n"
                    "    pip install playwright\n"
                    "    python -m playwright install chromium"
                ) from exc

            self._playwright = await async_playwright().start()
            await self._launch_browser()
            self._started = True
            LOGGER.info("Browser manager started (headless=%s, engine=%s)",
                        self.config.headless, self.config.engine)

    async def _launch_browser(self) -> "Browser":
        assert self._playwright is not None
        launch_kwargs: dict[str, Any] = {
            "headless": self.config.headless,
            "args": list(self.config.launch_args),
        }
        if self.config.slow_mo_ms:
            launch_kwargs["slow_mo"] = self.config.slow_mo_ms
        if self.config.executable_path:
            launch_kwargs["executable_path"] = self.config.executable_path
        try:
            browser = await self._playwright.chromium.launch(**launch_kwargs)
        except Exception as exc:  # pragma: no cover - environment dependent
            raise BrowserUnavailableError(
                f"Could not launch Chromium: {exc}\n"
                "Install the browser binaries with: python -m playwright install chromium"
            ) from exc
        self._browsers.append(browser)
        self._context_counts.append(0)
        LOGGER.info("Launched Chromium instance %d", len(self._browsers))
        return browser

    async def _pick_browser(self) -> int:
        """Return the index of a browser with spare context capacity."""
        for index, count in enumerate(self._context_counts):
            if count < self.config.contexts_per_browser:
                return index
        if len(self._browsers) < self.config.max_browsers:
            await self._launch_browser()
            return len(self._browsers) - 1
        # All browsers at nominal capacity: use the least loaded one.
        return self._context_counts.index(min(self._context_counts))

    # ---------------------------------------------------------------- context
    async def new_context(self, options: ContextOptions | None = None) -> "BrowserContext":
        """Create an isolated browser context for one visitor session."""
        if not self._started:
            await self.start()
        options = options or ContextOptions()
        async with self._lock:
            index = await self._pick_browser()
            browser = self._browsers[index]
            self._context_counts[index] += 1

        context_kwargs: dict[str, Any] = {
            "viewport": {"width": options.viewport_width or self.config.viewport_width,
                         "height": options.viewport_height or self.config.viewport_height},
            "locale": options.locale,
            "timezone_id": options.timezone_id,
            "ignore_https_errors": options.ignore_https_errors or self.config.ignore_https_errors,
            "extra_http_headers": {
                "X-Load-Test": "social-worker",
                "X-Load-Test-Client": f"{options.identify_as}/{APP_VERSION}",
                "X-Load-Test-Location": options.location_key or "unknown",
            },
        }
        if options.proxy_url:
            # Only an explicitly configured, authorized regional test proxy.
            context_kwargs["proxy"] = {"server": options.proxy_url}

        try:
            context = await browser.new_context(**context_kwargs)
        except Exception:
            async with self._lock:
                self._context_counts[index] = max(0, self._context_counts[index] - 1)
            raise

        await context.add_init_script(
            "window.__socialWorkerLoadTest = true;"
        )
        context.set_default_timeout(self.config.default_timeout_ms)
        context.set_default_navigation_timeout(self.config.navigation_timeout_ms)
        setattr(context, "_sw_browser_index", index)
        return context

    async def close_context(self, context: "BrowserContext") -> None:
        """Close a context and release its slot."""
        index = getattr(context, "_sw_browser_index", None)
        try:
            await context.close()
        except Exception as exc:  # pragma: no cover - best effort cleanup
            LOGGER.debug("Error closing context: %s", exc)
        if index is not None:
            async with self._lock:
                if 0 <= index < len(self._context_counts):
                    self._context_counts[index] = max(0, self._context_counts[index] - 1)

    # --------------------------------------------------------------- shutdown
    async def stop(self) -> None:
        """Close every browser and stop Playwright."""
        async with self._lock:
            for browser in self._browsers:
                try:
                    await browser.close()
                except Exception as exc:  # pragma: no cover - best effort
                    LOGGER.debug("Error closing browser: %s", exc)
            self._browsers.clear()
            self._context_counts.clear()
            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                except Exception as exc:  # pragma: no cover
                    LOGGER.debug("Error stopping Playwright: %s", exc)
                self._playwright = None
            self._started = False
        LOGGER.info("Browser manager stopped")

    # ------------------------------------------------------------------ state
    @property
    def started(self) -> bool:
        """True once Playwright is running."""
        return self._started

    def snapshot(self) -> dict[str, Any]:
        """Pool utilisation, for the dashboard."""
        return {
            "started": self._started,
            "browsers": len(self._browsers),
            "contexts": sum(self._context_counts),
            "contexts_per_browser": self.config.contexts_per_browser,
            "max_browsers": self.config.max_browsers,
            "headless": self.config.headless,
        }

    @staticmethod
    def is_available() -> bool:
        """True when the Playwright python package is importable."""
        try:
            import playwright.async_api  # noqa: F401
        except ImportError:
            return False
        return True
