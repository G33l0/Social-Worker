"""Forum selection policy (random / specific / rotate / weighted)."""

from __future__ import annotations

import random
import threading
from dataclasses import dataclass, field
from typing import Any, Sequence

from app.site.base_adapter import ForumRef
from app.utils.config import ForumConfig, ForumSelectionMode
from app.utils.logger import get_logger
from app.utils.validation import validate_weights

LOGGER = get_logger("forums")


@dataclass
class ForumStats:
    """Per-forum activity counters."""

    visits: int = 0
    posts: int = 0
    replies: int = 0
    errors: int = 0
    load_ms_total: float = 0.0
    load_samples: int = 0
    weight: float = 0.0

    @property
    def avg_load_ms(self) -> float:
        """Mean forum page load time."""
        return round(self.load_ms_total / self.load_samples, 2) if self.load_samples else 0.0


class ForumSelector:
    """Chooses which forum a worker enters, honouring the configured mode."""

    def __init__(self, config: ForumConfig | None = None, *,
                 rng: random.Random | None = None) -> None:
        self.config = config or ForumConfig()
        self._rng = rng or random.Random()
        self._lock = threading.Lock()
        self._rotation_index = 0
        self._worker_rotation: dict[str, int] = {}
        self.stats: dict[str, ForumStats] = {}
        self.discovered: list[ForumRef] = []

    # ------------------------------------------------------------- discovery
    def register(self, forums: Sequence[ForumRef]) -> list[ForumRef]:
        """Record the forums discovered on the site and apply the allow-list."""
        allowed = {key.lower() for key in self.config.allowed_forums}
        selected = [forum for forum in forums
                    if not allowed or forum.key.lower() in allowed]
        with self._lock:
            self.discovered = selected
            weights = self._normalised_weights({forum.key for forum in selected})
            for forum in selected:
                entry = self.stats.setdefault(forum.key, ForumStats())
                entry.weight = weights.get(forum.key, 0.0)
        if allowed and len(selected) != len(forums):
            LOGGER.info("Forum allow-list active: using %d of %d discovered forum(s)",
                        len(selected), len(forums))
        return selected

    def _normalised_weights(self, available: set[str]) -> dict[str, float]:
        raw = {key.lower(): value for key, value in self.config.weights.items()
               if key.lower() in {name.lower() for name in available}}
        if not raw:
            # Nothing configured for these forums: treat them as equally likely.
            return {key: 1.0 / len(available) for key in available} if available else {}
        if sum(raw.values()) <= 0:
            # Every candidate is weighted zero. That is an instruction not to
            # use them, not a reason to crash - the caller sees no selection.
            return {key: 0.0 for key in available}
        normalised = validate_weights(raw)
        return {key: normalised.get(key.lower(), 0.0) for key in available}

    # ------------------------------------------------------------- selection
    def select(self, worker_id: str = "", *,
               forums: Sequence[ForumRef] | None = None,
               override: str = "") -> ForumRef | None:
        """Return the forum this worker should enter next."""
        pool = list(forums if forums is not None else self.discovered)
        if not pool:
            return None

        if override:
            match = self._find(pool, override)
            if match:
                return match
            LOGGER.warning("Requested forum %r not available; using selection mode",
                           override)

        mode = self.config.mode
        if mode is ForumSelectionMode.SPECIFIC:
            match = self._find(pool, self.config.specific_forum)
            if match:
                return match
            LOGGER.warning("Configured forum %r not found; falling back to random",
                           self.config.specific_forum)
            return self._rng.choice(pool)

        if mode is ForumSelectionMode.ROTATE:
            order = self.config.rotation_order or [forum.key for forum in pool]
            with self._lock:
                index = self._worker_rotation.get(worker_id, self._rotation_index)
                self._worker_rotation[worker_id] = index + 1
                self._rotation_index += 1
            for offset in range(len(order)):
                match = self._find(pool, order[(index + offset) % len(order)])
                if match:
                    return match
            return pool[index % len(pool)]

        if mode is ForumSelectionMode.WEIGHTED:
            weights = self._normalised_weights({forum.key for forum in pool})
            values = [max(0.0, weights.get(forum.key, 0.0)) for forum in pool]
            if sum(values) <= 0:
                LOGGER.info("Every available forum is weighted zero (%s); no forum "
                            "selected", ", ".join(forum.key for forum in pool))
                return None
            return self._rng.choices(pool, weights=values, k=1)[0]

        return self._rng.choice(pool)

    def _find(self, pool: Sequence[ForumRef], key: str) -> ForumRef | None:
        target = (key or "").strip().lower()
        if not target:
            return None
        for forum in pool:
            if forum.key.lower() == target or forum.name.strip().lower() == target:
                return forum
        return None

    # --------------------------------------------------------------- counters
    def record_visit(self, forum_key: str, load_ms: float = 0.0) -> None:
        """Count a forum visit and its load time."""
        with self._lock:
            entry = self.stats.setdefault(forum_key, ForumStats())
            entry.visits += 1
            if load_ms:
                entry.load_ms_total += load_ms
                entry.load_samples += 1

    def record_post(self, forum_key: str) -> None:
        """Count a post published in *forum_key*."""
        with self._lock:
            self.stats.setdefault(forum_key, ForumStats()).posts += 1

    def record_reply(self, forum_key: str) -> None:
        """Count a reply published in *forum_key*."""
        with self._lock:
            self.stats.setdefault(forum_key, ForumStats()).replies += 1

    def record_error(self, forum_key: str) -> None:
        """Count an error that occurred in *forum_key*."""
        with self._lock:
            self.stats.setdefault(forum_key, ForumStats()).errors += 1

    def rows(self) -> list[dict[str, Any]]:
        """Forum activity rows for the forum report."""
        with self._lock:
            names = {forum.key: forum for forum in self.discovered}
            return [
                {
                    "key": key,
                    "name": names[key].name if key in names else key,
                    "url": names[key].url if key in names else "",
                    "weight": round(entry.weight, 4),
                    "visits": entry.visits,
                    "posts": entry.posts,
                    "replies": entry.replies,
                    "errors": entry.errors,
                    "avg_load_ms": entry.avg_load_ms,
                }
                for key, entry in sorted(self.stats.items())
            ]

    def distribution(self) -> dict[str, float]:
        """Observed visit distribution, for comparison with the configured weights."""
        with self._lock:
            total = sum(entry.visits for entry in self.stats.values())
            if not total:
                return {}
            return {key: round(entry.visits / total, 4)
                    for key, entry in sorted(self.stats.items())}
