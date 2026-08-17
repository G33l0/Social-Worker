"""Content selection strategies and per-worker usage history."""

from __future__ import annotations

import random
import threading
from collections import defaultdict
from typing import Sequence

from app.content.library import ContentEntry, ContentLibrary
from app.utils.config import ContentSelectionMode
from app.utils.logger import get_logger

LOGGER = get_logger("content.rotation")


class ContentExhausted(RuntimeError):
    """Raised when no eligible content remains for a worker."""


class ContentRotator:
    """Selects content according to the operator's rotation policy.

    * ``RANDOM`` - uniform random pick every time.
    * ``SEQUENTIAL`` - walk the library in order, wrapping at the end.
    * ``RANDOM_WITHOUT_REPETITION`` - shuffle and exhaust before reshuffling.
    * ``CATEGORY_BASED`` - restrict the pool to a requested category.

    Duplicate prevention can be applied per worker (default) or globally.
    """

    def __init__(self, library: ContentLibrary, *,
                 mode: ContentSelectionMode = ContentSelectionMode.RANDOM,
                 prevent_duplicates_per_worker: bool = True,
                 prevent_global_duplicates: bool = False,
                 rng: random.Random | None = None) -> None:
        self.library = library
        self.mode = mode
        self.prevent_duplicates_per_worker = prevent_duplicates_per_worker
        self.prevent_global_duplicates = prevent_global_duplicates
        self._rng = rng or random.Random()
        self._lock = threading.RLock()
        self._cursor = 0
        self._worker_history: dict[str, list[str]] = defaultdict(list)
        self._global_used: set[str] = set()
        self._shuffled: dict[str, list[str]] = {}

    # ------------------------------------------------------------------ pools
    def _pool(self, category: str | None) -> list[ContentEntry]:
        if self.mode == ContentSelectionMode.CATEGORY_BASED and category:
            entries = self.library.enabled(category=category)
            if entries:
                return entries
            LOGGER.warning("Category %r empty; falling back to full library", category)
        elif category:
            entries = self.library.enabled(category=category)
            if entries:
                return entries
        return self.library.enabled()

    def _eligible(self, pool: Sequence[ContentEntry], worker_id: str) -> list[ContentEntry]:
        used_by_worker = set(self._worker_history.get(worker_id, []))
        candidates = list(pool)
        if self.prevent_global_duplicates:
            candidates = [entry for entry in candidates if entry.ref not in self._global_used]
        if self.prevent_duplicates_per_worker:
            fresh = [entry for entry in candidates if entry.ref not in used_by_worker]
            if fresh:
                return fresh
            # Worker has seen everything: recycle its history so the test keeps
            # running rather than stalling mid-run.
            self._worker_history[worker_id] = []
            LOGGER.debug("Recycled content history for worker %s", worker_id)
        return candidates

    # --------------------------------------------------------------- selection
    def select(self, worker_id: str, *, category: str | None = None) -> ContentEntry:
        """Return the next content entry for *worker_id*."""
        with self._lock:
            pool = self._pool(category)
            if not pool:
                raise ContentExhausted(
                    f"No enabled content available in the {self.library.library_name} library")
            candidates = self._eligible(pool, worker_id)
            if not candidates:
                raise ContentExhausted(
                    f"All {self.library.library_name} content already used "
                    "(global duplicate prevention is enabled)")

            if self.mode == ContentSelectionMode.SEQUENTIAL:
                entry = candidates[self._cursor % len(candidates)]
                self._cursor = (self._cursor + 1) % max(1, len(candidates))
            elif self.mode == ContentSelectionMode.RANDOM_WITHOUT_REPETITION:
                entry = self._next_shuffled(candidates, category or "*")
            else:  # RANDOM and CATEGORY_BASED both pick uniformly from the pool
                entry = self._rng.choice(candidates)

            self._record(worker_id, entry)
            return entry

    def _next_shuffled(self, candidates: Sequence[ContentEntry], bucket: str) -> ContentEntry:
        available = {entry.ref: entry for entry in candidates}
        queue = [ref for ref in self._shuffled.get(bucket, []) if ref in available]
        if not queue:
            queue = list(available)
            self._rng.shuffle(queue)
        ref = queue.pop(0)
        self._shuffled[bucket] = queue
        return available[ref]

    def _record(self, worker_id: str, entry: ContentEntry) -> None:
        self._worker_history[worker_id].append(entry.ref)
        self._global_used.add(entry.ref)
        self.library.mark_used(entry.ref)

    # ------------------------------------------------------------------ state
    def history(self, worker_id: str) -> list[str]:
        """Refs already used by *worker_id*."""
        return list(self._worker_history.get(worker_id, []))

    def reset_worker(self, worker_id: str) -> None:
        """Forget a worker's history."""
        with self._lock:
            self._worker_history.pop(worker_id, None)

    def reset(self) -> None:
        """Forget every worker's history and any global usage."""
        with self._lock:
            self._worker_history.clear()
            self._global_used.clear()
            self._shuffled.clear()
            self._cursor = 0

    def stats(self) -> dict[str, object]:
        """Rotation statistics for reporting."""
        with self._lock:
            return {
                "mode": self.mode.value,
                "library": self.library.library_name,
                "library_size": len(self.library),
                "enabled_items": len(self.library.enabled()),
                "workers_tracked": len(self._worker_history),
                "unique_items_used": len(self._global_used),
                "total_selections": sum(len(v) for v in self._worker_history.values()),
                "prevent_duplicates_per_worker": self.prevent_duplicates_per_worker,
                "prevent_global_duplicates": self.prevent_global_duplicates,
            }
