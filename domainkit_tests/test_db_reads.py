"""Read-only units of work: how each reads, what it costs, and what it refuses.

``Reads.STATEMENTS`` runs each statement on its own on PostgreSQL, without
``BEGIN`` and ``ROLLBACK``; ``Reads.SNAPSHOT`` reads one snapshot. Both
refuse writes, and the first is cut off at the deadline, survives the
server closing its connections, never runs a statement twice, and hands
the next caller a session as it was made. Pre-ping costs one round trip.
"""

import asyncio
import os
import re
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import delete, event, func, insert, make_url, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    create_async_engine,
)
from sqlalchemy.pool import PoolResetState
from sqlalchemy.schema import CreateTable
from sqlalchemy.sql import Executable
from sqlalchemy.util import greenlet_spawn

from domainkit import deadlines, setup
from domainkit.apps import registry
from domainkit.clock import freeze_time
from domainkit.conf import DatabaseConfig, override_settings
from domainkit.db import Reads, connections
from domainkit.db.connections import (
    READS_SUFFIX,
    RESET_SESSION,
    ReadOnlySession,
    _closed,
    _plain_select,
    _reset_session,
    _sent,
    create_engine,
    create_read_engine,
    reads_alone,
)
from domainkit.db.connections import (
    _ping as ping,
)
from domainkit.db.drivers import connect_args
from domainkit.ddd import ReadOnlyError
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import get_asgi_application
from domainkit.testing import AsyncTestClient, assert_num_queries
from domainkit_tests.conftest import POSTGRES_VARIABLE
from domainkit_tests.gadgets import (
    GadgetsUnitOfWork,
    gadgets,
    make_gadget,
    metadata,
)

pytestmark = pytest.mark.anyio
ON_POSTGRES = bool(os.environ.get(POSTGRES_VARIABLE))
postgres_only = pytest.mark.skipif(not ON_POSTGRES, reason=f"set {POSTGRES_VARIABLE}")
asyncpg_only = pytest.mark.skipif(
    "+asyncpg" not in os.environ.get(POSTGRES_VARIABLE, ""),
    reason="reads asyncpg's own ping",
)
ROUTES = "domainkit_tests.reading_routes.router"
MIDDLEWARE = [
    "domainkit.http.middleware.RequestContextMiddleware",
    "domainkit.http.deadlines.DeadlineMiddleware",
]
WRITE_REFUSED = "25006"
"""PostgreSQL's read_only_sql_transaction."""
PROBE = "SELECT set_config('domainkit.probe', 'set', true)"
"""Set a value for the rest of the transaction (``SET LOCAL``)."""
PROBED = "SELECT current_setting('domainkit.probe', true)"
COUNT_RUNS = (
    "SELECT set_config('domainkit.runs', (coalesce(nullif("
    "current_setting('domainkit.runs', true), ''), '0')::int + 1)::text, false)"
)
"""Count, in the session, how many times the server runs this statement."""
RUNS = "SELECT current_setting('domainkit.runs', true)"


@pytest.fixture
def database(database_url: str) -> Iterator[str]:
    """An empty database, selected in the settings."""
    with override_settings(DATABASES={"default": {"URL": database_url}}):
        yield database_url


@pytest.fixture
async def tables(database: str) -> AsyncIterator[None]:
    """The gadget tables, with one gadget, in the running loop's engine."""
    async with connections.engine().begin() as connection:
        await connection.run_sync(metadata.create_all)
    await store("first")
    yield
    await connections.close_all()


@pytest.fixture
async def sqlite_tables(tmp_path: Path) -> AsyncIterator[None]:
    """The gadget tables on SQLite, whichever database the suite runs on.

    For what only SQLite does: these tests run in the PostgreSQL job too,
    which allows no skips.
    """
    url = f"sqlite+aiosqlite:///{tmp_path / 'reads.sqlite3'}"
    with override_settings(DATABASES={"default": {"URL": url}}):
        async with connections.engine().begin() as connection:
            await connection.run_sync(metadata.create_all)
        await store("first")
        yield
        await connections.close_all()


async def store(*names: str) -> None:
    """Add gadgets named ``names``, committed."""
    async with GadgetsUnitOfWork() as uow:
        for name in names:
            await uow.gadgets.add(make_gadget(name))
        await uow.commit()


async def names() -> list[str]:
    """Every gadget's name, read in a writable unit of work."""
    async with GadgetsUnitOfWork() as uow:
        return sorted(gadget.name for gadget in await uow.gadgets.filter())


async def in_transaction(executor: AsyncSession | AsyncConnection) -> bool:
    """Whether the statements run through ``executor`` share a transaction.

    On PostgreSQL, a value set for the rest of the transaction is gone by
    the next statement when each runs on its own.
    """
    await executor.execute(text(PROBE))
    return bool(await executor.scalar(text(PROBED)) == "set")


async def terminate_other_backends(database_url: str) -> int:
    """Close every other connection to the test's database, as a restart would.

    Each is waited for until its process has ended (up to 5 seconds).
    """
    admin = create_async_engine(database_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as connection:
            return int(
                await connection.scalar(
                    text(
                        "SELECT count(pg_terminate_backend(pid, 5000)) "
                        "FROM pg_stat_activity "
                        "WHERE datname = current_database() "
                        "AND pid <> pg_backend_pid()"
                    )
                )
                or 0
            )
    finally:
        await admin.dispose()


async def busy_backends(database_url: str) -> int:
    """Other connections to the test's database that are running a statement."""
    admin = create_async_engine(database_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as connection:
            return int(
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = "
                        "current_database() AND pid <> pg_backend_pid() "
                        "AND state <> 'idle'"
                    )
                )
                or 0
            )
    finally:
        await admin.dispose()


class Connects:
    """Count the connections every pool opens while entered."""

    def __init__(self) -> None:
        """Start at none."""
        self.count = 0

    def _connected(self, *args: object) -> None:
        self.count += 1

    def __enter__(self) -> "Connects":
        """Start counting."""
        from sqlalchemy.pool import Pool

        event.listen(Pool, "connect", self._connected)
        return self

    def __exit__(self, *exc: object) -> None:
        """Stop counting."""
        from sqlalchemy.pool import Pool

        event.remove(Pool, "connect", self._connected)


# Choosing how to read -------------------------------------------------------------


def test_reads_make_the_unit_of_work_read_only() -> None:
    """Any ``reads`` makes it read-only; a subclass may say so once."""

    class Reports(GadgetsUnitOfWork):
        reads = Reads.STATEMENTS

    assert GadgetsUnitOfWork(reads="statements").reads is Reads.STATEMENTS
    assert GadgetsUnitOfWork(reads=Reads.SNAPSHOT).readonly is True
    assert GadgetsUnitOfWork(reads=Reads.TRANSACTION).readonly is True
    assert Reports().readonly is True
    assert Reports(reads=Reads.SNAPSHOT).reads is Reads.SNAPSHOT
    plain = GadgetsUnitOfWork()
    assert (plain.readonly, plain.reads) == (False, Reads.TRANSACTION)
    writable = GadgetsUnitOfWork(readonly=False, reads=Reads.TRANSACTION)
    assert writable.readonly is False


def test_work_that_writes_always_has_a_transaction() -> None:
    """``readonly=False`` with another way of reading is refused, as is a typo."""

    class Reports(GadgetsUnitOfWork):
        reads = Reads.SNAPSHOT

    for reads, name in ((Reads.STATEMENTS, "statements"), ("snapshot", "snapshot")):
        refused = (
            f"reads='{name}' is for read-only units of work; "
            "one that writes always runs in a transaction."
        )
        with pytest.raises(ValueError, match=f"^{re.escape(refused)}$"):
            GadgetsUnitOfWork(readonly=False, reads=reads)
    with pytest.raises(ValueError, match="for read-only units of work"):
        Reports(readonly=False)
    with pytest.raises(ValueError, match="'dirty' is not a valid Reads"):
        GadgetsUnitOfWork(reads="dirty")


def test_only_postgresql_outside_pgbouncer_reads_alone() -> None:
    """PostgreSQL through asyncpg or psycopg can, unless through PgBouncer."""

    def alone(url: str, **options: object) -> bool:
        return reads_alone(DatabaseConfig.model_validate({"URL": url, **options}))

    assert alone("postgresql+asyncpg://app@db/app")
    assert alone("postgresql+psycopg://app@db/app")
    assert not alone("postgresql+asyncpg://app@db/app", PGBOUNCER=True)
    assert not alone("postgresql+pg8000://app@db/app")
    assert not alone("sqlite+aiosqlite:///x.sqlite3")


# Reading on its own ---------------------------------------------------------------


@postgres_only
async def test_statements_read_without_a_transaction(tables: None) -> None:
    """Its statements, no BEGIN or ROLLBACK, on a read-only pool of its own.

    A gadget comes with its parts and maker: three statements.
    """
    with assert_num_queries(3) as queries:
        async with GadgetsUnitOfWork(reads=Reads.STATEMENTS) as uow:
            [gadget] = await uow.gadgets.filter()
    assert gadget.name == "first"
    assert "gadgets" in queries.statements[0]
    assert connections.pool_stats()[f"default{READS_SUFFIX}"]["checked_out"] == 0

    async with GadgetsUnitOfWork(reads=Reads.STATEMENTS) as uow:
        assert not await in_transaction(uow.session)
        await uow.session.execute(text(COUNT_RUNS))  # Sent once.
        options = {"no_parameters": True}  # Sent as is, through its own path.
        await uow.session.execute(text(COUNT_RUNS), execution_options=options)
        assert await uow.session.scalar(text(RUNS)) == "2"
    for reads in (Reads.TRANSACTION, Reads.SNAPSHOT):
        async with GadgetsUnitOfWork(reads=reads) as uow:
            assert await in_transaction(uow.session)


@postgres_only
async def test_statements_read_in_a_transaction_through_pgbouncer(
    database: str,
) -> None:
    """PgBouncer keeps no timeouts in the session: the transaction sets them."""
    with override_settings(DATABASES={"default": {"URL": database, "PGBOUNCER": True}}):
        async with GadgetsUnitOfWork(reads=Reads.STATEMENTS) as uow:
            assert await in_transaction(uow.session)
            with pytest.raises(DBAPIError) as refused:
                await uow.session.execute(text("CREATE TABLE notes (id int)"))
        assert f"default{READS_SUFFIX}" not in connections.pool_stats()
        await connections.close_all()
    assert getattr(refused.value.orig, "sqlstate", None) == WRITE_REFUSED


async def test_sqlite_reads_on_the_alias_itself(sqlite_tables: None) -> None:
    """SQLite begins no transaction to read: there is no other pool to use."""
    with assert_num_queries(3):
        async with GadgetsUnitOfWork(reads=Reads.STATEMENTS) as uow:
            [gadget] = await uow.gadgets.filter()
            assert isinstance(uow.session.sync_session, ReadOnlySession)
    assert gadget.name == "first"
    assert list(connections.pool_stats()) == ["default"]


@postgres_only
async def test_statements_see_what_each_finds_committed(tables: None) -> None:
    """Without a transaction, two statements need not see the same data."""
    async with GadgetsUnitOfWork(reads=Reads.STATEMENTS) as uow:
        assert len(await uow.gadgets.filter()) == 1
        await store("second")
        assert len(await uow.gadgets.filter()) == 2


async def test_a_snapshot_reads_the_data_as_it_was(tables: None) -> None:
    """Every statement of a snapshot sees what the first one saw."""
    async with GadgetsUnitOfWork(reads=Reads.SNAPSHOT) as uow:
        assert len(await uow.gadgets.filter()) == 1
        if ON_POSTGRES:  # SQLite's writer would wait for the reader to finish.
            await store("second")
            assert len(await uow.gadgets.filter()) == 1
    if ON_POSTGRES:
        assert await names() == ["first", "second"]


async def test_read_only_work_on_sqlite_begins_before_it_reads(
    sqlite_tables: None,
) -> None:
    """SQLite's driver begins only before writes; read-only sessions begin themselves.

    Every way of reading, so a snapshot's reads agree and the rollback
    that ends the unit of work undoes what SQL text wrote. The ``BEGIN``
    is the framework's: it is not counted.
    """

    async def driver_in_transaction(uow: GadgetsUnitOfWork) -> bool:
        connection = await uow.session.connection()
        driver: Any = connection.sync_connection.connection.driver_connection  # type: ignore[union-attr]
        return bool(driver.in_transaction)

    for reads in Reads:
        with assert_num_queries(3):
            async with GadgetsUnitOfWork(reads=reads) as uow:
                await uow.gadgets.filter()
                assert await driver_in_transaction(uow), reads
    async with GadgetsUnitOfWork() as uow:
        await uow.gadgets.filter()
        assert not await driver_in_transaction(uow)  # A writer begins to write.


@pytest.mark.parametrize("reads", list(Reads))
async def test_sql_text_that_writes_in_read_only_work_never_lasts(
    tables: None, reads: Reads
) -> None:
    """DDL and ``WITH ... INSERT`` sent as SQL text: refused, or rolled back.

    PostgreSQL refuses them in a read-only transaction or session. SQLite's
    driver would commit them at once, outside a transaction; read-only work
    begins one there, which its end rolls back.
    """
    async with connections.engine().begin() as connection:
        await connection.execute(text("CREATE TABLE framework_test_kept (a int)"))
    statements = [
        (
            "WITH added AS (SELECT 1 AS a) "
            "INSERT INTO framework_test_kept SELECT a FROM added"
        ),
        "CREATE TABLE framework_test_made (a int)",
    ]
    for statement in statements:
        async with GadgetsUnitOfWork(reads=reads) as uow:
            if ON_POSTGRES:
                with pytest.raises(DBAPIError) as refused:
                    await uow.session.execute(text(statement))
                assert getattr(refused.value.orig, "sqlstate", None) == WRITE_REFUSED
            else:
                await uow.session.execute(text(statement))
    async with connections.engine().connect() as connection:
        kept = await connection.scalar(text("SELECT count(*) FROM framework_test_kept"))
        made = await connection.scalar(
            text(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_name = 'framework_test_made'"
            )
            if ON_POSTGRES
            else text("SELECT count(*) FROM sqlite_master WHERE name LIKE '%test_made'")
        )
    assert (kept, made) == (0, 0)


# Refusing writes ------------------------------------------------------------------


@pytest.mark.parametrize("reads", [Reads.STATEMENTS, Reads.SNAPSHOT])
async def test_reading_sessions_refuse_to_write(tables: None, reads: Reads) -> None:
    """A flush, a statement that writes, or a schema change fails at once."""
    statements: list[Executable] = [
        insert(gadgets).values(name="x"),
        update(gadgets).values(name="x"),
        delete(gadgets),
        CreateTable(gadgets),
    ]
    with assert_num_queries(0):
        async with GadgetsUnitOfWork(reads=reads) as uow:
            await uow.gadgets.add(make_gadget("added"))
            with pytest.raises(ReadOnlyError, match="cannot save changes"):
                await uow.gadgets.filter()  # Flushes the new gadget first.
            with pytest.raises(ReadOnlyError, match="cannot save changes"):
                await uow.session.flush()
            for statement in statements:
                with pytest.raises(ReadOnlyError, match="cannot write"):
                    await uow.session.execute(statement)
            with pytest.raises(ReadOnlyError, match="is read-only"):
                await uow.commit()
    assert await names() == ["first"]


@postgres_only
@pytest.mark.parametrize("reads", [Reads.STATEMENTS, Reads.SNAPSHOT])
async def test_postgresql_refuses_writes_however_sent(
    database: str, reads: Reads
) -> None:
    """SQL text is refused by the server; CONNECT_ARGS cannot turn that off."""
    reads_on = {
        "asyncpg": {"server_settings": {"default_transaction_read_only": "off"}},
        "psycopg": {"options": "-c default_transaction_read_only=off"},
    }[make_url(database).get_driver_name()]
    with override_settings(
        DATABASES={"default": {"URL": database, "CONNECT_ARGS": reads_on}}
    ):
        async with connections.engine().begin() as connection:
            await connection.run_sync(metadata.create_all)
        await store("first")
        async with GadgetsUnitOfWork(reads=reads) as uow:
            with pytest.raises(DBAPIError) as refused:
                await uow.session.execute(
                    text("UPDATE framework_test_gadgets SET name = 'x'")
                )
        assert getattr(refused.value.orig, "sqlstate", None) == WRITE_REFUSED
        assert await names() == ["first"]
        await connections.close_all()


# Deadlines --------------------------------------------------------------------------


@asynccontextmanager
async def reading_app(
    database_url: str,
    **changes: Any,  # noqa: ANN401 - settings.
) -> AsyncIterator[AsyncTestClient]:
    """The reading routes on ``database_url``, with gadget tables and a gadget."""
    registry.clear()
    with override_settings(
        DATABASES={"default": {"URL": database_url}},
        ROOT_ROUTER=ROUTES,
        MIDDLEWARE=MIDDLEWARE,
        **changes,
    ):
        setup()
        async with connections.engine().begin() as connection:
            await connection.run_sync(metadata.create_all)
        await store("first")
        try:
            async with AsyncTestClient(
                get_asgi_application(), lifespan=False
            ) as client:
                yield client
        finally:
            await connections.close_all()
            registry.clear()


@postgres_only
async def test_a_slow_read_is_cut_off_at_the_request_deadline(
    database_url: str,
) -> None:
    """The request answers 503, and the server stops the statement."""
    async with reading_app(database_url, REQUEST_TIMEOUT=0.3) as client:
        response = await client.get("/slow", params={"reads": "statements"})

        assert response.status_code == 503
        assert response.json()["code"] == "deadline_exceeded"
        assert connections.pool_stats()[f"default{READS_SUFFIX}"]["checked_out"] == 0
        after = await client.get("/gadgets", params={"reads": "statements"})
        assert after.json() == ["first"]


@postgres_only
async def test_a_read_sharing_a_deadline_is_cut_off_at_it(
    tables: None, database: str
) -> None:
    """A task under a deadline it does not enforce still stops at it.

    The task shares the block's deadline but not its timer, which cancels
    only the block's task: the statement keeps to the deadline itself.
    """

    async def read() -> None:
        async with GadgetsUnitOfWork(reads=Reads.STATEMENTS) as uow:
            await uow.session.execute(text("SELECT pg_sleep(30)"))

    started: list[asyncio.Task[None]] = []

    async def share_the_deadline() -> None:
        async with deadlines.timeout(0.3):
            started.append(asyncio.create_task(read()))
            await asyncio.wait(started)

    with pytest.raises(TimeoutError):  # The block's own seconds ran out.
        await share_the_deadline()
    [reading] = started
    with pytest.raises(deadlines.DeadlineExceeded, match="The deadline passed"):
        await reading
    # The pool's one connection is kept; it waits for the server to stop.
    async with GadgetsUnitOfWork(reads=Reads.STATEMENTS) as uow:
        assert len(await uow.gadgets.filter()) == 1
    assert await busy_backends(database) == 0


@postgres_only
async def test_a_failing_read_under_a_deadline_raises_its_own_error(
    tables: None,
) -> None:
    """The deadline bounds the statement; it does not hide the database's error.

    In the task whose timer enforces the deadline, and in a task that only
    shares it, where the statement keeps to the deadline itself.
    """

    async def read() -> int:
        async with GadgetsUnitOfWork(reads=Reads.STATEMENTS) as uow:
            with pytest.raises(DBAPIError, match="division by zero"):
                await uow.session.execute(text("SELECT 1 / 0"))
            return len(await uow.gadgets.filter())

    async with GadgetsUnitOfWork(reads=Reads.STATEMENTS) as uow:
        await uow.gadgets.filter()  # The pool's connection is made.
    with Connects() as connects:
        async with deadlines.timeout(10.0):
            assert await read() == 1
            assert await asyncio.create_task(read()) == 1
    assert connects.count == 0  # The same connection, kept: no retry.


# Connections the server closed --------------------------------------------------------


@postgres_only
async def test_reads_survive_the_server_closing_connections_between_requests(
    database_url: str,
) -> None:
    """Each way of reading gets its answer, on a fresh connection.

    Reads on their own replace a connection the driver knows is closed as
    they take it from the pool; the others' connections are tested with
    one round trip.
    """
    async with reading_app(database_url) as client:
        for reads in ("statements", "snapshot", "transaction", None):
            params: dict[str, str] = {} if reads is None else {"reads": reads}
            assert (await client.get("/gadgets", params=params)).json() == ["first"]
        assert await terminate_other_backends(database_url) >= 2

        with Connects() as connects:
            for reads in ("statements", "snapshot", "transaction", None):
                params = {} if reads is None else {"reads": reads}
                response = await client.get("/gadgets", params=params)
                assert response.json() == ["first"], reads
        assert connects.count == 2  # One per pool, each replaced once.


async def hold_connections(count: int) -> list[asyncio.Event]:
    """Leave ``count`` idle connections in the read pool; an event each when closed.

    The units of work hold their connections until all have one, so the
    pool makes ``count`` of them.
    """
    closed: list[asyncio.Event] = []
    all_in = asyncio.Event()

    async def hold() -> None:
        async with GadgetsUnitOfWork(reads=Reads.STATEMENTS) as uow:
            await uow.gadgets.filter()
            raw = await (await uow.session.connection()).get_raw_connection()
            event = asyncio.Event()
            raw.driver_connection.add_termination_listener(lambda _: event.set())  # type: ignore[union-attr]
            closed.append(event)
            if len(closed) == count:
                all_in.set()
            await all_in.wait()

    await asyncio.gather(*(hold() for _ in range(count)))
    return closed


@asyncpg_only
async def test_reads_survive_every_idle_connection_being_closed(
    database: str,
) -> None:
    """A restart closes all of them: each is replaced as it is taken, none fails.

    The driver has seen every connection close before the reads start, as
    it does once the server's goodbye arrives. The reads come one at a
    time first: a read that replaced only its own connection would retry
    on the next idle one, closed too.
    """
    with override_settings(DATABASES={"default": {"URL": database, "POOL_SIZE": 4}}):
        async with connections.engine().begin() as connection:
            await connection.run_sync(metadata.create_all)
        await store("first")
        closed = await hold_connections(4)
        assert await terminate_other_backends(database) >= 4
        await asyncio.gather(*(event.wait() for event in closed))

        async def read() -> list[str]:
            async with GadgetsUnitOfWork(reads=Reads.STATEMENTS) as uow:
                return [gadget.name for gadget in await uow.gadgets.filter()]

        with Connects() as connects:
            one_by_one = [await read() for _ in range(4)]
            together = await asyncio.gather(*(read() for _ in range(4)))
        await connections.close_all()

    assert one_by_one == list(together) == [["first"]] * 4
    assert connects.count == 4  # Each closed connection replaced once.


async def wait_for_lock_waiter(
    admin: AsyncConnection, key: int, *seen: int, until: asyncio.Event | None = None
) -> int | None:
    """The backend waiting for advisory lock ``key``, other than ``seen``.

    It asks the server until one is waiting, or ``until`` is set (None).
    """
    query = text(
        "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND NOT granted "
        "AND objid = :key AND NOT pid = ANY(:seen)"
    )
    while until is None or not until.is_set():
        pid = await admin.scalar(query, {"key": key, "seen": list(seen)})
        if pid is not None:
            return int(pid)
    return None


@postgres_only
async def test_a_read_closed_while_it_runs_is_not_run_again(
    tables: None, database: str
) -> None:
    """It may have done something outside its transaction (``dblink``): it fails.

    The read waits for a lock another session holds, and its connection is
    closed meanwhile. Run again, a second backend would wait for the lock.
    """
    key = 4242
    admin = create_async_engine(database, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as holder, admin.connect() as watcher:
            await holder.execute(text("SELECT pg_advisory_lock(:key)"), {"key": key})

            async def read() -> None:
                async with GadgetsUnitOfWork(reads=Reads.STATEMENTS) as uow:
                    await uow.session.execute(
                        text("SELECT pg_advisory_xact_lock_shared(:key)"),
                        {"key": key},
                    )

            reading = asyncio.create_task(read())
            first = await wait_for_lock_waiter(watcher, key)
            await watcher.execute(
                text("SELECT pg_terminate_backend(:pid, 5000)"), {"pid": first}
            )
            stop = asyncio.Event()
            again = asyncio.create_task(
                wait_for_lock_waiter(watcher, key, first or 0, until=stop)
            )
            await asyncio.wait({reading, again}, return_when=asyncio.FIRST_COMPLETED)
            stop.set()
            second = await again
            await holder.execute(text("SELECT pg_advisory_unlock_all()"))
            outcome = await asyncio.gather(reading, return_exceptions=True)
    finally:
        await admin.dispose()

    assert second is None, "the read ran a second time"
    assert isinstance(outcome[0], DBAPIError)


@asyncpg_only
async def test_a_connection_closed_between_reads_is_replaced_unless_it_set_something(
    tables: None,
) -> None:
    """Closed before a statement is sent, it is replaced: the statement runs once.

    Not after SQL text, which may have set something in the session that a
    fresh connection would not have.
    """

    async def close_it(uow: GadgetsUnitOfWork) -> None:
        """Close the unit of work's connection from the server, and wait till seen."""
        raw = await (await uow.session.connection()).get_raw_connection()
        driver: Any = raw.driver_connection
        closed = asyncio.Event()
        driver.add_termination_listener(lambda _: closed.set())
        async with connections.engine().connect() as admin:
            await admin.execute(
                text("SELECT pg_terminate_backend(:pid, 5000)"),
                {"pid": driver.get_server_pid()},
            )
        await closed.wait()

    async with GadgetsUnitOfWork(reads=Reads.STATEMENTS) as uow:
        await uow.gadgets.filter()  # The read pool's connection, made beforehand.
    with Connects() as connects:
        async with GadgetsUnitOfWork(reads=Reads.STATEMENTS) as uow:
            await uow.gadgets.filter()
            await close_it(uow)
            assert [gadget.name for gadget in await uow.gadgets.filter()] == ["first"]
    assert connects.count == 1

    async with GadgetsUnitOfWork(reads=Reads.STATEMENTS) as uow:
        await uow.session.execute(text("SET search_path TO pg_catalog"))
        await close_it(uow)
        with pytest.raises(DBAPIError, match="connection is closed"):
            await uow.gadgets.filter()


# What one read leaves for the next --------------------------------------------------

SESSION_STATE = text(
    "SELECT current_setting('search_path'), "
    "nullif(current_setting('domainkit.tenant', true), ''), "
    "current_setting('statement_timeout'), current_user = session_user, "
    "current_setting('default_transaction_read_only'), "
    "(SELECT count(*) FROM pg_listening_channels()), "
    "(SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
    "AND pid = pg_backend_pid())"
)
"""What a session may carry from one caller to the next."""


@pytest.fixture
async def one_connection(database: str) -> AsyncIterator[None]:
    """The gadget tables, read through a pool of one connection."""
    one = {"URL": database, "POOL_SIZE": 1, "MAX_OVERFLOW": 0}
    with override_settings(DATABASES={"default": one}):
        async with connections.engine().begin() as connection:
            await connection.run_sync(metadata.create_all)
        await store("first")
        yield
        await connections.close_all()


@postgres_only
@pytest.mark.usefixtures("one_connection")
async def test_what_a_read_sets_in_its_session_never_reaches_the_next() -> None:
    """Settings, role, LISTEN, advisory locks, temporary tables: all reset.

    The pool has one connection, so the second unit of work gets the
    first's. The first even turns read-only off; the second still cannot
    write, and reads the table, not a temporary one named like it.
    """
    async with GadgetsUnitOfWork(reads=Reads.STATEMENTS) as uow:
        backend = await uow.session.scalar(text("SELECT pg_backend_pid()"))
        before = (await uow.session.execute(SESSION_STATE)).one()
        for statement in (
            "SET search_path TO pg_catalog",
            "SELECT set_config('domainkit.tenant', 'tenant-a', false)",
            "SET statement_timeout = 0",
            "LISTEN tenant_a",
            "SELECT pg_advisory_lock(42)",
            "SET default_transaction_read_only = off",
            "CREATE TEMP TABLE framework_test_gadgets (name text)",
            "SET ROLE pg_read_all_data",
        ):
            await uow.session.execute(text(statement))
        assert (await uow.session.execute(SESSION_STATE)).one() != before

    async with GadgetsUnitOfWork(reads=Reads.STATEMENTS) as uow:
        assert await uow.session.scalar(text("SELECT pg_backend_pid()")) == backend
        assert (await uow.session.execute(SESSION_STATE)).one() == before
        assert [gadget.name for gadget in await uow.gadgets.filter()] == ["first"]
        with pytest.raises(DBAPIError) as refused:
            await uow.session.execute(
                text("UPDATE framework_test_gadgets SET name = 'x'")
            )
    assert getattr(refused.value.orig, "sqlstate", None) == WRITE_REFUSED


@asyncpg_only
@pytest.mark.usefixtures("one_connection")
async def test_what_the_driver_sends_unseen_is_reset_too() -> None:
    """Statements sent on the driver's own connection count: the session is reset.

    Between two plain reads, or after the last one.
    """
    tenant = "SELECT set_config('domainkit.tenant', 'tenant-a', false)"
    for between in (True, False):
        async with GadgetsUnitOfWork(reads=Reads.STATEMENTS) as uow:
            await uow.gadgets.filter()
            raw = await (await uow.session.connection()).get_raw_connection()
            await raw.driver_connection.execute(tenant)  # type: ignore[union-attr]
            if between:
                await uow.gadgets.filter()
        async with GadgetsUnitOfWork(reads=Reads.STATEMENTS) as uow:
            assert (await uow.session.execute(SESSION_STATE)).one()[1] is None


@asyncpg_only
@pytest.mark.usefixtures("one_connection")
async def test_plain_selects_need_no_reset_and_anything_else_one_round_trip() -> None:
    """A read of columns runs its own statements only.

    SQL text, a function call, or statements sent many at a time add the
    reset: one more query, by the simple query protocol. asyncpg counts
    the queries it runs; its loggers see those not prepared.
    """
    sent = Sent()
    async with GadgetsUnitOfWork(reads=Reads.STATEMENTS) as uow:
        raw = await (await uow.session.connection()).get_raw_connection()
        driver: Any = raw.driver_connection
        driver.add_query_logger(sent)

    async def read(
        statement: Executable | None = None, *, many: bool = False
    ) -> tuple[int, list[str]]:
        """Queries one read runs, and those logged: a gadget, its parts and makers."""
        ran, logged = _sent(driver) or 0, len(sent.queries)
        async with GadgetsUnitOfWork(reads=Reads.STATEMENTS) as uow:
            await uow.gadgets.filter()
            if statement is not None:
                await uow.session.execute(statement, [{}, {}] if many else None)
        async with asyncio.timeout(30):  # Fails, rather than hangs, without a reset.
            await sent.reach(logged + (statement is not None) + many)
        return (_sent(driver) or 0) - ran, sent.queries[logged:]

    assert await read() == (3, [])
    assert await read(select(func.count()).select_from(gadgets)) == (
        5,
        [RESET_SESSION],
    )
    assert await read(text("SELECT 1")) == (5, [RESET_SESSION])
    assert await read(text("SELECT 1"), many=True) == (5, ["SELECT 1", RESET_SESSION])
    assert await read() == (3, [])  # Nothing more arrived meanwhile.


@postgres_only
async def test_a_read_connection_that_is_not_read_only_is_refused(
    database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pooler that drops the startup settings would let reads write: no read runs.

    Readiness then reports the alias's read pool.
    """
    module = sys.modules[create_read_engine.__module__]
    monkeypatch.setattr(module, "read_only_args", connect_args)  # As a pooler would.
    with override_settings(DATABASES={"default": {"URL": database}}):
        assert await connections.check_all(10.0) == []
        with pytest.raises(ImproperlyConfigured, match="is not read-only"):
            async with GadgetsUnitOfWork(reads=Reads.STATEMENTS) as uow:
                await uow.session.execute(text("SELECT 1"))
        assert await connections.check_all(10.0) == [f"database:default{READS_SUFFIX}"]
        monkeypatch.undo()
        await connections.close_all()
        async with GadgetsUnitOfWork(reads=Reads.STATEMENTS) as uow:
            await uow.session.execute(text("SELECT 1"))
        assert await connections.check_all(10.0) == []
        await connections.close_all()


async def test_a_database_check_that_hangs_fails_readiness(
    database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each check has the readiness seconds; one that never answers fails."""

    async def never(alias: str = "default") -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(connections, "check", never)
    async with asyncio.timeout(30):  # Fails, rather than hangs, without a bound.
        assert await connections.check_all(0.01) == ["database:default"]


@postgres_only
@pytest.mark.usefixtures("one_connection")
async def test_a_connection_whose_reset_fails_is_closed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The pool replaces it: the next caller gets a fresh session."""
    async with GadgetsUnitOfWork(reads=Reads.STATEMENTS) as uow:
        first = await uow.session.scalar(text("SELECT pg_backend_pid()"))
        await uow.session.execute(text("SET search_path TO pg_catalog"))
        monkeypatch.setattr(
            sys.modules[create_read_engine.__module__], "RESET_SESSION", "RESET nothing"
        )
    monkeypatch.undo()
    async with GadgetsUnitOfWork(reads=Reads.STATEMENTS) as uow:
        assert await uow.session.scalar(text("SELECT pg_backend_pid()")) != first
        assert await uow.session.scalar(text("SHOW search_path")) == '"$user", public'
    assert "Exception during reset" in caplog.text


async def test_drivers_that_cannot_count_queries_are_always_reset() -> None:
    """psycopg: the reset is never prepared, so it is one simple query.

    A connection on its way out is left alone.
    """

    class Psycopg:
        closed = False

        def __init__(self) -> None:
            self.sent: list[tuple[str, object]] = []

        async def execute(self, query: str, *, prepare: bool | None = None) -> None:
            self.sent.append((query, prepare))

    driver = Psycopg()
    dbapi = SimpleNamespace(driver_connection=driver)
    record = SimpleNamespace(info={})
    leaving = PoolResetState(
        transaction_was_reset=False, terminate_only=True, asyncio_safe=True
    )
    unsafe = PoolResetState(
        transaction_was_reset=False, terminate_only=False, asyncio_safe=False
    )
    staying = PoolResetState(
        transaction_was_reset=False, terminate_only=False, asyncio_safe=True
    )
    for state in (leaving, unsafe, staying):
        await greenlet_spawn(_reset_session, dbapi, record, state)

    assert driver.sent == [(RESET_SESSION, False)]
    assert record.info == {"domainkit_reads_dirty": False}
    assert (_sent(driver), _closed(driver)) == (None, False)
    driver.closed = True
    assert _closed(driver)


@pytest.mark.parametrize(
    ("statement", "plain"),
    [
        ("SELECT t.a, t.b FROM t AS t WHERE t.id IN ($1::UUID, $2::UUID)", True),
        ("SELECT $1::INTEGER AS anon_1", True),
        ("select t.a from t where t.b = %(b_1)s::NUMERIC(18, 2)", True),
        ("SELECT t.a FROM t WHERE t.b > $1::TIMESTAMP(6) WITH TIME ZONE", True),
        ("SELECT t.a FROM t WHERE t.b = ANY ($1::VARCHAR[]) AND NOT (t.c = $2)", True),
        ("SELECT t.a FROM t WHERE EXISTS (SELECT u.a FROM u WHERE u.a = t.a)", True),
        ("SELECT t.a FROM t WHERE t.a = (SELECT u.a FROM u)", True),
        ("SELECT t.a FROM t WHERE (t.a, t.b) IN (($1, $2))", True),
        ("SELECT $1::pg_catalog.int4", True),
        ("SELECT $1::pg_catalog.numeric(18, 2)", True),
        ("SELECT $1::CHARACTER VARYING(255)", True),
        ('SELECT $1::"varchar"(10)', True),
        ('SELECT "user".id FROM "user"', True),
        ("SELECT 1;", False),
        ("SELECT 1 INTO copied", False),
        ("SELECT count(*) AS count_1 FROM t", False),
        ("SELECT $1::INTEGER OR pg_advisory_lock (1)", False),
        ("SELECT $1::INTEGER AS a, set_config($2, $3, false)", False),
        ("SELECT $1::NUMERIC(f(1))", False),
        ("SELECT $1::int4(1)(2)", False),
        ('SELECT "f"(1)', False),
        ("SELECT f/**/(1)", False),
        ("SELECT t.a -- AND\n(1) FROM t", False),
        ("SELECT t.a FROM t; SET search_path TO a", False),
        ("SELECT t.a FROM t WHERE t.a IN\u00a0($1)", False),  # A name to PostgreSQL.
        ("SELECT 'a'", False),
        ("SELECT t.a FROM t WHERE t.a ~ $1", False),
        ("SELECT (SELECT 1)(2)", False),
        ("SELECT t.a[1](2) FROM t", False),
        ("SELECT t.(a) FROM t", False),
        ("SELECT 1 INTO TEMP copied", False),
        ("WITH x AS (SELECT 1) SELECT 1", False),
        ("SET search_path TO a", False),
        ("", False),
    ],
)  # fmt: skip
def test_what_counts_as_a_plain_select(statement: str, plain: bool) -> None:
    """Names, parameters, casts and operators; a ``(`` never after a name."""
    assert _plain_select(statement) is plain


def test_a_ping_leaves_the_connection_as_it_found_it() -> None:
    """It pings outside a transaction, then puts autocommit back, even on failure."""

    class Driver:
        def __init__(self, autocommit: bool, *, fails: bool = False) -> None:
            self.autocommit = autocommit
            self.fails = fails
            self.pinged_with: bool | None = None

        def ping(self) -> None:
            self.pinged_with = self.autocommit
            if self.fails:
                raise ConnectionResetError

    for autocommit in (False, True):
        driver = Driver(autocommit)
        assert ping(driver) is True
        assert (driver.pinged_with, driver.autocommit) == (True, autocommit)
        failing = Driver(autocommit, fails=True)
        with pytest.raises(ConnectionResetError):
            ping(failing)
        assert (failing.pinged_with, failing.autocommit) == (True, autocommit)


def test_asyncpg_engines_ping_outside_a_transaction(tmp_path: Path) -> None:
    """Outside PgBouncer, which needs SQLAlchemy's transaction around the ping.

    The engine of reads on their own does not ping: it runs a read once
    more instead.
    """
    postgres = DatabaseConfig.model_validate({"URL": "postgresql+asyncpg://a@b/c"})
    local = DatabaseConfig.model_validate(
        {"URL": f"sqlite+aiosqlite:///{tmp_path / 'ping.sqlite3'}"}
    )
    bounced = postgres.model_copy(update={"PGBOUNCER": True})
    unpinged = postgres.model_copy(update={"POOL_PRE_PING": False})

    assert create_engine(postgres).sync_engine.dialect.do_ping == ping
    for other in (local, bounced):
        assert create_engine(other).sync_engine.dialect.do_ping != ping
    assert pre_pings(create_engine(postgres))
    assert not pre_pings(create_engine(unpinged))
    assert not pre_pings(create_read_engine(postgres))


def pre_pings(engine: AsyncEngine) -> bool:
    """Whether ``engine``'s pool tests a connection before handing it out."""
    return bool(engine.sync_engine.pool._pre_ping)


class Sent:
    """The statements a connection sends, from asyncpg's query loggers.

    asyncpg calls them soon after each statement: ``reach`` waits for them.
    """

    def __init__(self) -> None:
        """Start with none."""
        self.queries: list[str] = []
        self.changed = asyncio.Event()

    def __call__(self, record: Any) -> None:  # noqa: ANN401 - asyncpg's LoggedQuery.
        """Keep the statement's text."""
        self.queries.append(record.query)
        self.changed.set()

    async def reach(self, count: int) -> None:
        """Wait until ``count`` statements were logged."""
        while len(self.queries) < count:
            self.changed.clear()
            await self.changed.wait()


@asyncpg_only
async def test_a_ping_is_one_statement_outside_a_transaction(tables: None) -> None:
    """SQLAlchemy's own ping wraps it in BEGIN and ROLLBACK: three round trips."""
    sent = Sent()
    async with connections.engine().connect() as connection:
        raw = await connection.get_raw_connection()
        dbapi: Any = raw.dbapi_connection
        dbapi.driver_connection.add_query_logger(sent)

        assert await connection.run_sync(lambda _: ping(dbapi)) is True
        await sent.reach(1)
        assert dbapi.autocommit is False  # As it was.
        await connection.run_sync(lambda _: dbapi.ping())
        await sent.reach(4)

    assert sent.queries == [";", "BEGIN;", ";", "ROLLBACK;"]


@postgres_only
async def test_a_connection_that_failed_its_ping_is_replaced(tables: None) -> None:
    """Closed under the pool, it is replaced as it is taken; the work runs."""
    async with connections.engine().connect() as connection:
        raw = await connection.get_raw_connection()
        await raw.dbapi_connection.driver_connection.close()  # type: ignore[union-attr]

    with Connects() as connects:
        async with GadgetsUnitOfWork() as uow:
            assert len(await uow.gadgets.filter()) == 1
    assert connects.count == 1


# The pool --------------------------------------------------------------------------


@postgres_only
async def test_the_read_engine_takes_its_alias_settings(database: str) -> None:
    """Its pool's size, statement logging and hidden parameters, as configured."""
    messages = {}
    for logged in (False, True):
        config = DatabaseConfig.model_validate(
            {"URL": database, "POOL_SIZE": 2, "ECHO": logged, "LOG_PARAMETERS": logged}
        )
        engine = create_read_engine(config)
        assert (engine.pool.size(), engine.echo) == (2, logged)  # type: ignore[attr-defined]
        async with engine.connect() as connection:
            with pytest.raises(DBAPIError) as failed:
                await connection.execute(
                    text("SELECT * FROM missing WHERE name = :name"),
                    {"name": "s3cret-token"},
                )
        messages[logged] = str(failed.value)
        await engine.dispose()

    assert "s3cret-token" not in messages[False]
    assert "s3cret-token" in messages[True]


@postgres_only
async def test_the_read_pool_is_reported_and_closed(
    tables: None, database: str
) -> None:
    """``pool_stats`` names it ``<alias>:reads``; ``close_all`` disposes it.

    It is sized like the alias's own pool, and reports its overflow.
    """
    await connections.close_all()
    pool = {"URL": database, "POOL_SIZE": 1, "MAX_OVERFLOW": 1}
    with override_settings(DATABASES={"default": pool}):
        async with (
            GadgetsUnitOfWork(reads=Reads.STATEMENTS) as uow,
            GadgetsUnitOfWork(reads=Reads.STATEMENTS) as other,
        ):
            await uow.gadgets.filter()
            assert connections.pool_stats()[f"default{READS_SUFFIX}"] == {
                "size": 1,
                "checked_out": 1,
                "overflow": 0,
            }
            await other.gadgets.filter()
            assert connections.pool_stats()[f"default{READS_SUFFIX}"] == {
                "size": 1,
                "checked_out": 2,
                "overflow": 1,
            }

        await connections.close_all()

        assert connections.pool_stats() == {}
        async with GadgetsUnitOfWork(reads=Reads.STATEMENTS) as uow:  # A new one.
            assert len(await uow.gadgets.filter()) == 1


# Statements that commit as they run ------------------------------------------------


async def test_autocommit_commits_each_statement_as_it_runs(tables: None) -> None:
    """Another connection sees the first statement before the block ends."""
    async with connections.autocommit() as connection:
        await connection.execute(delete(gadgets))
        assert await names() == []
        if ON_POSTGRES:
            assert not await in_transaction(connection)


@postgres_only
async def test_autocommit_through_pgbouncer_is_one_transaction(database: str) -> None:
    """The block's statements commit together, with the timeouts set for them."""
    with override_settings(DATABASES={"default": {"URL": database, "PGBOUNCER": True}}):
        async with connections.engine().begin() as connection:
            await connection.run_sync(metadata.create_all)
        await store("first")
        with freeze_time("2026-01-01"):
            async with deadlines.timeout(5.0), connections.autocommit() as connection:
                await connection.execute(delete(gadgets))
                shown = await connection.scalar(text("SHOW statement_timeout"))
                assert await names() == ["first"]  # Not committed yet.
        assert await names() == []
        await connections.close_all()
    assert shown == "5s"


@postgres_only
async def test_statements_on_their_own_set_no_local_timeouts(tables: None) -> None:
    """``SET LOCAL`` would end with the statement: none is sent; the session's holds."""
    sent: list[str] = []

    def record(*args: Any) -> None:  # noqa: ANN401 - before_cursor_execute's.
        sent.append(args[2])

    event.listen(Engine, "before_cursor_execute", record)
    try:
        with freeze_time("2026-01-01"):
            async with deadlines.timeout(5.0):
                async with connections.autocommit() as connection:
                    shown = await connection.scalar(text("SHOW statement_timeout"))
                alone = list(sent)
                async with connections.engine().begin() as connection:
                    lowered = await connection.scalar(text("SHOW statement_timeout"))
    finally:
        event.remove(Engine, "before_cursor_execute", record)
    assert (shown, lowered) == ("30s", "5s")
    assert alone == ["SHOW statement_timeout"]
    assert "set_config('statement_timeout'" in sent[1]  # The transaction's own.
