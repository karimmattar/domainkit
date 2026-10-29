"""A statement a cancellation cuts off keeps its connection, once settled (CONC-08).

SQLAlchemy closes a connection whose statement was cancelled, so each
request cut off at its deadline cost one. With asyncpg, the connection is
now kept once it has settled: asyncpg has finished the cancelled exchange,
the server's transaction is rolled back, and asyncpg's and SQLAlchemy's
view of it is cleared to match. SEC-10b-1 of the 0.10 review is why each
step is there: a connection kept as it was, after a deadline cut ``BEGIN``
off, stayed inside a server transaction nobody tracked, and the next unit of
work's acknowledged write was lost, its locks held. Under an anyio cancel
scope, which cancels every wait until it exits, the connection cannot
settle and is closed, as before.
"""

import asyncio
import importlib
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from importlib.metadata import version
from types import SimpleNamespace
from typing import Any

import anyio
import asyncpg  # type: ignore[import-untyped]
import pytest
from sqlalchemy import make_url, select, text
from sqlalchemy.dialects.postgresql.asyncpg import AsyncAdapt_asyncpg_connection
from sqlalchemy.engine import RootTransaction
from sqlalchemy.exc import PendingRollbackError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import Pool

from domainkit import deadlines
from domainkit.conf import DatabaseConfig, override_settings
from domainkit.db import Reads, connections
from domainkit.db.connections import create_engine, create_read_engine
from domainkit_tests.conftest import POSTGRES_VARIABLE
from domainkit_tests.engine_events import watching
from domainkit_tests.gadgets import GadgetsUnitOfWork, gadgets, make_gadget, metadata
from domainkit_tests.test_db_reads import (
    idle_in_transaction,
    reading_app,
    slow_proxy,
    store,
)

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("settling")]
connections_module = importlib.import_module("domainkit.db.connections")
"""The module, which ``domainkit.db.connections`` (the handler) hides."""
proven_check = connections_module._proven
"""The module's own check of the installed releases, which ``settling`` may
stand in for while a test runs."""
asyncpg_only = pytest.mark.skipif(
    "+asyncpg" not in os.environ.get(POSTGRES_VARIABLE, ""),
    reason=f"set {POSTGRES_VARIABLE} to an asyncpg URL",
)
SLEEP = text("SELECT pg_sleep(5)")
"""A statement only a cancellation ends in time."""


@pytest.fixture
def settling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settle cut-off connections, whatever releases are installed.

    On releases of asyncpg or SQLAlchemy ``_PROVEN`` does not name yet, the
    cases run with settling on: they prove it on the releases installed, or
    fail on them. The ``newest`` job installs each release as it ships and
    runs them against PostgreSQL (ADR 0048's 0.12 amendment).
    """
    if not connections_module._proven():
        monkeypatch.setattr(connections_module, "_proven", lambda: True)


@pytest.fixture
def database(database_url: str) -> Iterator[str]:
    """An empty database, selected in the settings."""
    with override_settings(DATABASES={"default": {"URL": database_url}}):
        yield database_url


@pytest.fixture
async def tables(database: str) -> AsyncIterator[None]:
    """The gadget tables, with one gadget ("first"), in the running loop's engine."""
    async with connections.engine().begin() as connection:
        await connection.run_sync(metadata.create_all)
    await store("first")
    yield
    await connections.close_all()


class PoolEvents:
    """Count the connections every pool opens, and those it discards, while entered.

    A connection is discarded once, however many times SQLAlchemy reports
    it: a close that a cancellation interrupted is reported again when the
    pool finds the connection closed.
    """

    def __init__(self) -> None:
        """Start at none."""
        self.connects = 0
        self._discarded: set[int] = set()

    @property
    def counts(self) -> tuple[int, int]:
        """Connections opened, then discarded."""
        return self.connects, len(self._discarded)

    def _connected(self, *args: object) -> None:
        self.connects += 1

    def _discard(self, dbapi_connection: object, *args: object) -> None:
        self._discarded.add(id(dbapi_connection))

    def __enter__(self) -> "PoolEvents":
        """Start counting."""
        self._watches = [
            watching(Pool, "connect", self._connected),
            watching(Pool, "invalidate", self._discard),
        ]
        for watch in self._watches:
            watch.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        """Stop counting."""
        for watch in reversed(self._watches):
            watch.__exit__(None, None, None)


async def committed(database_url: str) -> list[str]:
    """The gadgets' names another connection sees: what was committed."""
    other = create_async_engine(database_url)
    try:
        async with other.connect() as connection:
            return sorted((await connection.scalars(select(gadgets.c.name))).all())
    finally:
        await other.dispose()


async def locks_held(database_url: str) -> int:
    """Locks other connections hold on the gadgets' table."""
    other = create_async_engine(database_url)
    try:
        async with other.connect() as connection:
            return int(
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_locks WHERE relation = "
                        "'framework_test_gadgets'::regclass "
                        "AND pid <> pg_backend_pid()"
                    )
                )
                or 0
            )
    finally:
        await other.dispose()


async def assert_clean(database_url: str) -> None:
    """No other connection holds a lock on the gadgets, or a transaction open."""
    assert await locks_held(database_url) == 0
    assert await idle_in_transaction(database_url) == 0


async def eventually_clean(database_url: str) -> None:
    """``assert_clean``, once the server has ended what a closed connection left.

    A connection closed without its transaction rolled back keeps its locks
    until the server notices it went away.
    """
    for _ in range(50):
        if not await locks_held(database_url):
            break
        await asyncio.sleep(0.1)
    await assert_clean(database_url)


@asynccontextmanager
async def through_proxy(
    database_url: str, delay: dict[str, float]
) -> AsyncIterator[None]:
    """``DATABASES`` through a proxy that holds the server's answers ``delay["s"]``.

    One connection, and no ping: a deadline lands on the statement. The
    pool's connection is made, writing the gadget "made", before the block.
    """
    url = make_url(database_url)
    proxy = await slow_proxy(url.port or 5432, delay)
    port = proxy.sockets[0].getsockname()[1]
    proxied = url.set(host="127.0.0.1", port=port).render_as_string(hide_password=False)
    await connections.close_all()
    try:
        with override_settings(
            DATABASES={
                "default": {
                    "URL": proxied,
                    "POOL_SIZE": 1,
                    "MAX_OVERFLOW": 0,
                    "POOL_PRE_PING": False,
                }
            }
        ):
            await store("made")
            yield
    finally:
        await connections.close_all()
        proxy.close()


async def write_then_sleep(
    name: str, *, deadline: float | None = None, reads: Reads | None = None
) -> None:
    """Write gadget ``name`` (unless ``reads``), then ``SLEEP`` under ``deadline``.

    The deadline starts after the write, so the transaction's
    ``statement_timeout`` is the server's own, not the deadline's: the
    deadline, not the server, ends ``SLEEP``.
    """
    async with GadgetsUnitOfWork(reads=reads) as uow:
        if reads is None:
            await uow.gadgets.add(make_gadget(name))
            await uow.session.flush()  # Written, and locked.
        else:  # Changes the session, which the next caller must not see.
            await uow.session.execute(
                text("SELECT set_config('domainkit.probe', 'set', false)")
            )
        async with deadlines.timeout(deadline):
            await uow.session.execute(SLEEP)


async def cut_off(seconds: float | None, work: Any) -> None:  # noqa: ANN401 - a coroutine.
    """Run ``work`` under ``seconds`` (None: its own deadline); it must be cut off."""
    with pytest.raises(TimeoutError):
        async with deadlines.timeout(seconds):
            await work


@pytest.fixture
def patient(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let a connection take 10 seconds to settle, however loaded the machine is.

    For the tests that a connection is kept; how long it may take is
    another test's.
    """
    monkeypatch.setattr(connections_module, "SETTLE_TIMEOUT", 10.0)


# What a cut-off request costs ------------------------------------------------------


@asyncpg_only
@pytest.mark.parametrize("reads", [None, "statements"])
@pytest.mark.usefixtures("patient")
async def test_requests_cut_off_mid_statement_keep_their_connection(
    database_url: str, reads: str | None
) -> None:
    """Three requests cut off at their deadline: no connection opened or discarded.

    CONC-08: each used to discard its connection, and the next opened one,
    in a transaction and reading statements on their own alike.
    """
    params = {} if reads is None else {"reads": reads}
    async with reading_app(database_url, REQUEST_TIMEOUT=1.0) as client:
        assert (await client.get("/gadgets", params=params)).json() == ["first"]
        with PoolEvents() as events:
            for _ in range(3):
                response = await client.get("/slow-second", params=params)
                assert response.status_code == 503
                assert response.json()["code"] == "deadline_exceeded"
            assert (await client.get("/gadgets", params=params)).json() == ["first"]
        await assert_clean(database_url)
        assert events.counts == (0, 0)


# SEC-10b-1: a deadline during BEGIN ----------------------------------------------


@asyncpg_only
@pytest.mark.usefixtures("patient")
async def test_a_write_after_a_deadline_cut_begin_off_is_committed(
    tables: None, database: str
) -> None:
    """The deadline fires while BEGIN's answer travels; the next write still commits.

    The kept connection was inside a server transaction asyncpg still
    tracked and SQLAlchemy did not, so the next unit of work's COMMIT only
    released a savepoint in it: its write was lost and its locks held.
    Settled, the transaction is rolled back and both trackers are cleared.
    """
    delay = {"s": 0.0}
    async with through_proxy(database, delay):
        with PoolEvents() as events:
            delay["s"] = 0.5
            await cut_off(0.2, store("cut"))  # BEGIN answers after the deadline.
            delay["s"] = 0.0
            await store("kept")  # Acknowledged: it must be committed.
        assert await committed(database) == ["first", "kept", "made"]
        await assert_clean(database)
        assert events.counts == (0, 0)  # Kept, once settled.


@asyncpg_only
@pytest.mark.usefixtures("patient")
async def test_a_unit_of_work_going_on_after_begin_was_cut_off_commits(
    tables: None, database: str
) -> None:
    """Code that goes on after the cut, in the same unit of work, commits its write.

    Nothing ran in the transaction ``BEGIN`` started, and it was rolled
    back: the write runs in a transaction of its own.
    """
    delay = {"s": 0.0}
    async with through_proxy(database, delay), GadgetsUnitOfWork() as uow:
        delay["s"] = 0.5
        await cut_off(0.2, uow.gadgets.filter())
        delay["s"] = 0.0
        await uow.gadgets.add(make_gadget("after"))
        await uow.commit()
    assert await committed(database) == ["after", "first", "made"]
    await assert_clean(database)


@asyncpg_only
async def test_a_write_after_an_anyio_scope_cut_begin_off_is_committed(
    tables: None, database: str
) -> None:
    """As above, under an anyio cancel scope: the connection is closed, as before.

    The pool does not ping, and SQLAlchemy may hand the closed connection
    out again (its close was cancelled too): the pool replaces it.
    """
    delay = {"s": 0.0}
    async with through_proxy(database, delay):
        with PoolEvents() as events:
            delay["s"] = 0.5
            with anyio.move_on_after(0.2):
                await store("cut")
            delay["s"] = 0.0
            await store("kept")
        assert await committed(database) == ["first", "kept", "made"]
        await eventually_clean(database)
        assert events.counts == (1, 1)  # Cancelled again as it settled: closed.


# Cut off mid-statement --------------------------------------------------------------


@asyncpg_only
@pytest.mark.usefixtures("patient")
async def test_a_statement_cut_off_in_a_transaction_rolls_it_back_at_once(
    tables: None, database: str
) -> None:
    """The transaction's write is undone and its locks released, before the next use.

    The connection is kept; the next unit of work's write commits.
    """
    await store("made")  # The pool's connection.
    with PoolEvents() as events:
        await cut_off(None, write_then_sleep("cut", deadline=0.3))
    await assert_clean(database)  # Rolled back already: the pool holds it idle.
    with events:
        await store("kept")
    assert await committed(database) == ["first", "kept", "made"]
    assert events.counts == (0, 0)


@asyncpg_only
@pytest.mark.usefixtures("patient")
async def test_a_transaction_cut_off_refuses_to_go_on(
    tables: None, database: str
) -> None:
    """Code that goes on after the cut cannot commit what the rollback undid.

    As when the connection was discarded: the transaction must be rolled
    back first (``PendingRollbackError``), and nothing it wrote is saved.
    """
    async with GadgetsUnitOfWork() as uow:
        await uow.gadgets.add(make_gadget("cut"))
        await uow.session.flush()
        await cut_off(0.3, uow.session.execute(SLEEP))
        with pytest.raises(PendingRollbackError):
            await uow.session.execute(text("SELECT 1"))
        with pytest.raises(PendingRollbackError):
            await uow.commit()
        await uow.rollback()
        await uow.gadgets.add(make_gadget("after"))
        await uow.commit()
    assert await committed(database) == ["after", "first"]
    await assert_clean(database)


@asyncpg_only
async def test_a_write_after_an_anyio_scope_cut_a_statement_off_is_committed(
    tables: None, database: str
) -> None:
    """Under an anyio cancel scope, mid-statement: closed, nothing left behind."""
    await store("made")
    with PoolEvents() as events:
        with anyio.move_on_after(0.3):
            await write_then_sleep("cut")
        for name in ("kept", "again"):
            await store(name)
    assert await committed(database) == ["again", "first", "kept", "made"]
    await eventually_clean(database)
    assert events.counts == (1, 1)


@asyncpg_only
@pytest.mark.usefixtures("patient")
async def test_a_read_cut_off_on_its_own_keeps_its_connection_and_resets_it(
    tables: None, database: str
) -> None:
    """``Reads.STATEMENTS``: the next caller gets the connection, its session reset."""
    async with GadgetsUnitOfWork(reads=Reads.STATEMENTS) as uow:
        await uow.gadgets.filter()  # The read pool's connection.
    with PoolEvents() as events:
        await cut_off(None, write_then_sleep("", deadline=0.3, reads=Reads.STATEMENTS))
        async with GadgetsUnitOfWork(reads=Reads.STATEMENTS) as uow:
            probed = text("SELECT current_setting('domainkit.probe', true)")
            assert await uow.session.scalar(probed) in {"", None}
            assert len(await uow.gadgets.filter()) == 1
    await assert_clean(database)
    assert events.counts == (0, 0)


# When it cannot settle ---------------------------------------------------------------


@asyncpg_only
async def test_a_connection_that_does_not_settle_in_time_is_discarded(
    tables: None, database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The server's answers come later than ``SETTLE_TIMEOUT``: closed, not waited on.

    The cut-off work ends within that bound, the connection is discarded
    then (the pool hands it to no one), and the next caller gets a new one
    that works.
    """
    monkeypatch.setattr(connections_module, "SETTLE_TIMEOUT", 0.3)
    delay = {"s": 0.0}
    async with through_proxy(database, delay):
        with PoolEvents() as events:
            delay["s"] = 3.0
            started = asyncio.get_running_loop().time()
            await cut_off(0.2, store("cut"))
            assert asyncio.get_running_loop().time() - started < 2.5
            assert events.counts == (0, 1)
            delay["s"] = 0.0
            await store("kept")
        assert await committed(database) == ["first", "kept", "made"]
        await eventually_clean(database)
        assert events.counts == (1, 1)


@asyncpg_only
async def test_a_transaction_begun_in_sql_text_is_not_kept(
    tables: None, database: str
) -> None:
    """An autocommit connection the server holds in a transaction is discarded.

    Neither asyncpg nor SQLAlchemy began that transaction, so nothing says
    what it holds: the connection is closed, and the server ends it.
    """
    delay = {"s": 0.0}
    async with through_proxy(database, delay):
        with PoolEvents() as events:
            async with connections.autocommit() as connection:
                await connection.execute(text("BEGIN"))
                delay["s"] = 0.5  # SELECT 1 is done before the deadline is.
                await cut_off(0.2, connection.execute(text("SELECT 1")))
            assert events.counts == (0, 1)  # At once: the pool hands it to no one.
            delay["s"] = 0.0
            await store("kept")
        assert await committed(database) == ["first", "kept", "made"]
        await eventually_clean(database)
        assert events.counts == (1, 1)


@asyncpg_only
async def test_a_settling_cancelled_by_shutdown_discards_the_connection(
    tables: None, database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The settling task is cancelled (as a shutdown does): closed, as before.

    The task is named, for the shutdown's log of tasks that would not stop.
    """
    names: list[str] = []

    async def cancelled(adapter: object, driver: object) -> bool:
        names.append(asyncio.current_task().get_name())  # type: ignore[union-attr]
        raise asyncio.CancelledError

    monkeypatch.setattr(connections_module, "_settle", cancelled)
    await store("made")
    with PoolEvents() as events:
        await cut_off(None, write_then_sleep("cut", deadline=0.3))
        assert events.counts == (0, 1)
        await store("kept")
    assert names == ["domainkit.db.settle"]
    assert await committed(database) == ["first", "kept", "made"]
    await eventually_clean(database)
    assert events.counts == (1, 1)


@asyncpg_only
async def test_releases_not_proven_discard_a_cut_off_connection(
    tables: None, database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With releases of asyncpg or SQLAlchemy not proven, it is closed, as before."""
    monkeypatch.setattr(connections_module, "_proven", lambda: False)
    await connections.close_all()
    await store("made")
    with PoolEvents() as events:
        await cut_off(None, write_then_sleep("cut", deadline=0.3))
        await store("kept")
    assert await committed(database) == ["first", "kept", "made"]
    await eventually_clean(database)
    assert events.counts == (1, 1)


# What settling relies on ------------------------------------------------------------


def test_the_installed_asyncpg_and_sqlalchemy_are_releases_proven() -> None:
    """Settling reads and clears state asyncpg and SQLAlchemy keep privately.

    ``_settle`` clears asyncpg's ``Connection._top_xact`` (the transaction
    it began, kept even when ``BEGIN`` failed) and SQLAlchemy's adapter's
    ``_transaction``, and deactivates SQLAlchemy's transaction. It is used
    only on the releases the tests of this module proved it on
    (``_PROVEN``); a new release fails here, saying what to do, until they
    pass on it and ``_PROVEN`` admits it.
    """
    unproven = [
        f"{name} {version(name)}"
        for name, (low, high) in connections_module._PROVEN.items()
        if not low <= connections_module._release(name) < high
    ]
    assert not unproven, (
        f"Settling a cut-off connection is not proven on {', '.join(unproven)}. "
        "The cases of test_db_cut_off.py and test_sec11b_2_cut_off_session_lock.py "
        "ran on them with settling on, where PostgreSQL is set (the newest job's "
        "'Prove settling' step). If they all passed, widen _PROVEN to the release's "
        "minor and amend ADR 0048; if one failed, leave _PROVEN: such a connection "
        "is discarded there, as before."
    )
    assert "_top_xact" in asyncpg.Connection.__slots__
    assert "_transaction" in AsyncAdapt_asyncpg_connection.__slots__
    assert callable(RootTransaction._deactivate_from_connection)


@pytest.mark.parametrize(
    ("asyncpg_release", "sqlalchemy_release", "proven"),
    [
        ("0.32.0", "2.1.4", True),
        ("0.32.9", "2.1.17", True),
        ("0.31.0", "2.1.4", False),
        ("0.33.0", "2.1.4", False),
        ("1.32.0", "2.1.4", False),
        ("0.32.0", "2.0.44", False),
        ("0.32.0", "2.2.0b1", False),
        ("0.32.0", "3.1.0", False),
    ],
)
def test_releases_are_proven_by_major_and_minor(
    monkeypatch: pytest.MonkeyPatch,
    asyncpg_release: str,
    sqlalchemy_release: str,
    proven: bool,
) -> None:
    """``_PROVEN`` admits the minor releases it names, pre-releases past them not."""
    installed = {"asyncpg": asyncpg_release, "sqlalchemy": sqlalchemy_release}
    monkeypatch.setattr(connections_module, "version", installed.__getitem__)
    assert proven_check.__wrapped__() is proven


@pytest.mark.parametrize(
    "context",
    [
        SimpleNamespace(connection=None),
        SimpleNamespace(connection=SimpleNamespace(invalidated=True)),
        SimpleNamespace(
            connection=SimpleNamespace(invalidated=False),
            is_disconnect=False,
            original_exception=asyncio.CancelledError(),
        ),
        SimpleNamespace(
            connection=SimpleNamespace(invalidated=False),
            is_disconnect=True,
            original_exception=TimeoutError(),
        ),
    ],
    ids=["no-connection", "invalidated", "not-a-disconnect", "not-cancelled"],
)
def test_only_a_cancelled_statement_on_a_live_connection_is_settled(
    context: Any,  # noqa: ANN401 - a stand-in for SQLAlchemy's.
) -> None:
    """Every other error goes on as SQLAlchemy decided, the connection untouched."""
    is_disconnect = getattr(context, "is_disconnect", None)
    connections_module._keep_cut_off(context)
    assert getattr(context, "is_disconnect", None) is is_disconnect


def test_only_asyncpg_outside_pgbouncer_settles_a_cut_off_connection() -> None:
    """Other drivers, and connections through PgBouncer, are closed, as before.

    An asyncpg pool that does not ping replaces a connection asyncpg knows
    is closed as it hands it out; one that pings finds it so then.
    """

    def settles(engine: AsyncEngine) -> bool:
        listeners = engine.sync_engine.dialect.dispatch.handle_error
        return any(
            getattr(listener, "func", None) is connections_module._keep_cut_off
            for listener in listeners
        )

    def replaces_closed(engine: AsyncEngine) -> bool:
        return any(
            getattr(listener, "func", None) is connections_module._replace_closed
            for listener in engine.sync_engine.pool.dispatch.checkout
        )

    asyncpg_config = DatabaseConfig.model_validate(
        {"URL": "postgresql+asyncpg://app@db/app"}
    )
    bounced = asyncpg_config.model_copy(update={"PGBOUNCER": True})
    unpinged = asyncpg_config.model_copy(update={"POOL_PRE_PING": False})
    sqlite = DatabaseConfig.model_validate({"URL": "sqlite+aiosqlite:///:memory:"})

    assert settles(create_engine(asyncpg_config))
    assert settles(create_read_engine(asyncpg_config))
    assert not settles(create_engine(bounced))
    assert not settles(create_engine(sqlite))
    assert replaces_closed(create_engine(unpinged))
    assert not replaces_closed(create_engine(asyncpg_config))
    assert not replaces_closed(
        create_engine(bounced.model_copy(update={"POOL_PRE_PING": False}))
    )
