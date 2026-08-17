"""Structured logging for Social Worker.

Four log sinks are maintained, as required by the operations spec:

``logs/application.log``  controller / CLI / scheduler activity
``logs/workers.log``      per-worker action records
``logs/browser.log``      browser + network diagnostics
``logs/errors.log``       WARNING and above, from every logger

Every record carries the test-run id, worker id, location, action, result and
duration when the caller supplies them.  A redaction filter strips anything
resembling a credential, cookie or token before it reaches disk.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
from pathlib import Path
from typing import Any, Iterable

DEFAULT_LOG_DIR = Path("logs")

APP_LOGGER = "social_worker"
WORKER_LOGGER = "social_worker.workers"
BROWSER_LOGGER = "social_worker.browser"

_CONFIGURED = False

_REDACTION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(password|passwd|pwd)\s*[=:]\s*\S+"), r"\1=***REDACTED***"),
    (re.compile(r"(?i)\b(api[_-]?key|apikey|secret|token|bearer)\s*[=:]\s*\S+"),
     r"\1=***REDACTED***"),
    (re.compile(r"(?i)\b(authorization|cookie|set-cookie)\s*:\s*[^\r\n]+"),
     r"\1: ***REDACTED***"),
    (re.compile(r"(?i)\b(session[_-]?id|sessid|csrf[_-]?token)\s*[=:]\s*\S+"),
     r"\1=***REDACTED***"),
)

_CONTEXT_FIELDS = ("test_run_id", "worker_id", "location", "action", "result",
                   "duration_ms", "error")


class RedactionFilter(logging.Filter):
    """Remove credential-like substrings from every emitted record."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D102
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            return True
        cleaned = redact(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = ()
        for field in ("error",):
            value = getattr(record, field, None)
            if isinstance(value, str):
                setattr(record, field, redact(value))
        return True


class ContextFormatter(logging.Formatter):
    """Formatter that appends structured key=value context to each line."""

    default_time_format = "%Y-%m-%dT%H:%M:%S"

    def format(self, record: logging.LogRecord) -> str:  # noqa: D102
        base = super().format(record)
        extras = []
        for field in _CONTEXT_FIELDS:
            value = getattr(record, field, None)
            if value is None or value == "":
                continue
            if field == "duration_ms":
                extras.append(f"duration_ms={float(value):.1f}")
            else:
                extras.append(f"{field}={value}")
        if extras:
            base = f"{base} | " + " ".join(extras)
        return base


def redact(text: str) -> str:
    """Apply all redaction patterns to *text*."""
    for pattern, replacement in _REDACTION_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _handler(path: Path, level: int, formatter: logging.Formatter,
             max_bytes: int, backups: int) -> logging.Handler:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
    )
    handler.setLevel(level)
    handler.setFormatter(formatter)
    handler.addFilter(RedactionFilter())
    return handler


def _only(names: Iterable[str]) -> logging.Filter:
    prefixes = tuple(names)

    class _NameFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            return record.name.startswith(prefixes)

    return _NameFilter()


def configure_logging(log_dir: str | Path = DEFAULT_LOG_DIR, *,
                      level: str = "INFO",
                      console: bool = True,
                      console_level: str = "WARNING",
                      max_bytes: int = 10 * 1024 * 1024,
                      backups: int = 5,
                      force: bool = False) -> None:
    """Install the Social Worker logging configuration (idempotent)."""
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)

    numeric = getattr(logging, str(level).upper(), logging.INFO)
    formatter = ContextFormatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    )

    root = logging.getLogger(APP_LOGGER)
    root.setLevel(numeric)
    root.propagate = False
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    application = _handler(directory / "application.log", numeric, formatter, max_bytes, backups)
    root.addHandler(application)

    errors = _handler(directory / "errors.log", logging.WARNING, formatter, max_bytes, backups)
    root.addHandler(errors)

    workers = logging.getLogger(WORKER_LOGGER)
    workers.setLevel(numeric)
    workers.propagate = True
    for handler in list(workers.handlers):
        workers.removeHandler(handler)
        handler.close()
    worker_handler = _handler(directory / "workers.log", numeric, formatter, max_bytes, backups)
    worker_handler.addFilter(_only([WORKER_LOGGER]))
    workers.addHandler(worker_handler)

    browser = logging.getLogger(BROWSER_LOGGER)
    browser.setLevel(numeric)
    browser.propagate = True
    for handler in list(browser.handlers):
        browser.removeHandler(handler)
        handler.close()
    browser_handler = _handler(directory / "browser.log", numeric, formatter, max_bytes, backups)
    browser_handler.addFilter(_only([BROWSER_LOGGER]))
    browser.addHandler(browser_handler)

    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setLevel(getattr(logging, str(console_level).upper(), logging.WARNING))
        stream.setFormatter(logging.Formatter("%(levelname)-8s %(message)s"))
        stream.addFilter(RedactionFilter())
        root.addHandler(stream)

    _CONFIGURED = True


def get_logger(name: str = APP_LOGGER) -> logging.Logger:
    """Return a namespaced Social Worker logger."""
    if name == APP_LOGGER or name.startswith(APP_LOGGER + "."):
        return logging.getLogger(name)
    return logging.getLogger(f"{APP_LOGGER}.{name}")


def worker_logger(worker_id: str) -> logging.LoggerAdapter:
    """Return a logger adapter that stamps every record with *worker_id*."""
    return logging.LoggerAdapter(logging.getLogger(WORKER_LOGGER), {"worker_id": worker_id})


def log_action(logger: logging.Logger | logging.LoggerAdapter, message: str,
               *, level: int = logging.INFO, **context: Any) -> None:
    """Log *message* with structured Social Worker context fields."""
    extra = {key: value for key, value in context.items() if key in _CONTEXT_FIELDS}
    logger.log(level, message, extra=extra)
