"""Input validation helpers used by the CLI wizards and the config loader."""

from __future__ import annotations

import re
from typing import Iterable, Sequence
from urllib.parse import urlparse

WORKER_ID_RE = re.compile(r"^[A-Z]{2,6}-\d{3,8}$")
IDENT_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,62}$")


class ValidationError(ValueError):
    """Raised when operator-supplied input fails validation."""


def validate_url(value: str, *, allow_insecure: bool = True) -> str:
    """Validate and normalise a website URL."""
    value = (value or "").strip()
    if not value:
        raise ValidationError("URL must not be empty")
    if "://" not in value:
        value = "http://" + value
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        raise ValidationError(f"Unsupported URL scheme: {parsed.scheme!r}")
    if not allow_insecure and parsed.scheme == "http":
        raise ValidationError("Insecure http:// URLs are disabled by policy")
    if not parsed.netloc:
        raise ValidationError("URL must include a hostname")
    return value.rstrip("/")


def validate_identifier(value: str, *, field: str = "identifier") -> str:
    """Validate a lowercase slug identifier (location keys, forum keys, ...)."""
    value = (value or "").strip().lower().replace(" ", "_")
    if not IDENT_RE.match(value):
        raise ValidationError(
            f"{field} must be lowercase alphanumeric with '_' or '-' (got {value!r})"
        )
    return value


def validate_worker_id(value: str) -> str:
    """Validate a worker identifier such as ``SW-00001``."""
    value = (value or "").strip().upper()
    if not WORKER_ID_RE.match(value):
        raise ValidationError(f"Worker ID must look like SW-00001 (got {value!r})")
    return value


def validate_positive_int(value: object, *, field: str = "value", minimum: int = 1,
                          maximum: int | None = None) -> int:
    """Coerce *value* to an int and range-check it."""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field} must be a whole number (got {value!r})") from exc
    if number < minimum:
        raise ValidationError(f"{field} must be >= {minimum} (got {number})")
    if maximum is not None and number > maximum:
        raise ValidationError(f"{field} must be <= {maximum} (got {number})")
    return number


def validate_float_range(value: object, *, field: str = "value",
                         minimum: float = 0.0, maximum: float = 1.0) -> float:
    """Coerce *value* to a float and range-check it."""
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field} must be a number (got {value!r})") from exc
    if not (minimum <= number <= maximum):
        raise ValidationError(f"{field} must be between {minimum} and {maximum} (got {number})")
    return number


def validate_probability(value: object, *, field: str = "probability") -> float:
    """Accept ``0.65`` or ``65`` / ``65%`` and normalise to a 0..1 float."""
    raw = str(value).strip().rstrip("%")
    number = float(raw) if raw else 0.0
    if number > 1.0:
        number = number / 100.0
    return validate_float_range(number, field=field, minimum=0.0, maximum=1.0)


def validate_interval(minimum: float, maximum: float, *, field: str = "interval") -> tuple[float, float]:
    """Validate a ``min <= max`` interval pair."""
    if minimum < 0 or maximum < 0:
        raise ValidationError(f"{field} bounds must not be negative")
    if minimum > maximum:
        raise ValidationError(f"{field} minimum ({minimum}) exceeds maximum ({maximum})")
    return float(minimum), float(maximum)


def validate_choice(value: str, choices: Sequence[str], *, field: str = "choice") -> str:
    """Validate that *value* is one of *choices* (case-insensitive)."""
    normalised = (value or "").strip().upper()
    upper = {choice.upper(): choice for choice in choices}
    if normalised not in upper:
        raise ValidationError(f"{field} must be one of {', '.join(choices)} (got {value!r})")
    return upper[normalised]


def validate_weights(weights: dict[str, float]) -> dict[str, float]:
    """Normalise a weight mapping so the values sum to 1.0."""
    if not weights:
        raise ValidationError("At least one weighted entry is required")
    for key, weight in weights.items():
        if weight < 0:
            raise ValidationError(f"Weight for {key!r} must not be negative")
    total = sum(weights.values())
    if total <= 0:
        raise ValidationError("Weights must sum to a positive number")
    return {key: value / total for key, value in weights.items()}


def ensure_unique(values: Iterable[str], *, field: str = "value") -> list[str]:
    """Return *values* as a list, raising if duplicates are present."""
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            raise ValidationError(f"Duplicate {field}: {value!r}")
        seen.add(value)
        result.append(value)
    return result
