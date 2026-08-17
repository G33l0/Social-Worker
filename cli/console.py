"""Console rendering helpers for the Windows CMD interface.

Everything here is plain stdlib so the console works in cmd.exe, PowerShell and
a POSIX terminal without extra dependencies.  ANSI colour is enabled only when
the stream is a TTY (and, on Windows, only when virtual-terminal processing can
be switched on).
"""

from __future__ import annotations

import os
import shutil
import sys
from typing import Any, Iterable, Mapping, Sequence

WIDTH = 62

_COLOR = False


def enable_color(force: bool | None = None) -> bool:
    """Enable ANSI colour when the terminal supports it."""
    global _COLOR
    if force is not None:
        _COLOR = force
        return _COLOR
    if os.environ.get("NO_COLOR"):
        _COLOR = False
        return _COLOR
    if not sys.stdout.isatty():
        _COLOR = False
        return _COLOR
    if os.name == "nt":  # pragma: no cover - Windows only
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
            _COLOR = True
        except Exception:
            _COLOR = False
    else:
        _COLOR = True
    return _COLOR


_CODES = {
    "reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m",
    "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
    "blue": "\033[34m", "magenta": "\033[35m", "cyan": "\033[36m",
    "grey": "\033[90m",
}


def paint(text: str, *styles: str) -> str:
    """Wrap *text* in ANSI styles when colour is enabled."""
    if not _COLOR or not styles:
        return text
    prefix = "".join(_CODES.get(style, "") for style in styles)
    return f"{prefix}{text}{_CODES['reset']}"


def terminal_width(default: int = 100) -> int:
    """Current terminal width (falls back to *default*)."""
    try:
        return shutil.get_terminal_size((default, 25)).columns
    except Exception:  # pragma: no cover
        return default


def clear() -> None:
    """Clear the console."""
    os.system("cls" if os.name == "nt" else "clear")


def rule(char: str = "=", width: int = WIDTH) -> str:
    """A horizontal rule."""
    return char * width


def banner(title: str, subtitle: str = "", width: int = WIDTH) -> str:
    """The bordered Social Worker banner."""
    lines = [rule("=", width), title.center(width)]
    if subtitle:
        lines.append(subtitle.center(width))
    lines.append(rule("=", width))
    return "\n".join(lines)


def header(title: str, width: int = WIDTH) -> None:
    """Print a section header."""
    print()
    print(paint(rule("=", width), "cyan"))
    print(paint(title.center(width), "bold"))
    print(paint(rule("=", width), "cyan"))


def section(title: str) -> None:
    """Print a smaller sub-heading."""
    print()
    print(paint(title, "bold"))
    print(paint("-" * max(len(title), 20), "grey"))


def info(message: str) -> None:
    """Print an informational line."""
    print(paint(message, "cyan"))


def success(message: str) -> None:
    """Print a success line."""
    print(paint(f"[OK] {message}", "green"))


def warn(message: str) -> None:
    """Print a warning line."""
    print(paint(f"[!] {message}", "yellow"))


def error(message: str) -> None:
    """Print an error line."""
    print(paint(f"[X] {message}", "red"))


def table(rows: Sequence[Mapping[str, Any]], columns: Sequence[str] | None = None, *,
          headers: Mapping[str, str] | None = None, max_rows: int = 0,
          empty: str = "(no data)") -> str:
    """Render *rows* as a fixed-width text table."""
    if not rows:
        return paint(empty, "grey")
    keys = list(columns) if columns else list(dict.fromkeys(
        key for row in rows for key in row))
    labels = {key: (headers or {}).get(key, key.replace("_", " ").upper())
              for key in keys}
    display = list(rows[:max_rows]) if max_rows else list(rows)

    widths = {key: len(labels[key]) for key in keys}
    for row in display:
        for key in keys:
            widths[key] = max(widths[key], len(_fmt(row.get(key, ""))))

    line = "  ".join(labels[key].ljust(widths[key]) for key in keys)
    out = [paint(line, "bold"), "-" * len(line)]
    for row in display:
        out.append("  ".join(_fmt(row.get(key, "")).ljust(widths[key]) for key in keys))
    if max_rows and len(rows) > max_rows:
        out.append(paint(f"... {len(rows) - max_rows} more row(s)", "grey"))
    return "\n".join(out)


def keyvalues(data: Mapping[str, Any], indent: str = "  ") -> str:
    """Render a mapping as aligned ``key : value`` lines."""
    if not data:
        return paint("(empty)", "grey")
    width = max(len(str(key)) for key in data)
    return "\n".join(f"{indent}{str(key).ljust(width)} : {_fmt(value)}"
                     for key, value in data.items())


def _fmt(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:,.2f}"
    if isinstance(value, int):
        return f"{value:,}"
    if value is None:
        return "-"
    return str(value)


# ------------------------------------------------------------------- prompts
def ask(prompt: str, default: str = "", *, required: bool = False) -> str:
    """Ask for a free-text value."""
    suffix = f" [{default}]" if default else ""
    while True:
        answer = input(paint(f"{prompt}{suffix}\n> ", "cyan")).strip()
        if not answer:
            answer = default
        if answer or not required:
            return answer
        error("A value is required.")


def ask_int(prompt: str, default: int | None = None, *, minimum: int | None = None,
            maximum: int | None = None) -> int:
    """Ask for an integer within optional bounds."""
    while True:
        raw = ask(prompt, "" if default is None else str(default))
        try:
            value = int(raw)
        except ValueError:
            error("Enter a whole number.")
            continue
        if minimum is not None and value < minimum:
            error(f"Minimum is {minimum}.")
            continue
        if maximum is not None and value > maximum:
            error(f"Maximum is {maximum}.")
            continue
        return value


def ask_float(prompt: str, default: float | None = None, *,
              minimum: float | None = None, maximum: float | None = None) -> float:
    """Ask for a number within optional bounds."""
    while True:
        raw = ask(prompt, "" if default is None else str(default))
        try:
            value = float(raw)
        except ValueError:
            error("Enter a number.")
            continue
        if minimum is not None and value < minimum:
            error(f"Minimum is {minimum}.")
            continue
        if maximum is not None and value > maximum:
            error(f"Maximum is {maximum}.")
            continue
        return value


def ask_bool(prompt: str, default: bool = False) -> bool:
    """Ask a yes/no question."""
    hint = "Y/n" if default else "y/N"
    while True:
        raw = input(paint(f"{prompt} ({hint})\n> ", "cyan")).strip().lower()
        if not raw:
            return default
        if raw in {"y", "yes", "true", "1"}:
            return True
        if raw in {"n", "no", "false", "0"}:
            return False
        error("Answer y or n.")


def choose(prompt: str, options: Sequence[str], *, default: int = 1,
           descriptions: Mapping[str, str] | None = None) -> str:
    """Present a numbered list and return the chosen option."""
    print()
    print(paint(prompt, "bold"))
    for index, option in enumerate(options, start=1):
        note = (descriptions or {}).get(option, "")
        suffix = paint(f"  {note}", "grey") if note else ""
        print(f"  {index}. {option}{suffix}")
    while True:
        raw = input(paint(f"> [{default}] ", "cyan")).strip()
        if not raw:
            return options[default - 1]
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]
        for option in options:
            if option.lower() == raw.lower():
                return option
        error(f"Choose 1-{len(options)}.")


def pause(message: str = "Press Enter to continue...") -> None:
    """Wait for the operator to acknowledge."""
    input(paint(message, "grey"))


def bullet_list(items: Iterable[str], marker: str = "-") -> str:
    """Render a simple bullet list."""
    return "\n".join(f"  {marker} {item}" for item in items)
