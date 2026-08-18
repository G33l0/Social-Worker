"""Live terminal dashboard (spec section 18)."""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from cli import console
from app.utils.time_utils import humanize, utc_now


def render(status: dict[str, Any], *, worker_rows: int = 15) -> str:
    """Render one frame of the live monitor."""
    width = max(62, min(console.terminal_width(), 110))
    lines: list[str] = [
        console.rule("=", width),
        "SOCIAL WORKER LIVE MONITOR".center(width),
        console.rule("=", width),
        "",
        f"TEST STATUS: {status.get('state', 'UNKNOWN')}"
        f"    RUN: {status.get('test_run_id', '-')}"
        f"    TARGET: {status.get('target', '-')}",
    ]
    if status.get("dry_run"):
        lines.append("MODE: DRY RUN - no posts or replies are submitted")

    stats = status.get("global", {})
    scheduler = status.get("scheduler", {}) or {}
    scheduler_stats = scheduler.get("stats", {})
    lines += [
        "",
        "GLOBAL",
        f"Workers: {status.get('workers_planned', 0)}"
        f"   Active: {stats.get('active', 0)}"
        f"   Completed: {stats.get('completed', 0)}"
        f"   Failed: {stats.get('failed', 0)}"
        f"   Errors: {stats.get('errors', 0)}",
        f"Posts: {stats.get('posts', 0)}"
        f"   Replies: {stats.get('replies', 0)}"
        f"   Requests: {stats.get('requests', 0)}"
        f"   Avg response: {stats.get('avg_response_ms', 0)} ms"
        f"   Error rate: {stats.get('error_rate', 0) * 100:.1f}%",
        f"Queued: {scheduler_stats.get('queued', 0)}"
        f"   Running: {scheduler_stats.get('running', 0)}"
        f"   Dispatched: {scheduler_stats.get('dispatched', 0)}"
        f"   Skipped: {scheduler_stats.get('skipped', 0)}"
        f"   Rate-limit events: {stats.get('rate_limit_events', 0)}",
        "",
    ]

    locations = status.get("locations", [])
    if locations:
        lines.append(console.table(
            locations,
            columns=["location", "active", "sessions", "posts", "replies", "errors",
                     "avg_response_ms", "avg_session_ms"]))
        lines.append("")

    workers = status.get("workers", [])
    if workers:
        ordered = sorted(workers, key=_worker_sort)
        lines.append(console.table(
            ordered,
            columns=["worker_id", "location", "status", "action", "posts", "replies",
                     "errors", "username"],
            max_rows=worker_rows))
        lines.append("")

    concurrency = scheduler.get("concurrency", {})
    if concurrency:
        lines.append(
            f"CONCURRENCY  global {concurrency.get('global_active', 0)}"
            f"/{concurrency.get('global_limit', 0)}"
            f" (peak {concurrency.get('global_peak', 0)})")
        per_location = concurrency.get("locations", {})
        if per_location:
            lines.append("  " + "   ".join(
                f"{key}: {value['active']}/{value['limit']}"
                for key, value in sorted(per_location.items())))
    lines.append(console.rule("=", width))
    verdict = (status.get("verdict") or {}).get("verdict")
    if verdict:
        lines.append(f"ACCEPTANCE: {verdict}")
    lines.append(f"Updated {utc_now().strftime('%H:%M:%S')} UTC"
                 f"   Uptime {humanize(_uptime(status))}"
                 "   Press Enter to leave the monitor")
    return "\n".join(lines)


def _worker_sort(row: dict[str, Any]) -> tuple[int, str]:
    order = {"ACTIVE": 0, "STARTING": 1, "WAITING": 2, "BACKOFF": 3, "PAUSED": 4}
    return order.get(str(row.get("status")), 9), str(row.get("worker_id"))


def _uptime(status: dict[str, Any]) -> float:
    started = status.get("global", {}).get("started_at") or status.get("started_at")
    if not started:
        return 0.0
    try:
        from datetime import datetime

        return (utc_now() - datetime.fromisoformat(started)).total_seconds()
    except Exception:  # pragma: no cover
        return 0.0


async def live_monitor(status_provider: Callable[[], dict[str, Any]], *,
                       interval: float = 1.0, duration: float = 0.0,
                       clear_screen: bool = True,
                       stop_event: asyncio.Event | None = None) -> None:
    """Refresh the dashboard until *stop_event* is set (or *duration* elapses).

    The caller owns the exit condition - the console waits on Enter - so leaving
    the monitor never tears down the run the way an interrupt would.
    """
    elapsed = 0.0
    try:
        while stop_event is None or not stop_event.is_set():
            if clear_screen:
                console.clear()
            print(render(status_provider()))
            if stop_event is not None:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=interval)
                    return
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(interval)
            elapsed += interval
            if duration and elapsed >= duration:
                return
    except (KeyboardInterrupt, asyncio.CancelledError):
        return
