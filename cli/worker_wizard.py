"""Interactive worker creation (spec section 17)."""

from __future__ import annotations

from cli import console
from app.core.worker_manager import WorkerManager
from app.utils.config import ConfigPaths, ForumSelectionMode, Settings
from app.utils.ids import new_worker_id
from app.utils.validation import ValidationError, validate_worker_id
from app.workers.worker_state import WorkerProfile

FORUM_MODES = [mode.value for mode in ForumSelectionMode]


def run_worker_wizard(settings: Settings, manager: WorkerManager, *,
                      paths: ConfigPaths | None = None) -> WorkerManager:
    """Create, edit and remove worker definitions."""
    paths = paths or ConfigPaths()

    while True:
        console.header("CONFIGURE WORKERS")
        rows = manager.summary_rows()
        print(console.table(rows, max_rows=30))
        print()
        print(console.keyvalues({
            "Defined workers": len(rows),
            "Global limit": settings.concurrency.max_global_workers,
            "Auto-generation": "on (workers are derived from the location plan)"
            if not rows else "off (explicit definitions in use)",
        }))

        action = console.choose(
            "Action:",
            ["Create worker", "Create workers in bulk", "Show worker profile",
             "Edit worker", "Remove worker", "Generate from location allocation",
             "Save and return", "Return without saving"],
            default=7)

        try:
            if action == "Create worker":
                _create(settings, manager)
            elif action == "Create workers in bulk":
                _create_bulk(settings, manager)
            elif action == "Show worker profile":
                _show(manager)
            elif action == "Edit worker":
                _edit(manager)
            elif action == "Remove worker":
                _remove(manager)
            elif action == "Generate from location allocation":
                _generate(settings, manager)
            elif action == "Save and return":
                manager.save(paths.workers)
                console.success(f"Saved {len(manager.all())} worker definition(s)")
                return manager
            else:
                return manager
        except (ValidationError, ValueError, KeyError) as exc:
            console.error(str(exc))
            console.pause()


def _ask_profile(settings: Settings, manager: WorkerManager,
                 worker_id: str = "") -> WorkerProfile:
    console.section("CREATE WORKER")
    locations = manager.locations.keys()
    if not locations:
        raise ValueError("Configure at least one location first")

    raw_id = console.ask("Worker ID", worker_id or new_worker_id())
    profile_id = validate_worker_id(raw_id)
    location = console.choose("Location:", locations, default=1)
    forum_mode = console.choose("Forum mode:", FORUM_MODES, default=4)
    specific = ""
    if forum_mode == ForumSelectionMode.SPECIFIC.value:
        specific = console.ask("Forum key", settings.forums.specific_forum,
                               required=True)

    posting = console.ask_bool("Posting enabled", settings.posting.enabled)
    replying = console.ask_bool("Replying enabled", settings.replies.enabled)
    min_post = console.ask_float("Minimum post interval (minutes)",
                                 settings.posting.min_interval_seconds / 60, minimum=0)
    max_post = console.ask_float("Maximum post interval (minutes)",
                                 settings.posting.max_interval_seconds / 60,
                                 minimum=min_post)
    min_reply = console.ask_float("Minimum reply interval (minutes)",
                                  settings.replies.min_interval_seconds / 60, minimum=0)
    max_reply = console.ask_float("Maximum reply interval (minutes)",
                                  settings.replies.max_interval_seconds / 60,
                                  minimum=min_reply)
    max_posts = console.ask_int("Maximum posts/session",
                                settings.posting.max_posts_per_session, minimum=0)
    max_replies = console.ask_int("Maximum replies/session",
                                  settings.replies.max_replies_per_session, minimum=0)
    scenario = console.ask("Scenario", settings.session.default_scenario)
    sessions = console.ask_int("Sessions for this worker",
                               settings.session.sessions_per_worker, minimum=1)

    return WorkerProfile(
        worker_id=profile_id,
        location_key=location,
        scenario=scenario,
        forum_mode=ForumSelectionMode(forum_mode),
        forum=specific,
        posting_enabled=posting,
        replying_enabled=replying,
        min_post_interval_seconds=min_post * 60,
        max_post_interval_seconds=max_post * 60,
        min_reply_interval_seconds=min_reply * 60,
        max_reply_interval_seconds=max_reply * 60,
        max_posts_per_session=max_posts,
        max_replies_per_session=max_replies,
        max_posts_per_day=settings.posting.max_posts_per_day,
        max_replies_per_day=settings.replies.max_replies_per_day,
        post_probability=settings.posting.probability,
        reply_probability=settings.replies.probability,
        sessions=sessions,
    )


def _create(settings: Settings, manager: WorkerManager) -> None:
    profile = _ask_profile(settings, manager)
    if console.ask_bool("Save worker?", True):
        manager.add(profile, replace=True)
        console.success(f"Worker {profile.worker_id} saved")
        print(console.keyvalues(profile.describe()))


def _create_bulk(settings: Settings, manager: WorkerManager) -> None:
    template = _ask_profile(settings, manager)
    count = console.ask_int("How many workers with this profile?", 5, minimum=1,
                            maximum=settings.concurrency.max_global_workers)
    created = 0
    for _ in range(count):
        data = template.model_dump()
        data["worker_id"] = new_worker_id()
        manager.add(WorkerProfile.model_validate(data), replace=True)
        created += 1
    console.success(f"Created {created} worker(s)")


def _show(manager: WorkerManager) -> None:
    worker_id = console.ask("Worker ID", required=True).upper()
    profile = manager.get(worker_id)
    console.section(f"WORKER {profile.worker_id}")
    print(console.keyvalues(profile.describe()))
    console.pause()


def _edit(manager: WorkerManager) -> None:
    worker_id = console.ask("Worker ID", required=True).upper()
    profile = manager.get(worker_id)
    data = profile.model_dump()
    data["posting_enabled"] = console.ask_bool("Posting enabled",
                                               profile.posting_enabled)
    data["replying_enabled"] = console.ask_bool("Replying enabled",
                                                profile.replying_enabled)
    data["max_posts_per_session"] = console.ask_int(
        "Maximum posts/session", profile.max_posts_per_session, minimum=0)
    data["max_replies_per_session"] = console.ask_int(
        "Maximum replies/session", profile.max_replies_per_session, minimum=0)
    data["scenario"] = console.ask("Scenario", profile.scenario)
    data["enabled"] = console.ask_bool("Worker enabled", profile.enabled)
    manager.add(WorkerProfile.model_validate(data), replace=True)
    console.success(f"Updated {worker_id}")


def _remove(manager: WorkerManager) -> None:
    worker_id = console.ask("Worker ID", required=True).upper()
    if console.ask_bool(f"Remove {worker_id}?", False):
        manager.remove(worker_id)
        console.success(f"Removed {worker_id}")


def _generate(settings: Settings, manager: WorkerManager) -> None:
    allocation = manager.locations.allocate(settings.concurrency.max_global_workers)
    profiles = manager.generate(allocation)
    console.success(f"Generated {len(profiles)} worker(s) from the location plan")
    rows = [{"location": key, "workers": count} for key, count in sorted(allocation.items())]
    print(console.table(rows))
    console.pause()
