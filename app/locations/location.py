"""Geographic test-location model.

A *location* describes where authorized test traffic originates from.  Traffic
is produced by real infrastructure the operator controls -- a regional cloud
runner, a VM, or an explicitly configured corporate test proxy.  Social Worker
does not attempt to disguise the origin of its traffic; the ``identify_as``
header set by the browser layer keeps every request attributable.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.utils.time_utils import parse_hhmm, utc_now, within_window
from app.utils.validation import validate_identifier

REGION_GROUPS = ("north_america", "europe", "asia", "africa", "oceania",
                 "south_america", "custom")

DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class RunnerType(str, Enum):
    """How a location's traffic is produced."""

    LOCAL = "local"          # this machine (development / mock-site testing)
    REMOTE_AGENT = "remote"  # a registered worker agent in that region
    PROXY = "proxy"          # an explicitly configured, authorized test proxy


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class TestWindow(Base):
    """An ``HH:MM``-``HH:MM`` window during which a location may run."""

    start: str = "00:00"
    end: str = "23:59"

    @field_validator("start", "end")
    @classmethod
    def _validate_time(cls, value: str) -> str:
        parse_hhmm(value)
        return value

    def contains(self, moment: Any = None) -> bool:
        """True when *moment* (default: now, UTC) falls in this window."""
        return within_window(moment or utc_now(), self.start, self.end)


class TestSchedule(Base):
    """A named schedule bound to one or more locations."""

    name: str = "always"
    timezone: str = "UTC"
    days: list[str] = Field(default_factory=lambda: list(DAY_NAMES))
    windows: list[TestWindow] = Field(default_factory=lambda: [TestWindow()])
    enabled: bool = True

    @field_validator("days")
    @classmethod
    def _validate_days(cls, value: list[str]) -> list[str]:
        days = [day.strip().lower()[:3] for day in value]
        invalid = [day for day in days if day not in DAY_NAMES]
        if invalid:
            raise ValueError(f"Unknown day(s): {', '.join(invalid)}")
        return days

    def is_active(self, moment: Any = None) -> bool:
        """True when the schedule permits testing at *moment*."""
        if not self.enabled:
            return False
        moment = moment or utc_now()
        if DAY_NAMES[moment.weekday()] not in self.days:
            return False
        return any(window.contains(moment) for window in self.windows) if self.windows else True


class Location(Base):
    """A configured geographic test location."""

    key: str
    name: str = ""
    region_group: Literal["north_america", "europe", "asia", "africa", "oceania",
                          "south_america", "custom"] = "custom"
    country: str = ""
    region: str = ""
    city: str = ""
    timezone: str = "UTC"
    locale: str = "en-US"
    enabled: bool = True
    workers: int = Field(0, ge=0, le=100_000)
    max_concurrent_workers: int = Field(0, ge=0, le=100_000)
    runner: RunnerType = RunnerType.LOCAL
    runner_endpoint: str = ""
    proxy_url: str = ""
    schedule: str = ""
    weight: float = Field(1.0, ge=0.0)
    notes: str = ""

    @field_validator("key")
    @classmethod
    def _validate_key(cls, value: str) -> str:
        return validate_identifier(value, field="location key")

    @model_validator(mode="after")
    def _defaults(self) -> "Location":
        if not self.name:
            object.__setattr__(self, "name", self.key.replace("_", " ").title())
        if self.max_concurrent_workers == 0:
            object.__setattr__(self, "max_concurrent_workers", max(1, self.workers))
        if self.runner == RunnerType.PROXY and not self.proxy_url:
            raise ValueError(
                f"Location {self.key!r} uses runner 'proxy' but no proxy_url was configured")
        if self.runner == RunnerType.REMOTE_AGENT and not self.runner_endpoint:
            raise ValueError(
                f"Location {self.key!r} uses runner 'remote' but no runner_endpoint was set")
        return self

    @property
    def label(self) -> str:
        """Human-readable label used in reports and the dashboard."""
        parts = [part for part in (self.city, self.region, self.country) if part]
        return f"{self.name} ({', '.join(parts)})" if parts else self.name

    def to_row(self) -> dict[str, Any]:
        """Flat dict used by report writers."""
        return {
            "key": self.key,
            "name": self.name,
            "region_group": self.region_group,
            "country": self.country,
            "region": self.region,
            "city": self.city,
            "enabled": self.enabled,
            "workers": self.workers,
            "max_concurrent_workers": self.max_concurrent_workers,
            "runner": self.runner.value,
            "schedule": self.schedule,
        }


DEFAULT_LOCATIONS: tuple[dict[str, Any], ...] = (
    {"key": "united_states", "name": "United States", "region_group": "north_america",
     "country": "United States", "city": "Ashburn", "timezone": "America/New_York",
     "workers": 20, "max_concurrent_workers": 20},
    {"key": "canada", "name": "Canada", "region_group": "north_america",
     "country": "Canada", "city": "Toronto", "timezone": "America/Toronto",
     "workers": 10, "max_concurrent_workers": 10},
    {"key": "united_kingdom", "name": "United Kingdom", "region_group": "europe",
     "country": "United Kingdom", "city": "London", "timezone": "Europe/London",
     "workers": 10, "max_concurrent_workers": 10},
    {"key": "germany", "name": "Germany", "region_group": "europe",
     "country": "Germany", "city": "Frankfurt", "timezone": "Europe/Berlin",
     "locale": "de-DE", "workers": 10, "max_concurrent_workers": 10},
    {"key": "nigeria", "name": "Nigeria", "region_group": "africa",
     "country": "Nigeria", "city": "Lagos", "timezone": "Africa/Lagos",
     "workers": 10, "max_concurrent_workers": 10},
    {"key": "japan", "name": "Japan", "region_group": "asia",
     "country": "Japan", "city": "Tokyo", "timezone": "Asia/Tokyo",
     "locale": "ja-JP", "workers": 5, "max_concurrent_workers": 5},
)
