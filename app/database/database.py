"""Database engine and session management.

SQLite is the development default; any SQLAlchemy URL (PostgreSQL in
particular) works for production deployments.  Writes issued from the asyncio
worker loop go through :meth:`Database.run` which off-loads them to a thread so
the event loop is never blocked by disk IO.
"""

from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.models import Base
from app.utils.config import DatabaseConfig
from app.utils.logger import get_logger

T = TypeVar("T")
LOGGER = get_logger("database")


class Database:
    """Owns the SQLAlchemy engine plus a session factory."""

    def __init__(self, config: DatabaseConfig | None = None, *, url: str | None = None,
                 echo: bool | None = None) -> None:
        self.config = config or DatabaseConfig()
        self.url = url or self.config.url
        self.echo = self.config.echo if echo is None else echo
        self._engine: Engine | None = None
        self._factory: sessionmaker[OrmSession] | None = None
        self._write_lock = threading.Lock()

    # ------------------------------------------------------------------ engine
    @property
    def engine(self) -> Engine:
        """Lazily create (and cache) the engine."""
        if self._engine is None:
            self._engine = self._create_engine()
            self._factory = sessionmaker(bind=self._engine, expire_on_commit=False,
                                         future=True)
        return self._engine

    @property
    def factory(self) -> sessionmaker[OrmSession]:
        """Session factory bound to :attr:`engine`."""
        if self._factory is None:
            _ = self.engine
        assert self._factory is not None
        return self._factory

    def _create_engine(self) -> Engine:
        kwargs: dict[str, Any] = {"echo": self.echo, "future": True}
        if self.url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
            if ":memory:" in self.url:
                kwargs["poolclass"] = StaticPool
            else:
                path = self.url.split("///", 1)[-1]
                if path and path != ":memory:":
                    Path(path).parent.mkdir(parents=True, exist_ok=True)
        else:
            kwargs["pool_size"] = self.config.pool_size
            kwargs["max_overflow"] = self.config.max_overflow
            kwargs["pool_pre_ping"] = True

        engine = create_engine(self.url, **kwargs)

        if self.url.startswith("sqlite"):
            @event.listens_for(engine, "connect")
            def _sqlite_pragmas(dbapi_connection, _record):  # type: ignore[no-untyped-def]
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

        return engine

    # ------------------------------------------------------------------ schema
    def create_all(self) -> None:
        """Create every table that does not yet exist."""
        Base.metadata.create_all(self.engine)
        LOGGER.info("Database schema ensured at %s", self.url)

    def drop_all(self) -> None:
        """Drop the entire schema (destructive; used by tests and ``reset``)."""
        Base.metadata.drop_all(self.engine)

    def healthcheck(self) -> bool:
        """Return True when the database answers a trivial query."""
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return True
        except Exception as exc:  # pragma: no cover - depends on environment
            LOGGER.error("Database healthcheck failed: %s", exc)
            return False

    # ----------------------------------------------------------------- session
    @contextmanager
    def session(self) -> Iterator[OrmSession]:
        """Context-managed session with commit/rollback handling."""
        session = self.factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def write(self, operation: Callable[[OrmSession], T]) -> T:
        """Run *operation* inside a serialized write transaction."""
        with self._write_lock:
            with self.session() as session:
                return operation(session)

    def read(self, operation: Callable[[OrmSession], T]) -> T:
        """Run a read-only *operation*."""
        with self.session() as session:
            return operation(session)

    async def run(self, operation: Callable[[OrmSession], T]) -> T:
        """Await *operation* on a worker thread (safe from the asyncio loop)."""
        return await asyncio.to_thread(self.write, operation)

    def dispose(self) -> None:
        """Close pooled connections."""
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None
            self._factory = None


_DEFAULT: Database | None = None


def get_database(config: DatabaseConfig | None = None, *, refresh: bool = False) -> Database:
    """Return the process-wide :class:`Database` singleton."""
    global _DEFAULT
    if _DEFAULT is None or refresh:
        _DEFAULT = Database(config)
    return _DEFAULT


def reset_database_singleton() -> None:
    """Drop the cached singleton (used by tests)."""
    global _DEFAULT
    if _DEFAULT is not None:
        _DEFAULT.dispose()
    _DEFAULT = None


def init_database(config: DatabaseConfig | None = None) -> Database:
    """Create the schema and return the ready database handle."""
    database = get_database(config, refresh=True)
    database.create_all()
    return database
