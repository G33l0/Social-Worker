"""Interactive test-schedule configuration."""

from __future__ import annotations

from cli import console
from app.locations.location import DAY_NAMES, TestSchedule, TestWindow
from app.locations.location_manager import LocationManager
from app.utils.config import ConfigPaths


def run_schedule_wizard(*, paths: ConfigPaths | None = None,
                        manager: LocationManager | None = None) -> LocationManager:
    """Create named time windows and bind them to locations."""
    paths = paths or ConfigPaths()
    manager = manager or LocationManager.load(paths.locations, paths.schedules)

    while True:
        console.header("CONFIGURE TEST SCHEDULES")
        rows = [
            {
                "name": schedule.name,
                "days": ",".join(schedule.days),
                "windows": "; ".join(f"{window.start}-{window.end}"
                                     for window in schedule.windows),
                "enabled": schedule.enabled,
                "active_now": schedule.is_active(),
            }
            for schedule in manager.schedules()
        ]
        print(console.table(rows))
        print()
        bindings = [{"location": location.key,
                     "schedule": location.schedule or "always",
                     "active_now": manager.is_active(location.key)}
                    for location in manager.all()]
        print(console.table(bindings, max_rows=20))

        action = console.choose(
            "Action:",
            ["Add schedule", "Bind schedule to location", "Unbind location",
             "Save and return", "Return without saving"],
            default=4)

        try:
            if action == "Add schedule":
                _add(manager)
            elif action == "Bind schedule to location":
                _bind(manager)
            elif action == "Unbind location":
                _unbind(manager)
            elif action == "Save and return":
                manager.save_schedules(paths.schedules)
                manager.save(paths.locations)
                console.success("Schedules saved")
                return manager
            else:
                return manager
        except (ValueError, KeyError) as exc:
            console.error(str(exc))
            console.pause()


def _add(manager: LocationManager) -> None:
    name = console.ask("Schedule name", required=True).strip().lower()
    days_raw = console.ask("Days (comma separated, blank = every day)", "")
    days = ([day.strip()[:3].lower() for day in days_raw.split(",") if day.strip()]
            or list(DAY_NAMES))
    windows: list[TestWindow] = []
    while True:
        start = console.ask("Window start (HH:MM)", "09:00")
        end = console.ask("Window end (HH:MM)", "17:00")
        windows.append(TestWindow(start=start, end=end))
        if not console.ask_bool("Add another window?", False):
            break
    manager.add_schedule(TestSchedule(name=name, days=days, windows=windows))
    console.success(f"Schedule {name} added")


def _bind(manager: LocationManager) -> None:
    names = [schedule.name for schedule in manager.schedules()]
    if not names:
        raise ValueError("Create a schedule first")
    location = console.choose("Location:", manager.keys(), default=1)
    schedule = console.choose("Schedule:", names, default=1)
    manager.update(location, schedule=schedule)
    console.success(f"{location} now follows the {schedule} schedule")


def _unbind(manager: LocationManager) -> None:
    location = console.choose("Location:", manager.keys(), default=1)
    manager.update(location, schedule="")
    console.success(f"{location} now runs whenever the test is running")
