"""Lightweight schema versioning.

Alembic is the right tool for a large production deployment; Social Worker
ships a minimal additive migration runner so a single-file SQLite install can
be upgraded in place without extra tooling.  Each migration is an idempotent
callable keyed by an integer version stored in the ``settings`` table.
"""

from __future__ import annotations

from typing import Callable

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session as OrmSession

from app.database.database import Database
from app.database.models import SettingRecord
from app.utils.logger import get_logger
from app.utils.time_utils import utc_now

LOGGER = get_logger("database.migrations")

SCHEMA_VERSION_KEY = "schema_version"
CURRENT_VERSION = 1

Migration = Callable[[Database], None]


def _get_version(database: Database) -> int:
    def _read(session: OrmSession) -> int:
        record = session.query(SettingRecord).filter_by(key=SCHEMA_VERSION_KEY).one_or_none()
        if record is None:
            return 0
        return int(record.value.get("version", 0))

    try:
        return database.read(_read)
    except Exception:
        return 0


def _set_version(database: Database, version: int) -> None:
    def _write(session: OrmSession) -> None:
        record = session.query(SettingRecord).filter_by(key=SCHEMA_VERSION_KEY).one_or_none()
        payload = {"version": version, "applied_at": utc_now().isoformat()}
        if record is None:
            session.add(SettingRecord(key=SCHEMA_VERSION_KEY, value=payload))
        else:
            record.value = payload

    database.write(_write)


def _migration_001_baseline(database: Database) -> None:
    """Baseline: ensure every declared table exists."""
    database.create_all()


MIGRATIONS: dict[int, Migration] = {
    1: _migration_001_baseline,
}


def upgrade(database: Database) -> int:
    """Apply pending migrations; returns the resulting schema version."""
    database.create_all()
    version = _get_version(database)
    for target in sorted(MIGRATIONS):
        if target > version:
            LOGGER.info("Applying migration %03d", target)
            MIGRATIONS[target](database)
            version = target
    _set_version(database, version)
    return version


def describe(database: Database) -> dict[str, list[str]]:
    """Return a table -> column-name mapping for the live schema."""
    inspector = inspect(database.engine)
    return {
        table: [column["name"] for column in inspector.get_columns(table)]
        for table in sorted(inspector.get_table_names())
    }


def vacuum(database: Database) -> None:
    """Compact a SQLite database file."""
    if not database.url.startswith("sqlite"):
        return
    with database.engine.connect() as connection:
        connection.execute(text("VACUUM"))
