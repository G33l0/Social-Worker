"""Interactive geographic test-location configuration (spec sections 2-3)."""

from __future__ import annotations

from cli import console
from app.locations.location import REGION_GROUPS, Location, RunnerType
from app.locations.location_manager import LocationManager
from app.utils.config import ConfigPaths, Settings
from app.utils.validation import ValidationError, validate_identifier


def run_location_wizard(settings: Settings, *, paths: ConfigPaths | None = None,
                        manager: LocationManager | None = None) -> LocationManager:
    """Add, edit, enable/disable and allocate workers to test locations."""
    paths = paths or ConfigPaths()
    manager = manager or LocationManager.load(paths.locations, paths.schedules)

    while True:
        console.header("CONFIGURE GEOGRAPHIC TEST LOCATIONS")
        _print_table(manager, settings)

        action = console.choose(
            "Action:",
            ["Add location", "Edit location", "Enable/disable location",
             "Set worker allocation", "Set concurrency ceiling", "Remove location",
             "Preview allocation", "Save and return", "Return without saving"],
            default=8)

        try:
            if action == "Add location":
                _add(manager)
            elif action == "Edit location":
                _edit(manager)
            elif action == "Enable/disable location":
                _toggle(manager)
            elif action == "Set worker allocation":
                _allocate(manager)
            elif action == "Set concurrency ceiling":
                _ceiling(manager)
            elif action == "Remove location":
                _remove(manager)
            elif action == "Preview allocation":
                _preview(manager, settings)
            elif action == "Save and return":
                manager.save(paths.locations)
                manager.save_schedules(paths.schedules)
                console.success(f"Saved {len(manager.all())} location(s)")
                return manager
            else:
                return manager
        except (ValidationError, ValueError, KeyError) as exc:
            console.error(str(exc))
            console.pause()


def _print_table(manager: LocationManager, settings: Settings) -> None:
    rows = [
        {
            "key": location.key,
            "name": location.name,
            "country": location.country,
            "group": location.region_group,
            "workers": location.workers,
            "max_concurrent": location.max_concurrent_workers,
            "runner": location.runner.value,
            "schedule": location.schedule or "always",
            "enabled": location.enabled,
        }
        for location in manager.all()
    ]
    print(console.table(rows))
    requested = manager.total_requested_workers()
    limit = settings.concurrency.max_global_workers
    print()
    print(console.keyvalues({
        "Requested workers": requested,
        "Global worker limit": limit,
        "Status": "within limit" if requested <= limit else "will be scaled down",
    }))


def _add(manager: LocationManager) -> None:
    key = validate_identifier(console.ask("Location key (e.g. united_states)",
                                          required=True), field="location key")
    name = console.ask("Display name", key.replace("_", " ").title())
    group = console.choose("Region group:", list(REGION_GROUPS), default=len(REGION_GROUPS))
    country = console.ask("Country", "")
    region = console.ask("Region/state (optional)", "")
    city = console.ask("City (optional)", "")
    timezone = console.ask("Timezone id", "UTC")
    locale = console.ask("Browser locale", "en-US")
    workers = console.ask_int("Worker allocation", 10, minimum=0)
    ceiling = console.ask_int("Maximum concurrent workers", max(1, workers), minimum=1)

    runner_choice = console.choose(
        "How is this location's traffic produced?",
        ["local", "remote", "proxy"], default=1,
        descriptions={
            "local": "this machine (development / mock site)",
            "remote": "a registered worker agent running in that region",
            "proxy": "an explicitly configured, authorized test proxy",
        })
    runner = RunnerType(runner_choice)
    endpoint = ""
    proxy = ""
    if runner is RunnerType.REMOTE_AGENT:
        endpoint = console.ask("Runner endpoint (http://host:port)", required=True)
    elif runner is RunnerType.PROXY:
        console.warn("Only configure proxies you are authorized to use for this test.")
        proxy = console.ask("Proxy server URL", required=True)

    schedule = console.ask("Schedule name (blank = always)", "")

    manager.add(Location(
        key=key, name=name, region_group=group, country=country, region=region,
        city=city, timezone=timezone, locale=locale, workers=workers,
        max_concurrent_workers=ceiling, runner=runner, runner_endpoint=endpoint,
        proxy_url=proxy, schedule=schedule,
    ), replace=True)
    console.success(f"Added location {key}")


def _pick(manager: LocationManager, prompt: str = "Location:") -> str:
    keys = manager.keys()
    if not keys:
        raise ValueError("No locations configured yet")
    return console.choose(prompt, keys, default=1)


def _edit(manager: LocationManager) -> None:
    key = _pick(manager)
    location = manager.get(key)
    manager.update(
        key,
        name=console.ask("Display name", location.name),
        country=console.ask("Country", location.country),
        city=console.ask("City", location.city),
        timezone=console.ask("Timezone id", location.timezone),
        locale=console.ask("Browser locale", location.locale),
        schedule=console.ask("Schedule name (blank = always)", location.schedule),
    )
    console.success(f"Updated {key}")


def _toggle(manager: LocationManager) -> None:
    key = _pick(manager)
    location = manager.get(key)
    manager.set_enabled(key, not location.enabled)
    console.success(f"{key} is now {'enabled' if not location.enabled else 'disabled'}")


def _allocate(manager: LocationManager) -> None:
    key = _pick(manager)
    location = manager.get(key)
    workers = console.ask_int(f"Workers for {key}", location.workers, minimum=0)
    ceiling = max(location.max_concurrent_workers, workers)
    manager.update(key, workers=workers, max_concurrent_workers=ceiling)
    console.success(f"{key}: {workers} worker(s)")


def _ceiling(manager: LocationManager) -> None:
    key = _pick(manager)
    location = manager.get(key)
    ceiling = console.ask_int(f"Maximum concurrent workers for {key}",
                              location.max_concurrent_workers, minimum=1)
    manager.update(key, max_concurrent_workers=ceiling)
    console.success(f"{key}: ceiling {ceiling}")


def _remove(manager: LocationManager) -> None:
    key = _pick(manager, "Remove which location?")
    if console.ask_bool(f"Really remove {key}?", False):
        manager.remove(key)
        console.success(f"Removed {key}")


def _preview(manager: LocationManager, settings: Settings) -> None:
    allocation = manager.allocate(settings.concurrency.max_global_workers)
    rows = [{"location": key, "allocated": count,
             "ceiling": manager.get(key).max_concurrent_workers}
            for key, count in sorted(allocation.items())]
    console.section("Allocation preview")
    print(console.table(rows))
    print()
    print(console.keyvalues({"Total allocated": allocation.total,
                             "Unused global slots": allocation.unallocated}))
    console.pause()
