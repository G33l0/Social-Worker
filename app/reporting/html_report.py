"""Self-contained HTML report writer (no external assets or CDNs)."""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.utils.time_utils import iso

_STYLE = """
:root { color-scheme: light dark; --bg:#fcfcfb; --fg:#0b0b0b; --muted:#52514e;
        --line:#e2e5ea; --accent:#2a78d6; --ok:#1a7f4b; --warn:#b06000; --err:#b3261e;
        --card:#f4f4f2;
        --series-1:#2a78d6; --series-2:#eb6834; --series-3:#1baf7a; --series-4:#eda100;
        --grid:#e2e5ea; --axis:#8a8a86; }
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg:#1a1a19; --fg:#ffffff; --muted:#c3c2b7; --line:#33332f;
    --accent:#3987e5; --ok:#4ec98a; --warn:#e0a34a; --err:#ff8a80; --card:#232322;
    --series-1:#3987e5; --series-2:#d95926; --series-3:#199e70; --series-4:#c98500;
    --grid:#33332f; --axis:#7d7d78; }
}
:root[data-theme="dark"] {
  --bg:#1a1a19; --fg:#ffffff; --muted:#c3c2b7; --line:#33332f;
  --accent:#3987e5; --ok:#4ec98a; --warn:#e0a34a; --err:#ff8a80; --card:#232322;
  --series-1:#3987e5; --series-2:#d95926; --series-3:#199e70; --series-4:#c98500;
  --grid:#33332f; --axis:#7d7d78; }
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
.verdict { display:inline-block; padding:.15rem .6rem; border-radius:99px;
           font-weight:600; font-size:.85rem; letter-spacing:.02em; }
.verdict.pass { background:rgba(26,127,75,.14); color:var(--ok);
                border:1px solid rgba(26,127,75,.4); }
.verdict.fail { background:rgba(179,38,30,.14); color:var(--err);
                border:1px solid rgba(179,38,30,.45); }
.charts { display:grid; grid-template-columns:repeat(auto-fit,minmax(430px,1fr));
          gap:1rem; margin-top:1rem; }
.chart.wide { grid-column:1 / -1; }
.chart { background:var(--card); border:1px solid var(--line); border-radius:10px;
         padding:.9rem 1rem 1rem; overflow-x:auto; align-self:start; }
.chart h3 { font-size:.92rem; margin:0 0 .1rem; font-weight:600; }
.chart .cap { color:var(--muted); font-size:.75rem; margin:0 0 .6rem; }
.chart svg { display:block; width:100%; height:auto; }
.legend { display:flex; flex-wrap:wrap; gap:.65rem; margin:.55rem 0 0;
          font-size:.75rem; color:var(--muted); }
.legend span { display:inline-flex; align-items:center; gap:.3rem; }
.swatch { width:10px; height:10px; border-radius:3px; display:inline-block; }
.tick { fill:var(--muted); font-size:10px; }
.value-label { fill:var(--fg); font-size:10px; font-weight:600; }
"""

_NUMERIC_HINTS = ("count", "ms", "posts", "replies", "errors", "sessions", "requests",
                  "workers", "visits", "uses", "rate", "duration", "active", "p50",
                  "p90", "p95", "p99", "avg", "min", "max")


def _is_numeric_column(name: str) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in _NUMERIC_HINTS)


def _verdict_badge(verdict: str) -> str:
    """Render the acceptance verdict as a badge next to the subtitle."""
    if not verdict:
        return ""
    css = "pass" if verdict.upper() == "PASS" else "fail"
    if verdict.upper() not in {"PASS", "FAIL"}:
        css = ""
    return f'&middot; <span class="verdict {css}">{html.escape(verdict)}</span>'


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
            value = str(row.get(name, ""))
            css = "num" if _is_numeric_column(name) else ""
            if len(value) > 160:
                cells.append(f'<td class="{css}" title="{html.escape(value[:1000])}">'
                             f'{html.escape(value[:157])}&hellip;</td>')
            else:
                cells.append(f'<td class="{css}">{html.escape(value)}</td>')
        body_rows.append("<tr>" + "".join(cells) + "</tr>")
    return ('<div class="tablewrap"><table><thead><tr>' + head
            + "</tr></thead><tbody>" + "".join(body_rows) + "</tbody></table></div>")


_SERIES = ("var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)")


def _fmt_number(value: float) -> str:
    """Compact axis/label formatting."""
    if value >= 10_000:
        return f"{value / 1000:.0f}k"
    if value >= 100:
        return f"{value:.0f}"
    if value >= 10:
        return f"{value:.1f}".rstrip("0").rstrip(".")
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _nice_max(value: float) -> float:
    """Round an axis maximum up to a readable step."""
    if value <= 0:
        return 1.0
    import math

    exponent = math.floor(math.log10(value))
    fraction = value / (10 ** exponent)
    for step in (1, 1.5, 2, 2.5, 3, 4, 5, 7.5, 10):
        if fraction <= step:
            return step * (10 ** exponent)
    return 10 ** (exponent + 1)


def _bar_chart(spec: Mapping[str, Any]) -> str:
    """Horizontal bars for a single measure across categories."""
    series = list(spec.get("series", []))
    if not series:
        return ""
    unit = spec.get("unit", "")
    row_height, gap, label_width = 26, 8, 118
    width, plot_width = 640, 640 - label_width - 56
    height = len(series) * (row_height + gap) + 10
    top = _nice_max(max((item["value"] for item in series), default=0.0))

    marks = []
    for index, item in enumerate(series):
        y = index * (row_height + gap)
        value = float(item["value"])
        bar = max(2.0, (value / top) * plot_width) if top else 2.0
        label = html.escape(str(item["label"]))
        marks.append(
            f'<text class="tick" x="{label_width - 8}" y="{y + row_height / 2 + 4}" '
            f'text-anchor="end">{label}</text>'
            f'<rect x="{label_width}" y="{y + 3}" width="{bar:.1f}" '
            f'height="{row_height - 6}" rx="4" fill="{_SERIES[0]}">'
            f'<title>{label}: {_fmt_number(value)} {html.escape(unit)}</title></rect>'
            f'<text class="value-label" x="{label_width + bar + 6:.1f}" '
            f'y="{y + row_height / 2 + 4}">{_fmt_number(value)}</text>')
    return (f'<svg viewBox="0 0 {width} {height}" role="img" '
            f'aria-label="{html.escape(str(spec.get("title", "chart")))}">'
            + "".join(marks) + "</svg>")


def _grouped_bar_chart(spec: Mapping[str, Any]) -> str:
    """Grouped vertical bars: several measures per category."""
    series = list(spec.get("series", []))
    groups = list(spec.get("groups", []))
    if not series or not groups:
        return ""
    width, height = 640, 240
    left, bottom, top_pad = 38, 34, 12
    plot_width = width - left - 12
    plot_height = height - bottom - top_pad
    peak = _nice_max(max((max(item["values"]) for item in series), default=0.0))

    band = plot_width / len(series)
    bar_width = max(4.0, (band - 14) / len(groups) - 2)
    marks = []
    for line in range(5):
        y = top_pad + plot_height - (plot_height * line / 4)
        marks.append(f'<line x1="{left}" x2="{width - 12}" y1="{y:.1f}" y2="{y:.1f}" '
                     f'stroke="var(--grid)" stroke-width="1"/>'
                     f'<text class="tick" x="{left - 6}" y="{y + 3:.1f}" '
                     f'text-anchor="end">{_fmt_number(peak * line / 4)}</text>')
    for index, item in enumerate(series):
        base = left + index * band + 7
        for slot, value in enumerate(item["values"][:len(groups)]):
            value = float(value)
            bar = (value / peak) * plot_height if peak else 0.0
            x = base + slot * (bar_width + 2)
            y = top_pad + plot_height - bar
            marks.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" '
                f'height="{max(bar, 1.5):.1f}" rx="3" fill="{_SERIES[slot % 4]}">'
                f'<title>{html.escape(str(item["label"]))} - '
                f'{html.escape(groups[slot])}: {_fmt_number(value)}</title></rect>')
        marks.append(f'<text class="tick" x="{left + index * band + band / 2:.1f}" '
                     f'y="{height - 12}" text-anchor="middle">'
                     f'{html.escape(str(item["label"])[:14])}</text>')
    marks.append(f'<line x1="{left}" x2="{width - 12}" y1="{top_pad + plot_height}" '
                 f'y2="{top_pad + plot_height}" stroke="var(--axis)" stroke-width="1"/>')
    legend = "".join(
        f'<span><i class="swatch" style="background:{_SERIES[index % 4]}"></i>'
        f'{html.escape(name)}</span>' for index, name in enumerate(groups))
    return (f'<svg viewBox="0 0 {width} {height}" role="img" '
            f'aria-label="{html.escape(str(spec.get("title", "chart")))}">'
            + "".join(marks) + f'</svg><p class="legend">{legend}</p>')


def _line_chart(spec: Mapping[str, Any]) -> str:
    """A single time series."""
    points = [point for point in spec.get("points", []) if point.get("count")]
    if len(points) < 2:
        return ""
    width, height = 640, 220
    left, bottom, top_pad = 44, 30, 12
    plot_width = width - left - 12
    plot_height = height - bottom - top_pad
    peak = _nice_max(max(point["value"] for point in points))
    span = max(point["t"] for point in points) or 1.0

    def coords(point: Mapping[str, Any]) -> tuple[float, float]:
        x = left + (point["t"] / span) * plot_width
        y = top_pad + plot_height - (point["value"] / peak) * plot_height
        return x, y

    grid = []
    for line in range(5):
        y = top_pad + plot_height - (plot_height * line / 4)
        grid.append(f'<line x1="{left}" x2="{width - 12}" y1="{y:.1f}" y2="{y:.1f}" '
                    f'stroke="var(--grid)" stroke-width="1"/>'
                    f'<text class="tick" x="{left - 6}" y="{y + 3:.1f}" '
                    f'text-anchor="end">{_fmt_number(peak * line / 4)}</text>')
    path = " ".join(f"{'M' if index == 0 else 'L'}{x:.1f},{y:.1f}"
                    for index, (x, y) in enumerate(coords(point) for point in points))
    markers = []
    for point in points:
        x, y = coords(point)
        markers.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{_SERIES[0]}" '
                       f'stroke="var(--card)" stroke-width="2">'
                       f'<title>+{_fmt_number(point["t"])}s: '
                       f'{_fmt_number(point["value"])} {html.escape(spec.get("unit", ""))} '
                       f'over {point["count"]} action(s)</title></circle>')
    axis = (f'<line x1="{left}" x2="{width - 12}" y1="{top_pad + plot_height}" '
            f'y2="{top_pad + plot_height}" stroke="var(--axis)" stroke-width="1"/>'
            f'<text class="tick" x="{left}" y="{height - 10}">start</text>'
            f'<text class="tick" x="{width - 12}" y="{height - 10}" '
            f'text-anchor="end">+{_fmt_number(span)}s</text>')
    return (f'<svg viewBox="0 0 {width} {height}" role="img" '
            f'aria-label="{html.escape(str(spec.get("title", "chart")))}">'
            + "".join(grid)
            + f'<path d="{path}" fill="none" stroke="{_SERIES[0]}" stroke-width="2" '
              'stroke-linejoin="round" stroke-linecap="round"/>'
            + "".join(markers) + axis + "</svg>")


def _percentile_chart(spec: Mapping[str, Any]) -> str:
    """p50 -> max distribution for one timing metric."""
    values = spec.get("values", {})
    order = [("p50", "p50"), ("p90", "p90"), ("p95", "p95"), ("p99", "p99"),
             ("max", "max")]
    series = [{"label": label, "value": float(values.get(key, 0.0))}
              for key, label in order if values.get(key) is not None]
    if not series:
        return ""
    return _bar_chart({**spec, "series": series})


def _render_chart(spec: Mapping[str, Any]) -> str:
    """Render one chart specification as an inline SVG figure."""
    renderers = {
        "bars": _bar_chart,
        "grouped_bars": _grouped_bar_chart,
        "line": _line_chart,
        "percentiles": _percentile_chart,
    }
    body = renderers.get(str(spec.get("kind", "")), lambda _spec: "")(spec)
    if not body:
        return ""
    unit = spec.get("unit", "")
    caption = f"values in {html.escape(unit)}" if unit and unit != "count" else ""
    wide = " wide" if spec.get("kind") == "line" else ""
    return (f'<figure class="chart{wide}">'
            f'<h3>{html.escape(str(spec.get("title", "")))}</h3>'
            f'<p class="cap">{caption}</p>{body}</figure>')


def _charts_block(charts: Sequence[Mapping[str, Any]]) -> str:
    """Render every chart, skipping any that has no data."""
    rendered = [_render_chart(spec) for spec in charts]
    rendered = [item for item in rendered if item]
    if not rendered:
        return ""
    return '<div class="charts">' + "".join(rendered) + "</div>"


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
               subtitle: str = "", notice: str = "",
               charts: Sequence[Mapping[str, Any]] = (),
               verdict: str = "") -> Path:
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
    if charts:
        blocks.append(_charts_block(charts))

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
<p class="sub">{html.escape(subtitle)} &middot; generated {html.escape(iso())}
{_verdict_badge(verdict)}</p>
{''.join(blocks)}
<footer>Social Worker &mdash; authorized load and behaviour testing.
Traffic in this report was generated against a system the operator owns or is
explicitly permitted to test.</footer>
</div></body></html>
"""
    target.write_text(document, encoding="utf-8")
    return target
