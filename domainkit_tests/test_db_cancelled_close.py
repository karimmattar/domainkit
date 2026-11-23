"""A unit of work gives its connection back however a cancellation reaches it.

Each case arms a hook on one database call; the hook cancels the work just
before the call's I/O, so the cancellation lands in that call. An anyio
cancel scope cancels again at every later wait until it exits; asyncio's
own cancellation comes once. Either way, the pool has every connection
back as the cancellation leaves the block, on SQLite and PostgreSQL.
"""

import asyncio
import gc
import threading
import time
import warnings
from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from typing import Any, Final

import anyio
import pytest
from sqlalchemy import insert, text

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.db import connections
from domainkit.db import uow as uow_module
from domainkit.db.connections import (
    _close_asyncpg,
    _discard_at_once,
    _terminate_asyncpg,
)
from domainkit_tests.sample_routes import PaymentsUnitOfWork, payments

UNCLOSED_SQLITE: Final = "unclosed database in <sqlite3.Connection"
"""On SQLite, a cut that lands in aiosqlite's connect, as a transaction
begins or as the pool replaces a connection, leaves aiosqlite to drop the
new sqlite3 handle unclosed: its own, not a pool connection, and not what
these tests are about. The garbage collector may find it in a later case,
so every case here allows it, and collects at its end (``collected``)."""

LATE_WORKER: Final = (
    r"Exception in thread [^\n]*\(_connection_worker_thread\)"
    r"[\s\S]*RuntimeError. Event loop is closed"
)
"""The same cut can leave aiosqlite's worker thread finishing that connect
after the case's event loop has closed: it then fails to hand the result to
the loop, in its own thread. Only that failure is allowed; any other error
in aiosqlite's thread still fails the case. (A filter splits at colons,
so the pattern reads the one after ``RuntimeError`` as any character.)"""

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.filterwarnings(f"ignore:{UNCLOSED_SQLITE}:ResourceWarning"),
    pytest.mark.filterwarnings(
        f"ignore:{LATE_WORKER}:pytest.PytestUnhandledThreadExceptionWarning"
    ),
]
WORKERS_END_WITHIN: Final = 2.0
"""Seconds a case waits, in all, for the aiosqlite workers it started to end:
one still serving an open connection never ends, so the wait is bounded."""
BOUNDED: Final = pytest.mark.timeout(30, method="signal")
"""Every case is bounded, so one that regresses fails rather than hangs."""


@pytest.fixture(autouse=True)
def collected() -> Iterator[None]:
    """Let each case's aiosqlite workers end, and collect their dropped handles.

    A worker that finishes a cut connect after the case's loop closed fails
    in its own thread; waited for here, briefly, its failure and its handle
    are reported within the case, under the module's filters, not in a case
    of another module.
    """
    before = set(threading.enumerate())
    yield
    deadline = time.monotonic() + WORKERS_END_WITHIN
    for thread in set(threading.enumerate()) - before:
        if thread.name.endswith("(_connection_worker_thread)"):
            thread.join(max(0.0, deadline - time.monotonic()))
    gc.collect()


def test_only_aiosqlite_handing_a_result_to_a_closed_loop_is_allowed() -> None:
    """A late worker's failure is allowed here; another of its errors is not."""
    head = "Exception in thread Thread-7 (_connection_worker_thread)\n\nTraceback:\n"
    warnings.warn(
        pytest.PytestUnhandledThreadExceptionWarning(
            head + "RuntimeError: Event loop is closed\n"
        ),
        stacklevel=1,
    )
    with pytest.raises(pytest.PytestUnhandledThreadExceptionWarning):
        warnings.warn(
            pytest.PytestUnhandledThreadExceptionWarning(
                head + "sqlite3.OperationalError: disk I/O error\n"
            ),
            stacklevel=1,
        )


@pytest.fixture
async def paying(database_url: str) -> AsyncIterator[None]:
    """Payments on a fresh database, closed afterwards within a bound."""
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=["domainkit_tests.sample_domains.alpha"],
        DATABASES={"default": {"URL": database_url}},
    ):
        setup()
        async with connections.engine().begin() as connection:
            await connection.run_sync(payments.create, checkfirst=True)
        try:
            yield
        finally:
            async with asyncio.timeout(15):
                await connections.close_all()
            registry.clear()


def checked_out() -> int:
    """The connections of the default pool not given back."""
    return connections.pool_stats()["default"]["checked_out"]


class Cut:
    """Cancel the work on the next call of a patched dialect method.

    ``scope`` is an anyio cancel scope, cancelled at every later wait;
    without one, the task is cancelled once, as asyncio does.
    """

    def __init__(self, repeating: bool) -> None:
        """Arm nothing yet."""
        self.repeating = repeating
        self.armed = False
        self.scope: anyio.CancelScope | None = None
        self.task: asyncio.Task[Any] | None = None
        self.fired = 0

    def arm(self) -> None:
        """Cancel on the next hooked call."""
        self.armed = True

    def fire(self) -> None:
        """Cancel the work, once armed."""
        if not self.armed:
            return
        self.armed = False
        self.fired += 1
        if self.scope is not None:
            self.scope.cancel()
        elif self.task is not None:
            self.task.cancel()


def hook(monkeypatch: pytest.MonkeyPatch, name: str, cut: Cut) -> None:
    """Fire ``cut`` just before the dialect's ``name`` does its I/O."""
    dialect = connections.engine().sync_engine.dialect
    real = getattr(dialect, name)

    def hooked(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 - the dialect's.
        cut.fire()
        return real(*args, **kwargs)

    monkeypatch.setattr(dialect, name, hooked)


async def cut_work(case: str, cut: Cut) -> None:
    """Run ``case`` in a unit of work, cut off where ``cut`` is armed."""
    readonly = case in {"normal-close", "first-statement"}
    async with PaymentsUnitOfWork(readonly=readonly) as uow:
        if case == "first-statement":  # Cut as its transaction begins.
            cut.arm()
            await uow.session.execute(text("SELECT 1"))
        elif case == "normal-close":  # Ends normally; cut in its close.
            await uow.session.execute(text("SELECT 1"))
            cut.arm()
        elif case == "close-after-error":  # Raises; cut in its close.
            await uow.session.execute(insert(payments))
            await uow.session.flush()
            cut.arm()
            raise ValueError("refused")
        elif case == "close-after-write":  # Flushed, not committed; cut in close.
            await uow.session.execute(insert(payments))
            await uow.session.flush()
            cut.arm()
        elif case == "statement":
            await uow.session.execute(insert(payments))
            cut.arm()
            await uow.session.execute(text("SELECT 1"))
        else:  # "savepoint": cut in its ROLLBACK TO SAVEPOINT.
            await uow.session.execute(insert(payments))
            cut.arm()
            async with uow.savepoint():
                await uow.session.execute(insert(payments))
                raise ValueError("in the savepoint")


HOOKED: Final = {
    "first-statement": "do_execute",
    "normal-close": "do_rollback",
    "close-after-error": "do_rollback",
    "close-after-write": "do_rollback",
    "statement": "do_execute",
    "savepoint": "do_rollback_to_savepoint",
}
"""The dialect call each case cuts."""


@BOUNDED
@pytest.mark.usefixtures("paying")
@pytest.mark.parametrize("case", list(HOOKED))
@pytest.mark.parametrize("repeating", [True, False], ids=["anyio-scope", "asyncio"])
async def test_a_cancelled_unit_of_work_gives_its_connection_back(
    case: str, repeating: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The connection is back in the pool as the cancellation leaves the block.

    So it is when the cancellation reaches the close of work that ended
    normally, or with an error, or after a flushed write, and when it
    reaches a statement or a savepoint's rollback, which leave through the
    close of work being cancelled. Nothing is committed.
    """
    cut = Cut(repeating)
    hook(monkeypatch, HOOKED[case], cut)

    async def run() -> None:
        if repeating:
            with anyio.CancelScope() as scope:
                cut.scope = scope
                await cut_work(case, cut)
            assert scope.cancelled_caught
        else:
            cut.task = asyncio.current_task()
            with pytest.raises(asyncio.CancelledError):
                await cut_work(case, cut)

    async with asyncio.timeout(10):
        await asyncio.create_task(run())
    assert cut.fired == 1
    assert checked_out() == 0
    monkeypatch.undo()
    async with connections.engine().connect() as connection:
        assert (await connection.execute(payments.select())).all() == []


@BOUNDED
@pytest.mark.usefixtures("paying")
async def test_closes_cut_one_after_another_each_give_their_connection_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Twenty closes cut in their ``ROLLBACK`` each give their connection back.

    After them, a read gets a connection at once.
    """
    for _ in range(20):
        cut = Cut(repeating=True)
        with monkeypatch.context() as patched:
            hook(patched, "do_rollback", cut)
            with anyio.CancelScope() as scope:
                cut.scope = scope
                await cut_work("normal-close", cut)
        assert checked_out() == 0
    async with asyncio.timeout(2), PaymentsUnitOfWork(readonly=True) as uow:
        assert await uow.session.scalar(text("SELECT 1")) == 1


@pytest.mark.usefixtures("paying")
async def test_the_connections_a_session_holds_are_read_from_its_transaction() -> None:
    """``_open_connections`` names the session's connection inside a transaction.

    The close reads them there before closing, to discard them if it is cut
    off; SQLAlchemy keeps them privately, so a rename fails here.
    """
    session = connections.session()
    try:
        assert (
            uow_module._open_connections(session.sync_session.get_transaction()) == []
        )
        await session.execute(text("SELECT 1"))
        transaction = session.sync_session.get_transaction()
        connection = await session.connection()
        assert uow_module._open_connections(transaction) == [connection.sync_connection]
    finally:
        await session.close()


@pytest.mark.usefixtures("paying")
async def test_discarding_leaves_a_closed_connection_alone() -> None:
    """A closed connection is already back in the pool: invalidating it would raise."""
    async with connections.engine().connect() as connection:
        sync = connection.sync_connection
        assert sync is not None
    assert sync.closed
    uow_module._discard([sync])  # No ResourceClosedError.
    assert checked_out() == 0


async def close_a_resting_connection() -> None:
    """Use a connection, give it back, then close its driver under the pool."""
    async with connections.engine().connect() as connection:
        await connection.execute(text("SELECT 1"))
        raw = await connection.get_raw_connection()
        driver: Any = raw.driver_connection
    if connections.engine().dialect.driver == "asyncpg":
        driver.terminate()
    else:
        stopped = driver.stop()  # aiosqlite's thread, once it has closed.
        assert stopped is not None
        await stopped


@BOUNDED
@pytest.mark.usefixtures("paying")
@pytest.mark.parametrize("repeating", [True, False], ids=["anyio-scope", "asyncio"])
async def test_a_unit_of_work_cut_as_its_connection_is_replaced_gives_it_back(
    repeating: bool,
) -> None:
    """Cut as the pool replaces a closed connection, it still returns the slot.

    A connection whose driver was closed while it rested in the pool is
    discarded as it leaves the pool; discarding it awaits nothing, so the
    cut cannot stop the discard halfway.
    """

    async def cut_at_checkout() -> None:
        if repeating:
            with anyio.CancelScope() as scope:
                scope.cancel()  # Lands at the first wait: the checkout.
                async with PaymentsUnitOfWork(readonly=True) as uow:
                    await uow.session.execute(text("SELECT 1"))
            assert scope.cancelled_caught
        else:
            current = asyncio.current_task()
            assert current is not None
            current.cancel()
            with pytest.raises(asyncio.CancelledError):
                async with PaymentsUnitOfWork(readonly=True) as uow:
                    await uow.session.execute(text("SELECT 1"))

    for _ in range(5):
        await close_a_resting_connection()
        async with asyncio.timeout(10):
            await asyncio.create_task(cut_at_checkout())
        assert checked_out() == 0
    async with asyncio.timeout(2), PaymentsUnitOfWork(readonly=True) as uow:
        assert await uow.session.scalar(text("SELECT 1")) == 1


def test_another_driver_keeps_sqlalchemys_discard() -> None:
    """Only asyncpg's and aiosqlite's discards are made to await nothing."""
    dialect = SimpleNamespace(driver="psycopg", do_terminate="SQLAlchemy's")

    _discard_at_once(SimpleNamespace(dialect=dialect))  # type: ignore[arg-type]

    assert dialect.do_terminate == "SQLAlchemy's"


class Driver:
    """An asyncpg connection's stand-in: its graceful close works or fails."""

    def __init__(self, *, closes: bool) -> None:
        """Open; ``closes`` says whether ``close()`` succeeds."""
        self.closes = closes
        self.closed = False
        self.terminated = False

    async def close(self) -> None:
        """Close gracefully, or fail as a connection gone does."""
        if not self.closes:
            raise OSError("The connection is gone.")
        self.closed = True

    def is_closed(self) -> bool:
        """Whether either close happened."""
        return self.closed or self.terminated

    def terminate(self) -> None:
        """Close at once."""
        self.terminated = True


def test_a_connection_discarded_outside_a_greenlet_closes_at_once() -> None:
    """Where the garbage collector discards it, its socket is closed at once."""
    driver = Driver(closes=True)

    _terminate_asyncpg(SimpleNamespace(driver_connection=driver))

    assert (driver.closed, driver.terminated) == (False, True)


@pytest.mark.parametrize("closes", [True, False], ids=["graceful", "at-once"])
async def test_a_discarded_asyncpg_connection_closes_gracefully_else_at_once(
    closes: bool,
) -> None:
    """The graceful close, and the close at once when that fails."""
    driver = Driver(closes=closes)

    await _close_asyncpg(driver)

    assert (driver.closed, driver.terminated) == (closes, not closes)
