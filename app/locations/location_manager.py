"""Load, validate and persist the geographic test-location catalogue."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from app.locations.allocation import Allocation, ConcurrencyController, allocate_workers
from app.locations.location import (
    DEFAULT_LOCATIONS, REGION_GROUPS, Location, TestSchedule,
)
from app.utils.config import ConfigPaths, load_yaml_document, save_yaml_document
from app.utils.logger import get_logger
from app.utils.validation import ValidationError

LOGGER = get_logger("locations")


class LocationManager:
    """Owns the set of configured locations and their schedules."""

    def __init__(self, locations: Iterable[Location] | None = None,
                 schedules: Iterable[TestSchedule] | None = None) -> None:
        self._locations: dict[str, Location] = {}
        self._schedules: dict[str, TestSchedule] = {}
        for location in locations or []:
            self._locations[location.key] = location
        for schedule in schedules or []:
            self._schedules[schedule.name] = schedule

    # -------------------------------------------------------------------- I/O
    @classmethod
    def load(cls, path: str | Path | None = None,
             schedules_path: str | Path | None = None) -> "LocationManager":
        """Load locations (and schedules) from YAML, falling back to defaults."""
        paths = ConfigPaths()
        location_file = Path(path or paths.locations)
        schedule_file = Path(schedules_path or paths.schedules)

        document = load_yaml_document(location_file)
        raw = document.get("locations", [])
        if isinstance(raw, dict):
            raw = [{"key": key, **(value or {})} for key, value in raw.items()]
        locations = [Location.model_validate(item) for item in raw]
        if not locations:
            LOGGER.info("No locations configured; using built-in defaults")
            locations = [Location.model_validate(item) for item in DEFAULT_LOCATIONS]

        schedules_doc = load_yaml_document(schedule_file)
        raw_schedules = schedules_doc.get("schedules", [])
        if isinstance(raw_schedules, dict):
            raw_schedules = [{"name": key, **(value or {})}
                             for key, value in raw_schedules.items()]
        schedules = [TestSchedule.model_validate(item) for item in raw_schedules]
        if not schedules:
            schedules = [TestSchedule()]

        manager = cls(locations, schedules)
        manager.validate()
        return manager

    def save(self, path: str | Path | None = None) -> Path:
        """Write the catalogue back to ``config/locations.yaml``."""
        target = Path(path or ConfigPaths().locations)
        payload = {"locations": [location.model_dump(mode="json")
                                 for location in self.all()]}
        save_yaml_document(target, payload)
        LOGGER.info("Saved %d location(s) to %s", len(self._locations), target)
        return target

    def save_schedules(self, path: str | Path | None = None) -> Path:
        """Write the schedule catalogue to ``config/schedules.yaml``."""
        target = Path(path or ConfigPaths().schedules)
        payload = {"schedules": [schedule.model_dump(mode="json")
                                 for schedule in self._schedules.values()]}
        save_yaml_document(target, payload)
        return target

    # -------------------------------------------------------------------- CRUD
    def add(self, location: Location, *, replace: bool = False) -> Location:
        """Register a location."""
        if location.key in self._locations and not replace:
            raise ValidationError(f"Location {location.key!r} already exists")
        self._locations[location.key] = location
        return location

    def update(self, key: str, **changes: Any) -> Location:
        """Apply *changes* to an existing location."""
        current = self.get(key)
        data = current.model_dump()
        data.update(changes)
        updated = Location.model_validate(data)
        self._locations[key] = updated
        return updated

    def remove(self, key: str) -> None:
        """Delete a location."""
        if key not in self._locations:
            raise KeyError(f"Unknown location: {key}")
        del self._locations[key]

    def get(self, key: str) -> Location:
        """Return the location for *key*."""
        try:
            return self._locations[key]
        except KeyError as exc:
            raise KeyError(f"Unknown location: {key}") from exc

    def set_enabled(self, key: str, enabled: bool) -> Location:
        """Enable/disable a location."""
        return self.update(key, enabled=enabled)

    # ------------------------------------------------------------------ views
    def all(self) -> list[Location]:
        """All configured locations sorted by key."""
        return [self._locations[key] for key in sorted(self._locations)]

    def enabled(self) -> list[Location]:
        """Only the enabled locations."""
        return [location for location in self.all() if location.enabled]

    def keys(self) -> list[str]:
        """Sorted location keys."""
        return sorted(self._locations)

    def by_group(self) -> dict[str, list[Location]]:
        """Locations grouped by their region group."""
        grouped: dict[str, list[Location]] = {group: [] for group in REGION_GROUPS}
        for location in self.all():
            grouped.setdefault(location.region_group, []).append(location)
        return {group: items for group, items in grouped.items() if items}

    def schedules(self) -> list[TestSchedule]:
        """All configured schedules."""
        return list(self._schedules.values())

    def schedule_for(self, key: str) -> TestSchedule | None:
        """Schedule bound to a location, if any."""
        location = self.get(key)
        if not location.schedule:
            return None
        return self._schedules.get(location.schedule)

    def add_schedule(self, schedule: TestSchedule) -> TestSchedule:
        """Register a named schedule."""
        self._schedules[schedule.name] = schedule
        return schedule

    def is_active(self, key: str, moment: Any = None) -> bool:
        """True when the location is enabled and inside its schedule window."""
        location = self.get(key)
        if not location.enabled:
            return False
        schedule = self.schedule_for(key)
        return True if schedule is None else schedule.is_active(moment)

    # ------------------------------------------------------- capacity planning
    def total_requested_workers(self) -> int:
        """Sum of the requested worker counts across enabled locations."""
        return sum(location.workers for location in self.enabled())

    def allocate(self, global_limit: int) -> Allocation:
        """Distribute *global_limit* slots across the enabled locations."""
        return allocate_workers(self.enabled(), global_limit)

    def apply_default_ceiling(self, default_ceiling: int) -> int:
        """Give locations without an explicit ceiling the configured default.

        Returns how many locations were adjusted.  Called before a run so
        ``concurrency.default_max_workers_per_location`` actually governs the
        locations that did not set a ceiling of their own.
        """
        if default_ceiling < 1:
            return 0
        adjusted = 0
        for location in self.all():
            if location.ceiling_is_explicit:
                continue
            if location.max_concurrent_workers != default_ceiling:
                updated = self.update(location.key,
                                      max_concurrent_workers=default_ceiling)
                object.__setattr__(updated, "_ceiling_explicit", False)
                adjusted += 1
        if adjusted:
            LOGGER.info("Applied the default per-location ceiling (%d) to %d location(s)",
                        default_ceiling, adjusted)
        return adjusted

    def concurrency_controller(self, global_limit: int, *,
                               per_worker_limit: int = 1) -> ConcurrencyController:
        """Build a controller pre-loaded with every location ceiling."""
        limits = {location.key: location.max_concurrent_workers
                  for location in self.enabled()}
        return ConcurrencyController(global_limit, limits,
                                     per_worker_limit=per_worker_limit)

    def validate(self, *, global_limit: int | None = None) -> list[str]:
        """Return a list of human-readable configuration warnings."""
        warnings: list[str] = []
        if not self._locations:
            warnings.append("No locations configured")
        for location in self.all():
            if location.enabled and location.workers == 0:
                warnings.append(f"{location.key}: enabled but allocated 0 workers")
            if location.workers > location.max_concurrent_workers:
                warnings.append(
                    f"{location.key}: requested {location.workers} workers exceeds the "
                    f"location ceiling of {location.max_concurrent_workers}")
            if location.schedule and location.schedule not in self._schedules:
                warnings.append(
                    f"{location.key}: references unknown schedule {location.schedule!r}")
        if global_limit is not None:
            requested = self.total_requested_workers()
            if requested > global_limit:
                warnings.append(
                    f"Locations request {requested} workers but the global limit is "
                    f"{global_limit}; allocation will be scaled down proportionally")
        for warning in warnings:
            LOGGER.warning("Location config: %s", warning)
        return warnings

    def summary_rows(self) -> list[dict[str, Any]]:
        """Rows for the CLI location table."""
        return [location.to_row() for location in self.all()]
