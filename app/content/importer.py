"""Import post/reply content from TXT, CSV and JSON files."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterable, Sequence

from app.content.library import ContentEntry
from app.utils.logger import get_logger

LOGGER = get_logger("content.importer")

SUPPORTED_SUFFIXES = {".txt", ".csv", ".json"}


class ImportError_(ValueError):
    """Raised when a content file cannot be parsed."""


def detect_format(path: str | Path) -> str:
    """Return ``txt``/``csv``/``json`` based on the file suffix."""
    suffix = Path(path).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ImportError_(
            f"Unsupported content format {suffix!r}; use one of "
            + ", ".join(sorted(SUPPORTED_SUFFIXES))
        )
    return suffix.lstrip(".")


def import_txt(path: str | Path, *, category: str = "general",
               paragraph_mode: bool = False) -> list[ContentEntry]:
    """Import a plain-text library.

    One item per line by default.  With ``paragraph_mode`` the file is split on
    blank lines instead, allowing multi-line posts.  Lines starting with ``#``
    are treated as comments.
    """
    text = Path(path).read_text(encoding="utf-8")
    if paragraph_mode:
        chunks = [chunk.strip() for chunk in text.split("\n\n")]
    else:
        chunks = [line.strip() for line in text.splitlines()]
    entries: list[ContentEntry] = []
    for chunk in chunks:
        if not chunk or chunk.startswith("#"):
            continue
        entries.append(ContentEntry(ref="", body=chunk, category=category,
                                    source=str(path)))
    return entries


def import_csv(path: str | Path, *, category: str = "general") -> list[ContentEntry]:
    """Import a CSV library.

    Recognised columns (case-insensitive, all optional except the body):
    ``id``/``ref``, ``category``, ``title``, ``body``/``text``/``content``/``post``/``reply``,
    ``enabled``.
    A headerless single-column CSV is also accepted.
    """
    raw = Path(path).read_text(encoding="utf-8")
    if not raw.strip():
        return []
    has_header = _looks_like_header(raw)

    entries: list[ContentEntry] = []
    if has_header:
        reader = csv.DictReader(raw.splitlines())
        for row in reader:
            normalised = {(key or "").strip().lower(): (value or "").strip()
                          for key, value in row.items()}
            body = _first(normalised, ("body", "text", "content", "post", "reply", "message"))
            if not body:
                continue
            enabled_raw = normalised.get("enabled", "true").lower()
            entries.append(ContentEntry(
                ref=_first(normalised, ("id", "ref")) or "",
                body=body,
                category=normalised.get("category") or category,
                title=normalised.get("title", ""),
                enabled=enabled_raw not in {"0", "false", "no", "off"},
                source=str(path),
            ))
    else:
        for row in csv.reader(raw.splitlines()):
            if not row:
                continue
            body = row[-1].strip() if len(row) == 1 else row[-1].strip()
            if not body or body.startswith("#"):
                continue
            entries.append(ContentEntry(ref="", body=body, category=category,
                                        source=str(path)))
    return entries


def import_json(path: str | Path, *, category: str = "general") -> list[ContentEntry]:
    """Import a JSON library.

    Accepted shapes::

        ["post one", "post two"]
        [{"id": "P1", "body": "...", "category": "general"}]
        {"items": [...]}  /  {"posts": [...]}  /  {"replies": [...]}
    """
    data: Any = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        for key in ("items", "posts", "replies", "content", "data"):
            if key in data:
                data = data[key]
                break
        else:
            raise ImportError_(
                "JSON object must contain an 'items', 'posts' or 'replies' array")
    if not isinstance(data, list):
        raise ImportError_("JSON content must be an array of strings or objects")

    entries: list[ContentEntry] = []
    for element in data:
        if isinstance(element, str):
            body = element.strip()
            if body:
                entries.append(ContentEntry(ref="", body=body, category=category,
                                            source=str(path)))
            continue
        if not isinstance(element, dict):
            continue
        lowered = {str(key).lower(): value for key, value in element.items()}
        body = str(_first(lowered, ("body", "text", "content", "post", "reply",
                                    "message")) or "").strip()
        if not body:
            continue
        entries.append(ContentEntry(
            ref=str(_first(lowered, ("id", "ref")) or ""),
            body=body,
            category=str(lowered.get("category") or category),
            title=str(lowered.get("title") or ""),
            enabled=bool(lowered.get("enabled", True)),
            source=str(path),
        ))
    return entries


def import_file(path: str | Path, *, category: str = "general",
                paragraph_mode: bool = False) -> list[ContentEntry]:
    """Import any supported content file and return the parsed entries."""
    target = Path(path)
    if not target.exists():
        raise ImportError_(f"Content file not found: {target}")
    fmt = detect_format(target)
    if fmt == "txt":
        entries = import_txt(target, category=category, paragraph_mode=paragraph_mode)
    elif fmt == "csv":
        entries = import_csv(target, category=category)
    else:
        entries = import_json(target, category=category)
    LOGGER.info("Imported %d item(s) from %s", len(entries), target)
    return entries


def import_directory(directory: str | Path, *, category: str = "general") -> list[ContentEntry]:
    """Import every supported file inside *directory*."""
    entries: list[ContentEntry] = []
    for path in sorted(Path(directory).iterdir()):
        if path.suffix.lower() in SUPPORTED_SUFFIXES:
            entries.extend(import_file(path, category=category))
    return entries


def export_entries(entries: Sequence[ContentEntry], path: str | Path,
                   fmt: str | None = None) -> Path:
    """Write *entries* to *path* in txt/csv/json format."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fmt = (fmt or detect_format(target)).lower()
    if fmt == "txt":
        target.write_text(
            "\n".join(" ".join(entry.body.split()) for entry in entries) + "\n",
            encoding="utf-8")
    elif fmt == "csv":
        with target.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["id", "category", "title", "body", "enabled"])
            for entry in entries:
                writer.writerow([entry.ref, entry.category, entry.title,
                                 entry.body, "true" if entry.enabled else "false"])
    else:
        target.write_text(
            json.dumps([entry.to_dict() for entry in entries], indent=2,
                       ensure_ascii=False), encoding="utf-8")
    return target


KNOWN_COLUMNS = {"id", "ref", "category", "title", "body", "text", "content",
                 "post", "reply", "message", "enabled"}


def _looks_like_header(raw: str) -> bool:
    """True when the first CSV row names known columns rather than holding data."""
    for row in csv.reader(raw.splitlines()):
        if not row:
            continue
        cells = {cell.strip().lower() for cell in row}
        return bool(cells & KNOWN_COLUMNS)
    return False


def _first(mapping: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if mapping.get(key):
            return mapping[key]
    return None
