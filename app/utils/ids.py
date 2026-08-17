"""Identifier generation for test runs, workers and sessions."""

from __future__ import annotations

import itertools
import threading
import uuid

from app.utils.time_utils import date_slug

_lock = threading.Lock()
_worker_counter = itertools.count(1)


def new_test_run_id(sequence: int = 1, *, prefix: str = "TEST") -> str:
    """Build a test-run identifier such as ``TEST-2026-08-17-0001``."""
    return f"{prefix}-{date_slug()}-{sequence:04d}"


def new_worker_id(prefix: str = "SW", width: int = 5) -> str:
    """Allocate the next sequential worker identifier (``SW-00001``)."""
    with _lock:
        number = next(_worker_counter)
    return f"{prefix}-{number:0{width}d}"


def reset_worker_ids(start: int = 1) -> None:
    """Reset the worker-id counter (used by tests and by fresh test runs)."""
    global _worker_counter
    with _lock:
        _worker_counter = itertools.count(start)


def new_session_id() -> str:
    """Return a unique session identifier."""
    return f"SES-{uuid.uuid4().hex[:16]}"


def new_event_id() -> str:
    """Return a unique event identifier."""
    return uuid.uuid4().hex
