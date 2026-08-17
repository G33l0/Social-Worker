"""Self-contained HTML report writer (no external assets or CDNs)."""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.utils.time_utils import iso

_STYLE = """
:root { color-scheme: light dark; --bg:#ffffff; --fg:#16181d; --muted:#5c6370;
        --line:#e2e5ea; --accent:#2f6feb; --ok:#1a7f4b; --warn:#b06000; --err:#b3261e;
        --card:#f7f8fa; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#14161a; --fg:#e6e8ec; --muted:#9aa1ad; --line:#2a2e35;
          --accent:#6ea8ff; --ok:#4ec98a; --warn:#e0a34a; --err:#ff8a80; --card:#1c1f25; }
}
* { box-sizing: border-box; }
body { margin:0; padding:2rem 1.25rem 4rem; background:var(--bg); color:var(--fg);
       font:15px/1.55 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; }
.wrap { max-width: 1100px; margin: 0 auto; }
h1 { font-size:1.6rem; margin:0 0 .25rem; }
h2 { font-size:1.15rem; margin:2.25rem 0 .75rem; padding-bottom:.35rem;
     border-bottom:1px solid var(--line); }
.sub { color:var(--muted); margin:0 0 1.5rem; font-size:.9rem; }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:.75rem; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px;
        padding:.85rem 1rem; }
.card .label { color:var(--muted); font-size:.75rem; text-transform:uppercase;
               letter-spacing:.04em; }
.card .value { font-size:1.5rem; font-weight:600; margin-top:.15rem; }
.tablewrap { overflow-x:auto; border:1px solid var(--line); border-radius:10px; }
table { border-collapse:collapse; width:100%; font-size:.9rem; }
th,td { padding:.5rem .7rem; text-align:left; border-bottom:1px solid var(--line);
        white-space:nowrap; }
th { background:var(--card); font-weight:600; }
tr:last-child td { border-bottom:none; }
td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
.badge { display:inline-block; padding:.1rem .45rem; border-radius:99px; font-size:.75rem;
         border:1px solid var(--line); }
.ok { color:var(--ok); } .warn { color:var(--warn); } .err { color:var(--err); }
footer { margin-top:3rem; color:var(--muted); font-size:.8rem; }
.notice { background:var(--card); border-left:3px solid var(--accent); padding:.75rem 1rem;
          border-radius:6px; margin:1rem 0; font-size:.9rem; }
"""

_NUMERIC_HINTS = ("count", "ms", "posts", "replies", "errors", "sessions", "requests",
                  "workers", "visits", "uses", "rate", "duration", "active", "p50",
                  "p90", "p95", "p99", "avg", "min", "max")


def _is_numeric_column(name: str) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in _NUMERIC_HINTS)


def _table(rows: Sequence[Mapping[str, Any]], *, columns: Sequence[str] | None = None) -> str:
    if not rows:
        return '<p class="sub">No data recorded.</p>'
    header: list[str] = list(columns) if columns else []
    if not header:
        seen: dict[str, None] = {}
        for row in rows:
            for key in row:
                seen.setdefault(key, None)
        header = list(seen)

    head = "".join(
        f'<th class="{"num" if _is_numeric_column(name) else ""}">'
        f"{html.escape(name.replace('_', ' ').title())}</th>"
        for name in header)
    body_rows = []
    for row in rows:
        cells = []
        for name in header:
            value = row.get(name, "")
            css = "num" if _is_numeric_column(name) else ""
            cells.append(f'<td class="{css}">{html.escape(str(value))}</td>')
        body_rows.append("<tr>" + "".join(cells) + "</tr>")
    return ('<div class="tablewrap"><table><thead><tr>' + head
            + "</tr></thead><tbody>" + "".join(body_rows) + "</tbody></table></div>")


def _cards(summary: Mapping[str, Any]) -> str:
    cards = []
    for label, value in summary.items():
        cards.append(
            f'<div class="card"><div class="label">'
            f'{html.escape(str(label).replace("_", " "))}</div>'
            f'<div class="value">{html.escape(str(value))}</div></div>')
    return '<div class="cards">' + "".join(cards) + "</div>"


def write_html(title: str, sections: Sequence[tuple[str, Any]], path: str | Path, *,
               summary: Mapping[str, Any] | None = None,
               subtitle: str = "", notice: str = "") -> Path:
    """Render a report page.

    *sections* is a sequence of ``(heading, payload)`` pairs where the payload is
    either a list of row mappings (rendered as a table) or a mapping (rendered as
    a key/value table).
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    blocks: list[str] = []
    if summary:
        blocks.append(_cards(summary))
    if notice:
        blocks.append(f'<div class="notice">{html.escape(notice)}</div>')

    for heading, payload in sections:
        blocks.append(f"<h2>{html.escape(heading)}</h2>")
        if isinstance(payload, Mapping):
            rows = [{"metric": key, "value": value} for key, value in payload.items()]
            blocks.append(_table(rows, columns=["metric", "value"]))
        elif isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
            blocks.append(_table(list(payload)))
        else:
            blocks.append(f"<p>{html.escape(str(payload))}</p>")

    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title><style>{_STYLE}</style></head>
<body><div class="wrap">
<h1>{html.escape(title)}</h1>
<p class="sub">{html.escape(subtitle)} &middot; generated {html.escape(iso())}</p>
{''.join(blocks)}
<footer>Social Worker &mdash; authorized load and behaviour testing.
Traffic in this report was generated against a system the operator owns or is
explicitly permitted to test.</footer>
</div></body></html>
"""
    target.write_text(document, encoding="utf-8")
    return target
