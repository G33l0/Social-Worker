"""CSV report writer."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Sequence


def write_csv(rows: Sequence[dict[str, Any]], path: str | Path, *,
              columns: Sequence[str] | None = None) -> Path:
    """Write *rows* to *path* as CSV, inferring the header when needed."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        target.write_text("", encoding="utf-8")
        return target

    header: list[str] = list(columns) if columns else []
    if not header:
        seen: dict[str, None] = {}
        for row in rows:
            for key in row:
                seen.setdefault(key, None)
        header = list(seen)

    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=header, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _flatten(row.get(key, "")) for key in header})
    return target


def _flatten(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return str(value)
    return value
