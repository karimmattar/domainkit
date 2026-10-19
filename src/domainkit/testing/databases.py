"""Test databases: made for the run, migrated, and dropped when it ends.

``migrated_database`` gives the tests databases of their own, whatever the
test settings point at:

- PostgreSQL: a new ``test_<name>`` database on the same server, as Django
  names it, or ``TEST_NAME``. Under pytest-xdist, worker ``gw0`` gets
  ``test_<name>_gw0``. The schema owner (``MIGRATION_URL`` when set)
  creates it from the server's ``postgres`` database, and drops it once the
  run ends. Only names that start with ``test_`` are ever created or
  dropped; a database of that name left by an earlier run is replaced.
- SQLite in memory (``sqlite+aiosqlite:///:memory:``): a file in the run's
  temporary directory, so every connection sees the same tables.
- Any other URL is used as it is.

Every database is then migrated up, down to base, and up again, which
proves each revision reversible. With ``keepdb``, PostgreSQL test databases
are kept for the next run, which reuses them, migrations only go up, and
the rows are put back as ``migrate`` left them when the run ends.

Only test settings are accepted (``ENVIRONMENT = "test"``): the databases
are emptied, and the configured ones must never be the ones touched.
"""

import io
import os
import re
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Iterator,
    Mapping,
)
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest
from pydantic import SecretStr
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

from domainkit.conf import DEFAULT_DB_ALIAS, DatabaseConfig, override_settings, settings
from domainkit.db.drivers import connect_args
from domainkit.db.snapshots import restore_snapshot, take_snapshot
from domainkit.management import call_command
from domainkit.management.base import run_async

IN_MEMORY: Final = "sqlite+aiosqlite:///:memory:"
TEST_PREFIX: Final = "test_"
"""Every database the plugin creates or drops is named with it."""
WORKER_VARIABLE: Final = "PYTEST_XDIST_WORKER"
"""Set by pytest-xdist in each worker (``gw0``); read without needing xdist."""
MAX_NAME_BYTES: Final = 63
"""PostgreSQL cuts longer names short, so they are refused instead."""
MAINTENANCE_DATABASE: Final = "postgres"
"""Where databases are created and dropped from."""
URL_FIELDS: Final = ("URL", "MIGRATION_URL")
_WORKER: Final = re.compile(r"\w{1,32}", re.ASCII)
_EXISTS: Final = text("SELECT 1 FROM pg_database WHERE datname = :name")

type _Action = Callable[..., Awaitable[None]]


def worker_id() -> str | None:
    """Return the pytest-xdist worker's id (``gw0``), or None outside workers."""
    worker = os.environ.get(WORKER_VARIABLE)
    if not worker:
        return None
    if _WORKER.fullmatch(worker) is None:
        raise pytest.UsageError(
            f"{WORKER_VARIABLE} must be 1 to 32 letters, digits, or underscores."
        )
    return worker


def check_test_settings() -> None:
    """Refuse settings that are not the test settings, before any change."""
    if settings.ENVIRONMENT != "test":
        raise pytest.UsageError(
            f"The database fixtures empty every database in DATABASES, so they "
            f'need ENVIRONMENT = "test", not {settings.ENVIRONMENT!r}. Name the '
            f"test settings with domainkit_settings in pyproject.toml."
        )


def name_test_database(alias: str, config: DatabaseConfig, worker: str | None) -> str:
    """Return the name of ``alias``'s PostgreSQL test database.

    ``TEST_NAME``, or ``test_`` and the configured database's name; then
    ``_<worker>`` under pytest-xdist. Names that do not start with ``test_``
    are refused, so a database the tests did not make is never dropped.
    """
    name = config.TEST_NAME
    if name is None:
        configured = make_url(config.URL.get_secret_value()).database
        if not configured:
            raise pytest.UsageError(
                f"DATABASES[{alias!r}] names no database to derive the test "
                f"database's name from; set its TEST_NAME."
            )
        name = f"{TEST_PREFIX}{configured}"
    if worker is not None:
        name = f"{name}_{worker}"
    if not name.startswith(TEST_PREFIX):
        raise pytest.UsageError(
            f"DATABASES[{alias!r}]: refusing to create or drop {name!r}; test "
            f"databases are named {TEST_PREFIX}*."
        )
    if len(name.encode()) > MAX_NAME_BYTES:
        raise pytest.UsageError(
            f"DATABASES[{alias!r}]: the test database's name {name!r} is longer "
            f"than PostgreSQL's {MAX_NAME_BYTES} bytes; set a shorter TEST_NAME."
        )
    return name


@dataclass(frozen=True, slots=True)
class TestDatabase:
    """A PostgreSQL database made for the tests: its name, and how to reach it."""

    __test__ = False

    name: str
    config: DatabaseConfig
    """The entry pointed at it; its owner creates and drops it."""

    async def create(self, *, keep: bool) -> None:
        """Create the database; with ``keep``, an existing one is reused."""
        async with self._server() as server:
            exists = await server.scalar(_EXISTS, {"name": self.name}) is not None
            if exists and keep:
                return
            quoted = server.dialect.identifier_preparer.quote(self.name)
            if exists:
                await server.exec_driver_sql(f"DROP DATABASE {quoted} WITH (FORCE)")
            await server.exec_driver_sql(f"CREATE DATABASE {quoted}")

    async def drop(self) -> None:
        """Drop the database, ending the connections still open to it."""
        async with self._server() as server:
            quoted = server.dialect.identifier_preparer.quote(self.name)
            await server.exec_driver_sql(
                f"DROP DATABASE IF EXISTS {quoted} WITH (FORCE)"
            )

    @asynccontextmanager
    async def _server(self) -> AsyncIterator[AsyncConnection]:
        """Connect to the server's maintenance database as the schema owner."""
        owner = (self.config.MIGRATION_URL or self.config.URL).get_secret_value()
        engine = create_async_engine(
            make_url(owner).set(database=MAINTENANCE_DATABASE),
            isolation_level="AUTOCOMMIT",
            poolclass=NullPool,
            connect_args=connect_args(self.config, owner, timeouts=False),
        )
        try:
            async with engine.connect() as connection:
                yield connection
        finally:
            await engine.dispose()


def plan_databases(
    databases: Mapping[str, DatabaseConfig], scratch: Path, worker: str | None
) -> tuple[dict[str, DatabaseConfig], list[TestDatabase]]:
    """Point every entry at its test database; list the ones to create.

    Entries naming the same database on the same server share one.
    """
    replaced: dict[str, DatabaseConfig] = {}
    created: dict[tuple[str | None, int | None, str], TestDatabase] = {}
    for alias, config in databases.items():
        url = make_url(config.URL.get_secret_value())
        if url.get_backend_name() == "postgresql":
            name = name_test_database(alias, config, worker)
            replaced[alias] = _renamed(config, name)
            created.setdefault(
                (url.host, url.port, name), TestDatabase(name, replaced[alias])
            )
        else:
            file = "test" if alias == DEFAULT_DB_ALIAS else f"test_{alias}"
            replaced[alias] = _in_file(config, scratch / f"{file}.sqlite3")
    return replaced, list(created.values())


@contextmanager
def migrated_database(
    scratch: Path, *, keepdb: bool = False, worker: str | None = None
) -> Iterator[str]:
    """Make, migrate, and later drop the test databases; yield the default's URL.

    Each in-memory SQLite database becomes a file in ``scratch``
    (``test.sqlite3`` for ``default``, ``test_<alias>.sqlite3`` for others).
    ``worker`` is pytest-xdist's (``worker_id()``). Migrating down drops
    every table, so only test settings are accepted.
    """
    check_test_settings()
    databases, created = plan_databases(settings.DATABASES, scratch, worker)
    with override_settings(DATABASES=databases):
        try:
            run_async(_each(created, TestDatabase.create, keep=keepdb))
            migrate(round_trip=not keepdb)
            snapshot = run_async(take_snapshot()) if keepdb else None
            try:
                yield settings.DATABASES[DEFAULT_DB_ALIAS].URL.get_secret_value()
            finally:
                if snapshot is not None:
                    run_async(restore_snapshot(snapshot))
        finally:
            if not keepdb:
                run_async(_each(created, TestDatabase.drop))


def migrate(*, round_trip: bool) -> None:
    """Migrate every database up; with ``round_trip``, down to base and up again."""
    output = io.StringIO()
    call_command("migrate", stdout=output)
    if round_trip:
        call_command("migrate", "base", downgrade=True, stdout=output)
        call_command("migrate", stdout=output)


def _renamed(config: DatabaseConfig, name: str) -> DatabaseConfig:
    """``config`` with its URLs pointed at the database ``name``."""
    update = {
        field: SecretStr(
            make_url(value.get_secret_value())
            .set(database=name)
            .render_as_string(hide_password=False)
        )
        for field in URL_FIELDS
        if (value := getattr(config, field)) is not None
    }
    return config.model_copy(update=update)


def _in_file(config: DatabaseConfig, path: Path) -> DatabaseConfig:
    """``config`` with each in-memory SQLite URL replaced by the file at ``path``."""
    url = SecretStr(f"sqlite+aiosqlite:///{path}")
    update = {
        field: url
        for field in URL_FIELDS
        if (value := getattr(config, field)) is not None
        and value.get_secret_value() == IN_MEMORY
    }
    return config.model_copy(update=update)


async def _each(
    databases: Iterable[TestDatabase], action: _Action, **options: bool
) -> None:
    """Apply ``action`` to each test database in turn."""
    for database in databases:
        await action(database, **options)
