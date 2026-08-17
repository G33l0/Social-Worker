"""JSON report writer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.utils.time_utils import iso


def write_json(data: Any, path: str | Path, *, metadata: dict[str, Any] | None = None) -> Path:
    """Write *data* to *path* as pretty-printed JSON."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "generated_at": iso(),
        "generator": "Social Worker",
    }
    if metadata:
        payload.update(metadata)
    payload["data"] = data
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str),
                      encoding="utf-8")
    return target
