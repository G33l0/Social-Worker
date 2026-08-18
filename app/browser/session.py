"""A single browser session: context + page + diagnostics capture."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TYPE_CHECKING

from app.browser.browser_manager import BrowserManager, ContextOptions
from app.utils.logger import BROWSER_LOGGER, get_logger
from app.utils.time_utils import timestamp_slug, utc_now

if TYPE_CHECKING:  # pragma: no cover - typing only
    from playwright.async_api import BrowserContext, Page, Response

LOGGER = get_logger(BROWSER_LOGGER)


@dataclass
class ResponseRecord:
    """One HTTP response observed by the session."""

    url: str
    status: int
    method: str = "GET"
    resource_type: str = ""
    duration_ms: float = 0.0
    at: str = ""


@dataclass
class SessionDiagnostics:
    """Console output, page errors and network activity for one session."""

    console_messages: list[dict[str, str]] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    responses: list[ResponseRecord] = field(default_factory=list)
    failed_requests: list[dict[str, str]] = field(default_factory=list)

    @property
    def javascript_errors(self) -> int:
        """Count of uncaught page errors plus console errors."""
        console_errors = sum(1 for message in self.console_messages
                             if message.get("type") == "error")
        return len(self.page_errors) + console_errors

    def rate_limited(self, codes: tuple[int, ...] = (429,)) -> list[ResponseRecord]:
        """Responses whose status indicates throttling."""
        return [record for record in self.responses if record.status in codes]

    def error_responses(self) -> list[ResponseRecord]:
        """Responses with a 4xx/5xx status."""
        return [record for record in self.responses if record.status >= 400]

    def summary(self) -> dict[str, Any]:
        """Aggregate view stored with the session telemetry."""
        durations = [record.duration_ms for record in self.responses if record.duration_ms]
        return {
            "responses": len(self.responses),
            "errors_4xx_5xx": len(self.error_responses()),
            "failed_requests": len(self.failed_requests),
            "console_messages": len(self.console_messages),
            "javascript_errors": self.javascript_errors,
            "avg_response_ms": round(sum(durations) / len(durations), 2) if durations else 0.0,
            "max_response_ms": round(max(durations), 2) if durations else 0.0,
        }


class BrowserSession:
    """Owns one browser context/page pair and its diagnostics."""

    def __init__(self, manager: BrowserManager, options: ContextOptions, *,
                 test_run_id: str = "", worker_id: str = "", session_id: str = "",
                 capture_console: bool = True, capture_network: bool = True,
                 capture_screenshot_on_failure: bool = True,
                 capture_html_on_failure: bool = True,
                 debug_directory: str | Path = "data/debug",
                 screenshot_directory: str | Path = "data/screenshots") -> None:
        self.manager = manager
        self.options = options
        self.test_run_id = test_run_id
        self.worker_id = worker_id
        self.session_id = session_id
        self.capture_console = capture_console
        self.capture_network = capture_network
        self.capture_screenshot_on_failure = capture_screenshot_on_failure
        self.capture_html_on_failure = capture_html_on_failure
        self.debug_directory = Path(debug_directory)
        self.screenshot_directory = Path(screenshot_directory)
        self.diagnostics = SessionDiagnostics()

        self._context: "BrowserContext | None" = None
        self._page: "Page | None" = None
        self._request_started: dict[Any, float] = {}
        self._closed = False

    # ------------------------------------------------------------------ setup
    async def open(self) -> "Page":
        """Create the context and page, wiring up diagnostics listeners."""
        self._context = await self.manager.new_context(self.options)
        self._page = await self._context.new_page()
        if self.capture_console:
            self._page.on("console", self._on_console)
            self._page.on("pageerror", self._on_page_error)
        if self.capture_network:
            self._page.on("request", self._on_request)
            self._page.on("response", self._on_response)
            self._page.on("requestfailed", self._on_request_failed)
        LOGGER.debug("Session %s opened for worker %s", self.session_id, self.worker_id)
        return self._page

    @property
    def page(self) -> "Page":
        """The live page (raises if the session is not open)."""
        if self._page is None:
            raise RuntimeError("Browser session is not open; call open() first")
        return self._page

    @property
    def context(self) -> "BrowserContext":
        """The live context."""
        if self._context is None:
            raise RuntimeError("Browser session is not open; call open() first")
        return self._context

    @property
    def is_open(self) -> bool:
        """True while the page is usable."""
        return self._page is not None and not self._closed

    # ------------------------------------------------------------- listeners
    def _on_console(self, message: Any) -> None:
        try:
            entry = {"type": message.type, "text": message.text[:500]}
        except Exception:  # pragma: no cover - defensive
            return
        self.diagnostics.console_messages.append(entry)
        if entry["type"] == "error":
            LOGGER.warning("Console error [%s]: %s", self.worker_id, entry["text"],
                           extra={"worker_id": self.worker_id})

    def _on_page_error(self, error: Any) -> None:
        text = str(error)[:1000]
        self.diagnostics.page_errors.append(text)
        LOGGER.warning("Page error [%s]: %s", self.worker_id, text,
                       extra={"worker_id": self.worker_id})

    def _on_request(self, request: Any) -> None:
        self._request_started[request] = time.monotonic()

    def _on_response(self, response: "Response") -> None:
        started = self._request_started.pop(response.request, None)
        duration = 0.0
        if started is not None:
            duration = (time.monotonic() - started) * 1000.0
        record = ResponseRecord(
            url=response.url[:500],
            status=response.status,
            method=response.request.method,
            resource_type=response.request.resource_type,
            duration_ms=round(duration, 2),
            at=utc_now().isoformat(),
        )
        self.diagnostics.responses.append(record)
        if response.status >= 400:
            LOGGER.warning("HTTP %s for %s", response.status, record.url,
                           extra={"worker_id": self.worker_id})

    def _on_request_failed(self, request: Any) -> None:
        failure = getattr(request, "failure", None)
        self.diagnostics.failed_requests.append({
            "url": str(request.url)[:500],
            "method": request.method,
            "error": str(failure) if failure else "unknown",
        })

    # ------------------------------------------------------------ diagnostics
    async def navigation_timing(self) -> dict[str, float]:
        """Read DNS/connect/response/load timings from the Navigation Timing API."""
        script = """() => {
          const entry = performance.getEntriesByType('navigation')[0];
          if (!entry) { return null; }
          return {
            dns_ms: entry.domainLookupEnd - entry.domainLookupStart,
            connect_ms: entry.connectEnd - entry.connectStart,
            tls_ms: entry.secureConnectionStart > 0
              ? entry.connectEnd - entry.secureConnectionStart : 0,
            ttfb_ms: entry.responseStart - entry.requestStart,
            response_ms: entry.responseEnd - entry.responseStart,
            dom_content_loaded_ms: entry.domContentLoadedEventEnd - entry.startTime,
            load_ms: entry.loadEventEnd > 0 ? entry.loadEventEnd - entry.startTime : 0,
            transfer_bytes: entry.transferSize || 0
          };
        }"""
        try:
            data = await self.page.evaluate(script)
        except Exception as exc:  # pragma: no cover - page may have navigated away
            LOGGER.debug("Navigation timing unavailable: %s", exc)
            return {}
        if not data:
            return {}
        return {key: round(float(value), 2) for key, value in data.items()}

    async def capture_failure(self, action: str, *, selector: str = "",
                              error: str = "") -> dict[str, str]:
        """Save a screenshot and the page HTML for a failed automation step.

        Artefacts are written to ``data/debug/<TEST-ID>/<worker-id>/``.
        """
        artefacts = {"screenshot_path": "", "html_path": "", "url": ""}
        if self._page is None or self._closed:
            return artefacts
        run_folder = self.test_run_id or "no-test-run"
        worker_folder = self.worker_id or "unknown-worker"
        target = self.debug_directory / run_folder / worker_folder
        target.mkdir(parents=True, exist_ok=True)
        stamp = timestamp_slug()
        try:
            artefacts["url"] = self.page.url
        except Exception:  # pragma: no cover
            pass
        if self.capture_screenshot_on_failure:
            try:
                screenshot = target / f"{stamp}.png"
                await self.page.screenshot(path=str(screenshot), full_page=False)
                artefacts["screenshot_path"] = str(screenshot)
            except Exception as exc:  # pragma: no cover - best effort
                LOGGER.debug("Screenshot capture failed: %s", exc)
        if self.capture_html_on_failure:
            try:
                html_path = target / f"{stamp}.html"
                content = await self.page.content()
                html_path.write_text(content, encoding="utf-8")
                artefacts["html_path"] = str(html_path)
            except Exception as exc:  # pragma: no cover - best effort
                LOGGER.debug("HTML capture failed: %s", exc)

        meta = target / f"{stamp}.txt"
        try:
            meta.write_text(
                "\n".join([
                    f"test_run_id={self.test_run_id}",
                    f"worker_id={self.worker_id}",
                    f"session_id={self.session_id}",
                    f"location={self.options.location_key}",
                    f"action={action}",
                    f"selector={selector}",
                    f"url={artefacts['url']}",
                    f"error={error}",
                    f"captured_at={utc_now().isoformat()}",
                ]) + "\n",
                encoding="utf-8")
        except Exception as exc:  # pragma: no cover
            LOGGER.debug("Failure metadata write failed: %s", exc)

        LOGGER.error("Captured failure artefacts for %s action=%s: %s",
                     self.worker_id, action, artefacts["screenshot_path"],
                     extra={"worker_id": self.worker_id, "action": action, "error": error})
        return artefacts

    async def screenshot(self, name: str) -> str:
        """Take an on-demand screenshot (used by the single-worker test mode)."""
        if self._page is None:
            return ""
        target = self.screenshot_directory / (self.test_run_id or "manual")
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"{name}-{timestamp_slug()}.png"
        try:
            await self.page.screenshot(path=str(path))
            return str(path)
        except Exception as exc:  # pragma: no cover
            LOGGER.debug("Screenshot failed: %s", exc)
            return ""

    # --------------------------------------------------------------- teardown
    async def close(self) -> None:
        """Close the page and its context."""
        if self._closed:
            return
        self._closed = True
        if self._page is not None:
            try:
                await self._page.close()
            except Exception as exc:  # pragma: no cover
                LOGGER.debug("Error closing page: %s", exc)
        if self._context is not None:
            await self.manager.close_context(self._context)
        self._page = None
        self._context = None
        LOGGER.debug("Session %s closed", self.session_id)

    async def __aenter__(self) -> "BrowserSession":
        await self.open()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()
