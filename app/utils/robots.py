"""robots.txt compliance check.

Social Worker is a load-testing client, and a load-testing client that ignores
a site's own crawl policy is badly behaved even when the operator owns the
site.  When ``safety.honour_robots_txt`` is on, the target's robots.txt is
fetched during preflight and a disallowed path blocks the run.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

from app.utils.logger import get_logger

LOGGER = get_logger("robots")

DEFAULT_TIMEOUT = 10.0


@dataclass
class RobotsVerdict:
    """Outcome of the robots.txt check."""

    allowed: bool = True
    checked: bool = False
    robots_url: str = ""
    reason: str = ""
    crawl_delay: float | None = None

    def as_problem(self) -> str:
        """The preflight message when the target disallows this path."""
        return (f"robots.txt at {self.robots_url} disallows automated access to "
                f"{self.reason}. Adjust the site's robots.txt, or set "
                "safety.honour_robots_txt to false if you own the target and "
                "have decided this policy does not apply to your own test client.")


def check(url: str, *, user_agent: str = "SocialWorker-LoadTest",
          timeout: float = DEFAULT_TIMEOUT) -> RobotsVerdict:
    """Return whether *url* may be fetched by *user_agent* per robots.txt.

    A missing or unreachable robots.txt is treated as "allowed" (that is the
    convention), but the outcome is recorded so the operator can see that the
    check did not actually run.
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return RobotsVerdict(allowed=True, checked=False, reason="unsupported URL")

    robots_url = urljoin(f"{parsed.scheme}://{parsed.netloc}", "/robots.txt")
    verdict = RobotsVerdict(robots_url=robots_url)
    parser = RobotFileParser()
    parser.set_url(robots_url)

    try:
        request = urllib.request.Request(robots_url,
                                         headers={"User-Agent": user_agent})
        # Deliberately bypass any ambient proxy: the check must describe the
        # target the workers will actually hit.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
        parser.parse(body.splitlines())
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            verdict.checked = True
            verdict.allowed = False
            verdict.reason = f"{parsed.path or '/'} (robots.txt returned HTTP {exc.code})"
            return verdict
        LOGGER.info("robots.txt not available (HTTP %s); continuing", exc.code)
        verdict.reason = f"robots.txt unavailable (HTTP {exc.code})"
        return verdict
    except Exception as exc:  # network error, timeout, bad TLS
        LOGGER.info("robots.txt could not be fetched (%s); continuing", exc)
        verdict.reason = f"robots.txt unreachable ({exc})"
        return verdict

    verdict.checked = True
    path = parsed.path or "/"
    verdict.allowed = parser.can_fetch(user_agent, url)
    if not verdict.allowed:
        verdict.reason = path
    try:
        delay = parser.crawl_delay(user_agent)
        verdict.crawl_delay = float(delay) if delay is not None else None
    except Exception:  # pragma: no cover - parser dependent
        verdict.crawl_delay = None
    LOGGER.info("robots.txt check for %s: %s", url,
                "allowed" if verdict.allowed else "DISALLOWED")
    return verdict
