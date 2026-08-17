"""In-memory content library shared by the post and reply libraries."""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from app.utils.logger import get_logger
from app.utils.time_utils import iso

LOGGER = get_logger("content")


@dataclass
class ContentEntry:
    """A single post or reply available to the workers."""

    ref: str
    body: str
    category: str = "general"
    title: str = ""
    enabled: bool = True
    usage_count: int = 0
    last_used_at: str = ""
    source: str = ""

    @property
    def checksum(self) -> str:
        """Stable hash of the body, used for duplicate detection."""
        return hashlib.sha256(self.body.strip().encode("utf-8")).hexdigest()[:32]

    def preview(self, width: int = 60) -> str:
        """Single-line preview for CLI listings."""
        flat = " ".join(self.body.split())
        return flat if len(flat) <= width else flat[: width - 3] + "..."

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["checksum"] = self.checksum
        return data


class DuplicateContentError(ValueError):
    """Raised when adding an item whose body already exists in the library."""


class ContentLibrary:
    """Editable collection of content entries with import/export support."""

    library_name = "posts"
    ref_prefix = "POST"

    def __init__(self, entries: Sequence[ContentEntry] | None = None, *,
                 allow_duplicates: bool = False, max_size: int = 10_000) -> None:
        self._entries: dict[str, ContentEntry] = {}
        self._order: list[str] = []
        self._lock = threading.RLock()
        self.allow_duplicates = allow_duplicates
        self.max_size = max_size
        self.source_path: Path | None = None
        for entry in entries or []:
            self._insert(entry)

    # ---------------------------------------------------------------- helpers
    def _next_ref(self) -> str:
        with self._lock:
            index = len(self._order) + 1
            ref = f"{self.ref_prefix}-{index:03d}"
            while ref in self._entries:
                index += 1
                ref = f"{self.ref_prefix}-{index:03d}"
            return ref

    def _insert(self, entry: ContentEntry) -> ContentEntry:
        with self._lock:
            if len(self._order) >= self.max_size:
                raise ValueError(f"Content library limit reached ({self.max_size})")
            if not entry.ref:
                entry.ref = self._next_ref()
            if entry.ref in self._entries:
                entry.ref = self._next_ref()
            if not self.allow_duplicates:
                checksum = entry.checksum
                for existing in self._entries.values():
                    if existing.checksum == checksum:
                        raise DuplicateContentError(
                            f"Duplicate content already present as {existing.ref}")
            self._entries[entry.ref] = entry
            self._order.append(entry.ref)
            return entry

    # ------------------------------------------------------------------- CRUD
    def add(self, body: str, *, category: str = "general", title: str = "",
            ref: str = "", source: str = "", enabled: bool = True) -> ContentEntry:
        """Add one entry; raises :class:`DuplicateContentError` on repeats."""
        body = (body or "").strip()
        if not body:
            raise ValueError("Content body must not be empty")
        return self._insert(ContentEntry(ref=ref, body=body, category=category or "general",
                                         title=title, source=source, enabled=enabled))

    def add_many(self, bodies: Iterable[str], *, category: str = "general",
                 source: str = "") -> int:
        """Bulk-add, skipping duplicates.  Returns the number added."""
        added = 0
        for body in bodies:
            try:
                self.add(body, category=category, source=source)
                added += 1
            except (DuplicateContentError, ValueError) as exc:
                LOGGER.debug("Skipping content item: %s", exc)
        return added

    def edit(self, ref: str, *, body: str | None = None, category: str | None = None,
             title: str | None = None) -> ContentEntry:
        """Edit an existing entry in place."""
        with self._lock:
            entry = self.get(ref)
            if body is not None:
                cleaned = body.strip()
                if not cleaned:
                    raise ValueError("Content body must not be empty")
                entry.body = cleaned
            if category is not None:
                entry.category = category or "general"
            if title is not None:
                entry.title = title
            return entry

    def delete(self, ref: str) -> None:
        """Remove an entry."""
        with self._lock:
            if ref not in self._entries:
                raise KeyError(f"Unknown content ref: {ref}")
            del self._entries[ref]
            self._order.remove(ref)

    def set_enabled(self, ref: str, enabled: bool) -> ContentEntry:
        """Enable or disable an entry without deleting it."""
        entry = self.get(ref)
        entry.enabled = enabled
        return entry

    def get(self, ref: str) -> ContentEntry:
        """Return the entry for *ref*."""
        try:
            return self._entries[ref]
        except KeyError as exc:
            raise KeyError(f"Unknown content ref: {ref}") from exc

    def mark_used(self, ref: str) -> None:
        """Increment usage counters for *ref*."""
        with self._lock:
            entry = self._entries.get(ref)
            if entry is None:
                return
            entry.usage_count += 1
            entry.last_used_at = iso()

    # ------------------------------------------------------------------ views
    def __len__(self) -> int:
        return len(self._order)

    def __iter__(self) -> Iterator[ContentEntry]:
        return iter(self.all())

    def all(self) -> list[ContentEntry]:
        """Every entry in insertion order."""
        with self._lock:
            return [self._entries[ref] for ref in self._order]

    def enabled(self, category: str | None = None) -> list[ContentEntry]:
        """Enabled entries, optionally filtered by category."""
        items = [entry for entry in self.all() if entry.enabled]
        if category:
            items = [entry for entry in items if entry.category == category]
        return items

    def categories(self) -> list[str]:
        """Sorted list of the categories present in the library."""
        return sorted({entry.category for entry in self.all()})

    def stats(self) -> dict[str, object]:
        """Summary used by the CLI content manager."""
        entries = self.all()
        return {
            "library": self.library_name,
            "total": len(entries),
            "enabled": sum(1 for entry in entries if entry.enabled),
            "disabled": sum(1 for entry in entries if not entry.enabled),
            "categories": self.categories(),
            "total_uses": sum(entry.usage_count for entry in entries),
            "source": str(self.source_path) if self.source_path else "",
        }

    # ------------------------------------------------------------------- I/O
    def clear(self) -> None:
        """Drop every entry."""
        with self._lock:
            self._entries.clear()
            self._order.clear()

    def load_entries(self, entries: Sequence[ContentEntry], *, replace: bool = True) -> int:
        """Load *entries*, replacing the current contents by default."""
        if replace:
            self.clear()
        loaded = 0
        for entry in entries:
            try:
                self._insert(ContentEntry(
                    ref="" if replace else entry.ref, body=entry.body,
                    category=entry.category, title=entry.title,
                    enabled=entry.enabled, source=entry.source,
                ))
                loaded += 1
            except DuplicateContentError as exc:
                LOGGER.debug("Duplicate skipped during load: %s", exc)
        return loaded

    def save_json(self, path: str | Path) -> Path:
        """Persist the library as JSON (preserves categories and flags)."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "library": self.library_name,
            "exported_at": iso(),
            "items": [entry.to_dict() for entry in self.all()],
        }
        target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return target

    def save_txt(self, path: str | Path) -> Path:
        """Persist the library as a plain one-item-per-line text file."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        lines = [" ".join(entry.body.split()) for entry in self.all() if entry.enabled]
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return target
