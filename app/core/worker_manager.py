"""Worker profile management: generation, persistence and DB synchronisation."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Sequence

from app.database.database import Database
from app.database.models import Worker as WorkerRow
from app.locations.allocation import Allocation
from app.locations.location_manager import LocationManager
from app.utils.config import (
    ConfigPaths, Settings, load_yaml_document, save_yaml_document,
)
from app.utils.ids import new_worker_id, reset_worker_ids
from app.utils.logger import get_logger
from app.workers.worker import Worker
from app.workers.worker_state import WorkerProfile

LOGGER = get_logger("workers.manager")


class WorkerManager:
    """Builds worker profiles from the location plan or from ``workers.yaml``."""

    def __init__(self, settings: Settings, location_manager: LocationManager) -> None:
        self.settings = settings
        self.locations = location_manager
        self.profiles: dict[str, WorkerProfile] = {}

    # ------------------------------------------------------------------- I/O
    def load(self, path: str | Path | None = None) -> list[WorkerProfile]:
        """Load explicit worker definitions from ``config/workers.yaml``."""
        target = Path(path or ConfigPaths().workers)
        document = load_yaml_document(target)
        raw = document.get("workers", [])
        if isinstance(raw, dict):
            raw = [{"worker_id": key, **(value or {})} for key, value in raw.items()]
        profiles = [WorkerProfile.model_validate(item) for item in raw]
        for profile in profiles:
            self.profiles[profile.worker_id] = profile
        if profiles:
            LOGGER.info("Loaded %d worker definition(s) from %s", len(profiles), target)
        return profiles

    def save(self, path: str | Path | None = None) -> Path:
        """Persist the current worker definitions."""
        target = Path(path or ConfigPaths().workers)
        payload = {"workers": [profile.model_dump(mode="json")
                               for profile in self.all()]}
        save_yaml_document(target, payload)
        LOGGER.info("Saved %d worker definition(s) to %s", len(self.profiles), target)
        return target

    # -------------------------------------------------------------- profiles
    def add(self, profile: WorkerProfile, *, replace: bool = False) -> WorkerProfile:
        """Register a worker profile."""
        if profile.worker_id in self.profiles and not replace:
            raise ValueError(f"Worker {profile.worker_id} already exists")
        self.profiles[profile.worker_id] = profile
        return profile

    def remove(self, worker_id: str) -> None:
        """Delete a worker profile."""
        self.profiles.pop(worker_id, None)

    def get(self, worker_id: str) -> WorkerProfile:
        """Return a worker profile."""
        try:
            return self.profiles[worker_id]
        except KeyError as exc:
            raise KeyError(f"Unknown worker: {worker_id}") from exc

    def all(self) -> list[WorkerProfile]:
        """All worker profiles sorted by id."""
        return [self.profiles[key] for key in sorted(self.profiles)]

    def for_location(self, location_key: str) -> list[WorkerProfile]:
        """Profiles assigned to a location."""
        return [profile for profile in self.all()
                if profile.location_key == location_key]

    def template(self, location_key: str, worker_id: str = "") -> WorkerProfile:
        """Build a profile pre-filled from the global behaviour settings."""
        posting = self.settings.posting
        replies = self.settings.replies
        return WorkerProfile(
            worker_id=worker_id or new_worker_id(),
            location_key=location_key,
            scenario=self.settings.session.default_scenario,
            forum_mode=self.settings.forums.mode,
            forum=self.settings.forums.specific_forum,
            posting_enabled=posting.enabled,
            replying_enabled=replies.enabled,
            min_post_interval_seconds=posting.min_interval_seconds,
            max_post_interval_seconds=posting.max_interval_seconds,
            min_reply_interval_seconds=replies.min_interval_seconds,
            max_reply_interval_seconds=replies.max_interval_seconds,
            max_posts_per_session=posting.max_posts_per_session,
            max_replies_per_session=replies.max_replies_per_session,
            max_posts_per_day=posting.max_posts_per_day,
            max_replies_per_day=replies.max_replies_per_day,
            post_probability=posting.probability,
            reply_probability=replies.probability,
            sessions=self.settings.session.sessions_per_worker,
        )

    def generate(self, allocation: Allocation, *, reset_ids: bool = True,
                 keep_existing: bool = False) -> list[WorkerProfile]:
        """Create one profile per allocated worker slot."""
        if reset_ids:
            reset_worker_ids()
        if not keep_existing:
            self.profiles.clear()
        generated: list[WorkerProfile] = []
        for location_key, count in sorted(allocation.items()):
            for _ in range(count):
                profile = self.template(location_key)
                self.profiles[profile.worker_id] = profile
                generated.append(profile)
        LOGGER.info("Generated %d worker profile(s) across %d location(s)",
                    len(generated), len(allocation.per_location))
        return generated

    def plan(self, *, global_limit: int | None = None) -> list[WorkerProfile]:
        """Return the profiles to run: explicit definitions, else generated ones."""
        limit = global_limit or self.settings.concurrency.max_global_workers
        explicit = [profile for profile in self.all() if profile.enabled]
        if explicit:
            valid = [profile for profile in explicit
                     if profile.location_key in self.locations.keys()]
            dropped = len(explicit) - len(valid)
            if dropped:
                LOGGER.warning("%d worker(s) reference unknown locations and were skipped",
                               dropped)
            if len(valid) > limit:
                LOGGER.warning("Trimming worker plan from %d to the global limit of %d",
                               len(valid), limit)
                valid = valid[:limit]
            return valid
        return self.generate(self.locations.allocate(limit))

    # -------------------------------------------------------------- database
    async def persist(self, database: Database, test_run_id: str,
                      profiles: Sequence[WorkerProfile] | None = None) -> int:
        """Insert the worker rows for this test run."""
        targets = list(profiles if profiles is not None else self.all())

        def _write(session: Any) -> int:
            for profile in targets:
                existing = session.query(WorkerRow).filter_by(
                    test_run_id=test_run_id, worker_id=profile.worker_id).one_or_none()
                if existing is None:
                    existing = WorkerRow(test_run_id=test_run_id,
                                         worker_id=profile.worker_id)
                    session.add(existing)
                existing.location_key = profile.location_key
                existing.scenario = profile.scenario
                existing.status = "READY"
                existing.posting_enabled = profile.posting_enabled
                existing.replying_enabled = profile.replying_enabled
                existing.min_post_interval_seconds = profile.min_post_interval_seconds
                existing.max_post_interval_seconds = profile.max_post_interval_seconds
                existing.min_reply_interval_seconds = profile.min_reply_interval_seconds
                existing.max_reply_interval_seconds = profile.max_reply_interval_seconds
                existing.max_posts_per_session = profile.max_posts_per_session
                existing.max_replies_per_session = profile.max_replies_per_session
            return len(targets)

        written = await database.run(_write)
        LOGGER.info("Persisted %d worker row(s) for %s", written, test_run_id)
        return written

    async def sync_runtime(self, database: Database, test_run_id: str,
                           workers: Iterable[Worker]) -> int:
        """Write live worker counters back to the database."""
        payload = [
            {
                "worker_id": worker.worker_id,
                "status": worker.runtime.status.value,
                "current_action": worker.runtime.action.value,
                "sessions_completed": worker.runtime.sessions_completed,
                "posts_created": worker.runtime.posts_created,
                "replies_created": worker.runtime.replies_created,
                "errors": worker.runtime.errors,
            }
            for worker in workers
        ]

        def _write(session: Any) -> int:
            from app.utils.time_utils import utc_now

            for item in payload:
                row = session.query(WorkerRow).filter_by(
                    test_run_id=test_run_id, worker_id=item["worker_id"]).one_or_none()
                if row is None:
                    continue
                row.status = item["status"]
                row.current_action = item["current_action"]
                row.sessions_completed = item["sessions_completed"]
                row.posts_created = item["posts_created"]
                row.replies_created = item["replies_created"]
                row.errors = item["errors"]
                row.last_heartbeat = utc_now()
            return len(payload)

        return await database.run(_write)

    def summary_rows(self) -> list[dict[str, Any]]:
        """Rows for the CLI worker table."""
        return [
            {
                "worker_id": profile.worker_id,
                "location": profile.location_key,
                "scenario": profile.scenario,
                "posting": profile.posting_enabled,
                "replying": profile.replying_enabled,
                "max_posts": profile.max_posts_per_session,
                "max_replies": profile.max_replies_per_session,
                "enabled": profile.enabled,
            }
            for profile in self.all()
        ]
