"""Transactions for money (FIN-78): retries, isolation levels, and locks.

The proofs of concept come first: on 0.10, under the default isolation, two
transactions that each check a rule across rows and change a different row
both commit and break it (write skew); a serialization failure reached the
client as 503 ``database_busy`` with nothing to run the work again; and two
transfers that lock their rows in opposite orders deadlock.
"""

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any, ClassVar, Final
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.requests import HTTPConnection

from benchmarks.counting import SqlCounter
from domainkit import deadlines
from domainkit.clock import freeze_time
from domainkit.conf import override_settings
from domainkit.db import Isolation, connections, lock_key
from domainkit.ddd import (
    Command,
    ConcurrentUpdate,
    EventBus,
    LockNotAvailable,
    NotFound,
    ReadOnlyError,
    Retry,
    RowLocks,
)
from domainkit.ddd import uow as uow_module
from domainkit.ddd.query import F
from domainkit.ddd.uow import DEFAULT_RETRY, MAX_ATTEMPTS, NAMED_LOCKS
from domainkit.http.access import BasePermission, Public
from domainkit.http.errors import install_exception_handlers
from domainkit.http.middleware import RequestContextMiddleware
from domainkit.resources import ResourceRouter
from domainkit.testing import AsyncTestClient, assert_num_queries
from domainkit_tests.conftest import POSTGRES_VARIABLE
from domainkit_tests.gadgets import (
    FakeGadgetsUnitOfWork,
    Gadget,
    GadgetId,
    GadgetMade,
    GadgetsUnitOfWork,
    InMemoryGadgets,
    gadgets,
    make_gadget,
    metadata,
)
from domainkit_tests.sample_routes import DriverError
from domainkit_tests.test_resources_sql import Anonymous

pytestmark = pytest.mark.anyio
ON_POSTGRES: Final = bool(os.environ.get(POSTGRES_VARIABLE))
postgres_only = pytest.mark.skipif(not ON_POSTGRES, reason=f"set {POSTGRES_VARIABLE}")
NOW: Final = Retry(backoff=0)
"""Runs again at once: tests never wait for a backoff."""


def conflict(sqlstate: str = "40001") -> DBAPIError:
    """The error a driver raises for a serialization failure (or ``sqlstate``)."""
    return DBAPIError("COMMIT", None, DriverError(sqlstate))


class Lost(Exception):
    """A failure a test asks ``run`` to retry."""


@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The waits ``run`` makes before each next run, recorded instead of slept."""
    recorded: list[float] = []

    async def record(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(uow_module, "_sleep", record)
    return recorded


@pytest.fixture
def most(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every jittered wait is its longest."""
    monkeypatch.setattr(uow_module._random, "uniform", lambda low, high: high)


# The retry policy ---------------------------------------------------------------------


def test_retries_wait_up_to_a_doubling_capped_bound(most: None) -> None:
    """Full jitter: a wait is drawn up to the backoff, doubled per run, capped."""
    policy = Retry(attempts=6, backoff=0.1, max_backoff=0.5)

    assert [policy.delay(runs) for runs in range(1, 6)] == [0.1, 0.2, 0.4, 0.5, 0.5]
    assert Retry(attempts=3, backoff=0.02, max_backoff=1.0) == DEFAULT_RETRY
    assert Retry(backoff=1, max_backoff=60).delay(10_000) == 60
    assert Retry(retry_on=[Lost]).retry_on == (Lost,)  # type: ignore[arg-type]
    assert Retry(backoff=1e-25).delay(100) == 1e-25 * 2.0**64  # No overflow.
    assert Retry(attempts=MAX_ATTEMPTS, backoff=60, max_backoff=60).attempts == 10


def test_jittered_waits_spread_below_the_bound() -> None:
    """Without the stub, waits are random and within the bound."""
    policy = Retry(backoff=0.5, max_backoff=0.5)
    drawn = {policy.delay(1) for _ in range(50)}

    assert all(0 <= wait <= 0.5 for wait in drawn)
    assert len(drawn) > 1


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"attempts": 0}, "Retry attempts is 1 to 10 runs"),
        ({"attempts": MAX_ATTEMPTS + 1}, "Retry attempts is 1 to 10 runs"),
        ({"attempts": True}, "Retry attempts is a whole number"),
        ({"attempts": 2.0}, "Retry attempts is a whole number"),
        ({"backoff": -0.1}, "0 <= backoff <= max_backoff <= 60"),
        ({"backoff": float("nan")}, "0 <= backoff <= max_backoff <= 60"),
        ({"max_backoff": float("inf")}, "0 <= backoff <= max_backoff <= 60"),
        ({"backoff": 2.0, "max_backoff": 1.0}, "0 <= backoff <= max_backoff <= 60"),
        ({"max_backoff": 61.0}, "0 <= backoff <= max_backoff <= 60"),
        ({"retry_on": (ValueError, "Lost")}, "retry_on takes exception classes"),
        ({"retry_on": (KeyboardInterrupt,)}, "retry_on takes exception classes"),
    ],
)
def test_retry_policies_are_bounded(options: dict[str, Any], message: str) -> None:
    """Attempts, waits, and errors are checked when the policy is made."""
    with pytest.raises(ValueError, match=message):
        Retry(**options)


# Running work again, in memory --------------------------------------------------------


def fake(*stored: Gadget, events: EventBus | None = None) -> FakeGadgetsUnitOfWork:
    """An in-memory unit of work over ``stored``."""
    return FakeGadgetsUnitOfWork(InMemoryGadgets(stored), events=events)


def losing(times: int, error: Callable[[], Exception] = Lost) -> Callable[[], None]:
    """A check that raises ``error()`` the first ``times`` calls."""
    calls = iter(range(times + 1))

    def check() -> None:
        if next(calls) < times:
            raise error()

    return check


async def test_work_lost_to_a_race_runs_again_whole(waits: list[float]) -> None:
    """Each run starts again from what was stored; events go out once, after."""
    gadget = make_gadget("a", stock=1)
    published: list[GadgetMade] = []
    bus = EventBus()

    async def keep(event: GadgetMade) -> None:
        published.append(event)

    bus.subscribe(GadgetMade, keep)
    uow = fake(gadget, events=bus)
    lose = losing(2)
    runs: list[str] = []

    async def rename() -> str:
        async with uow:
            found = await uow.gadgets.get(gadget.id)
            runs.append(found.name)
            found.rename(f"run-{len(runs)}")
            await uow.gadgets.add(make_gadget(f"extra-{len(runs)}"))
            lose()
            await uow.commit()
        return found.name

    result = await uow.run(rename, retry=Retry(retry_on=(Lost,)))

    assert result == "run-3"
    assert runs == ["a", "a", "a"]  # Each run saw the stored state, not the last.
    assert sorted(each.name for each in uow.gadgets.items.values()) == [
        "extra-3",
        "run-3",
    ]
    assert [event.gadget_id for event in published] == [gadget.id]
    assert uow.commits == 1
    assert "_commits" not in vars(uow)  # repositories() walks vars(): untouched.
    assert len(waits) == 2
    assert all(0 <= wait <= 0.04 for wait in waits)


async def test_only_conflicts_and_named_errors_run_again(waits: list[float]) -> None:
    """Other errors are raised at once, as they were; in memory, nothing conflicts."""
    uow = fake()
    runs: list[str] = []

    async def broken() -> None:
        runs.append("broken")
        raise ValueError("a bug")

    async def stale() -> None:
        runs.append("stale")
        raise ConcurrentUpdate("Someone else changed it.")

    with pytest.raises(ValueError, match="a bug") as raised:
        await uow.run(broken, retry=Retry(retry_on=(Lost,)))
    with pytest.raises(ConcurrentUpdate):
        await uow.run(stale)  # Not named, so not run again.

    assert (runs, waits) == (["broken", "stale"], [])
    assert not hasattr(raised.value, "__notes__")
    assert uow._conflicted(conflict()) is False


async def test_the_last_failure_is_raised_with_its_cause(
    waits: list[float], most: None
) -> None:
    """After ``attempts`` runs, the last failure is raised itself, with a note."""
    uow = fake()
    failures: list[Lost] = []

    async def always() -> None:
        try:
            raise KeyError("the driver's")
        except KeyError as cause:
            failures.append(Lost(len(failures)))
            raise failures[-1] from cause

    with pytest.raises(Lost) as raised:
        await uow.run(always, retry=Retry(attempts=4, backoff=0.01, retry_on=(Lost,)))
    with pytest.raises(Lost) as once:
        await uow.run(always, retry=Retry(attempts=1, retry_on=(Lost,)))
    with pytest.raises(Lost) as twice:
        await uow.run(always, retry=Retry(attempts=2, backoff=0, retry_on=(Lost,)))

    assert raised.value is failures[3]
    assert isinstance(raised.value.__cause__, KeyError)
    assert raised.value.__notes__ == ["The work ran 4 times; each run lost a race."]
    assert waits == [0.01, 0.02, 0.04, 0]  # The two-run policy waits once.
    assert not hasattr(once.value, "__notes__")
    assert twice.value.__notes__ == ["The work ran 2 times; each run lost a race."]


async def test_work_that_committed_is_never_run_again(waits: list[float]) -> None:
    """A failure after a commit of this unit of work would commit twice: raised."""
    uow = fake(make_gadget("a"))
    runs = 0

    async def two_blocks() -> None:
        nonlocal runs
        runs += 1
        async with uow:
            await uow.commit()
        async with uow:
            raise Lost

    with pytest.raises(Lost):
        await uow.run(two_blocks, retry=Retry(retry_on=(Lost,)))

    assert (runs, waits) == (1, [])


async def test_a_run_inside_another_counts_its_commits_for_both(
    waits: list[float],
) -> None:
    """The inner run committed, so the outer is not run again; nothing is left."""
    uow = fake(make_gadget("a"))
    runs = 0

    async def inner() -> None:
        async with uow:
            await uow.commit()

    async def outer() -> None:
        nonlocal runs
        runs += 1
        await uow.run(inner)
        raise Lost

    with pytest.raises(Lost):
        await uow.run(outer, retry=Retry(retry_on=(Lost,)))

    assert (runs, waits) == (1, [])
    assert "_commits" not in vars(uow)  # repositories() walks vars(): untouched.


async def test_no_wait_passes_the_deadline(waits: list[float], most: None) -> None:
    """A wait that would end past the deadline raises the failure instead."""
    uow = fake()
    lose = losing(5)

    async def work() -> None:
        lose()

    with freeze_time():
        async with deadlines.timeout(0.5):  # A wait to the deadline is too long.
            with pytest.raises(Lost) as raised:
                await uow.run(work, retry=Retry(backoff=0.5, retry_on=(Lost,)))
        async with deadlines.timeout(10):
            await uow.run(work, retry=Retry(attempts=5, backoff=0.1, retry_on=(Lost,)))

    assert not hasattr(raised.value, "__notes__")  # The first run's failure.
    assert waits == [0.1, 0.2, 0.4, 0.8]


async def test_each_run_lost_is_logged(
    waits: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    """At INFO, by run, unit of work, and the error's class: never its text."""
    uow = fake()
    lose = losing(1)

    async def work() -> None:
        lose()

    with caplog.at_level(logging.INFO, logger="domainkit.ddd.uow"):
        await uow.run(work, retry=Retry(retry_on=(Lost,)))

    assert [record.getMessage() for record in caplog.records] == [
        "Run 1 of FakeGadgetsUnitOfWork lost a race (Lost); running it again"
    ]


async def test_waits_follow_the_frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under a frozen clock the wait only yields: tests never sleep for it."""
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    with freeze_time():
        await uow_module._sleep(30.0)
    await uow_module._sleep(0.5)

    assert slept == [0, 0.5]


# Named locks, in memory ---------------------------------------------------------------


async def test_named_locks_are_held_until_the_work_ends() -> None:
    """A second unit of work waits for the name; nowait refuses it."""
    one, two = fake(), fake()
    async with one:
        await one.lock("payout:acct-1")
        await one.lock("payout:acct-1")  # Held already: nothing changes.
        async with two:
            async with asyncio.timeout(1):  # Refused at once, not after a wait.
                with pytest.raises(LockNotAvailable):
                    await two.lock("payout:acct-1", nowait=True)
            await two.lock("payout:acct-2", nowait=True)
            waiting = asyncio.create_task(two.lock("payout:acct-1"))
            done, _ = await asyncio.wait({waiting}, timeout=0.05)
            assert not done
            await one.commit()
            async with asyncio.timeout(1):
                await waiting
            assert NAMED_LOCKS.holder("payout:acct-1") is two
    assert NAMED_LOCKS.holder("payout:acct-1") is None
    assert NAMED_LOCKS.holder("payout:acct-2") is None
    assert (one._named_locks, two._named_locks) == (False, False)


@pytest.mark.parametrize("name", ["", None, 7])
async def test_lock_names_are_text(name: Any) -> None:  # noqa: ANN401 - wrong on purpose.
    """A lock is named by a non-empty string."""
    with pytest.raises(ValueError, match="A lock's name is a non-empty string"):
        await fake().lock(name)


async def test_read_only_work_takes_no_locks() -> None:
    """Locks are for work that changes something."""
    uow = FakeGadgetsUnitOfWork(InMemoryGadgets(), readonly=True)
    with pytest.raises(
        ReadOnlyError, match=r"^FakeGadgetsUnitOfWork is read-only; it takes no locks"
    ):
        await uow.lock("x")


# Locking several rows, in memory ------------------------------------------------------


async def test_rows_are_locked_by_id_whatever_order_they_are_asked_in() -> None:
    """The answer and the locks go by ascending id."""
    first, second, third = sorted(
        (make_gadget(name) for name in "abc"), key=lambda gadget: gadget.id
    )
    shared = InMemoryGadgets([first, second, third], locks=RowLocks(timeout=0.05))
    other = InMemoryGadgets(locks=shared.locks)
    other.items = shared.items

    found = await shared.get_many_for_update([third.id, first.id, third.id])

    assert list(found) == [first.id, third.id]
    assert found[third.id] is third
    assert await shared.get_many_for_update([]) == {}
    assert list(
        await other.get_many_for_update([second.id, third.id], skip_locked=True)
    ) == [second.id]
    with pytest.raises(LockNotAvailable):
        await other.get_many_for_update([first.id], nowait=True)
    with pytest.raises(NotFound, match=f"No aggregate has id {UUID(int=0)}"):
        await shared.get_many_for_update([first.id, GadgetId(UUID(int=0))])


async def test_a_failed_savepoint_drops_events_of_what_it_first_loaded() -> None:
    """In memory, events recorded inside are dropped, also by an aggregate it loaded."""
    gadget = make_gadget("a")
    uow = fake(gadget)

    async def rename() -> None:
        async with uow.savepoint():
            (await uow.gadgets.get(gadget.id)).rename("b")
            raise Lost

    async with uow:
        with pytest.raises(Lost):
            await rename()
        again = await uow.gadgets.get(gadget.id)

    assert (again.name, again.pending_events) == ("a", ())


# Isolation levels ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "level"),
    [
        ("serializable", Isolation.SERIALIZABLE),
        ("repeatable_read", Isolation.REPEATABLE_READ),
        (" Read Committed ", Isolation.READ_COMMITTED),
        ("SERIALIZABLE", Isolation.SERIALIZABLE),
    ],
)
def test_isolation_levels_are_read_in_any_case(given: str, level: Isolation) -> None:
    """The names SQL uses, in any case, with ``_`` or spaces."""
    assert Isolation(given) is level


@pytest.mark.parametrize("given", ["snapshot", "read uncommitted", 3])
def test_unknown_isolation_levels_are_refused(given: object) -> None:
    """Only the three levels both databases define."""
    with pytest.raises(ValueError, match="is not a valid Isolation"):
        GadgetsUnitOfWork(isolation=given)  # type: ignore[arg-type]


class SerializableGadgets(GadgetsUnitOfWork):
    """Gadgets in serializable transactions, declared on the class."""

    isolation = "serializable"  # type: ignore[assignment] # Read as Isolation.


def test_isolation_is_declared_or_given_and_only_for_writes() -> None:
    """The class's level, or the one given; a read-only unit of work says reads=."""
    assert SerializableGadgets().isolation is Isolation.SERIALIZABLE
    assert SerializableGadgets(isolation="read committed").isolation is (
        Isolation.READ_COMMITTED
    )
    assert GadgetsUnitOfWork().isolation is None
    with pytest.raises(ValueError, match="isolation=") as refused:
        GadgetsUnitOfWork(readonly=True, isolation=Isolation.SERIALIZABLE)
    with pytest.raises(ValueError, match="isolation= is for units of work that write"):
        SerializableGadgets(readonly=True)

    assert str(refused.value) == (
        "isolation= is for units of work that write; a read-only one picks how "
        "it reads with reads= (Reads.SNAPSHOT reads one snapshot)."
    )


@pytest.fixture
def database(database_url: str) -> Iterator[str]:
    """An empty database, selected in the settings."""
    with override_settings(DATABASES={"default": {"URL": database_url}}):
        yield database_url


@pytest.fixture
async def tables(database: str) -> AsyncIterator[None]:
    """The gadget tables, in the running loop's engine."""
    async with connections.engine().begin() as connection:
        await connection.run_sync(metadata.create_all)
    yield
    await connections.close_all()


async def store(*items: Gadget) -> None:
    """Commit ``items`` in one unit of work."""
    async with GadgetsUnitOfWork() as uow:
        await uow.gadgets.add_all(items)
        await uow.commit()


async def total_stock() -> int:
    """The stock of every gadget, summed."""
    async with GadgetsUnitOfWork() as uow:
        return int(await uow.session.scalar(select(func.sum(gadgets.c.stock))) or 0)


async def test_a_failed_sql_savepoint_drops_events_of_what_it_first_loaded(
    tables: None,
) -> None:
    """The session keeps the aggregate a failed savepoint loaded, not its events.

    While the caller holds it, loading it again gives back the same object:
    the events it recorded inside, for changes the savepoint undid, were
    published by the commit.
    """
    gadget = make_gadget("a")
    await store(gadget)
    published: list[GadgetMade] = []
    bus = EventBus()

    async def keep(event: GadgetMade) -> None:
        published.append(event)

    bus.subscribe(GadgetMade, keep)
    held: list[Gadget] = []

    async def rename(uow: GadgetsUnitOfWork) -> None:
        async with uow.savepoint():
            held.append(await uow.gadgets.get(gadget.id))
            held[0].rename("b")
            raise Lost

    async with GadgetsUnitOfWork(events=bus) as uow:
        with pytest.raises(Lost):
            await rename(uow)
        again = await uow.gadgets.get(gadget.id)
        assert again is held[0]
        assert (again.name, again.pending_events) == ("a", ())
        again.rename("c")
        await uow.commit()

    assert len(published) == 1


@postgres_only
async def test_the_level_is_set_as_the_transaction_begins(tables: None) -> None:
    """In ``BEGIN`` itself: no statement more; the pool's connection is reset."""

    async def level(**options: Any) -> tuple[str, int]:  # noqa: ANN401 - the options.
        with SqlCounter() as sql:
            async with GadgetsUnitOfWork(**options) as uow:
                shown = await uow.session.scalar(text("SHOW transaction_isolation"))
                await uow.gadgets.count()
                await uow.commit()
        return str(shown), sql.counts.statements

    plain = await level()
    levels = [await level(isolation=each) for each in Isolation]

    assert plain == ("read committed", 2)
    assert levels == [
        ("read committed", 2),
        ("repeatable read", 2),
        ("serializable", 2),
    ]
    assert await level() == plain  # Back in the pool at its default.


@pytest.fixture
async def sqlite_file(tmp_path: Path) -> AsyncIterator[str]:
    """The gadget tables in a SQLite file, whatever server the suite targets.

    A writer waits 0.1 seconds for another's lock, then fails.
    """
    url = f"sqlite+aiosqlite:///{tmp_path / 'gadgets.sqlite3'}"
    with override_settings(
        DATABASES={"default": {"URL": url, "CONNECT_ARGS": {"timeout": 0.1}}}
    ):
        async with connections.engine().begin() as connection:
            await connection.run_sync(metadata.create_all)
        yield url
        await connections.close_all()


async def test_sqlite_runs_serializable_work_one_at_a_time(sqlite_file: str) -> None:
    """``BEGIN IMMEDIATE``: the write lock is taken before the first read.

    So a second such unit of work waits for the first (here, past the 0.1
    seconds it may wait, it fails); one at ``READ COMMITTED`` reads freely.
    """
    gadget = make_gadget("a", stock=1)
    await store(gadget)
    async with GadgetsUnitOfWork(isolation=Isolation.SERIALIZABLE) as one:
        assert (await one.gadgets.get(gadget.id)).stock == 1
        async with GadgetsUnitOfWork(isolation="repeatable read") as two:
            with pytest.raises(OperationalError, match="database is locked"):
                await two.gadgets.count()
        async with GadgetsUnitOfWork(isolation=Isolation.READ_COMMITTED) as three:
            assert await three.gadgets.count() == 1
        await one.commit()
    async with GadgetsUnitOfWork(isolation=Isolation.SERIALIZABLE) as two:
        assert await two.gadgets.count() == 1
        await two.lock("written")  # In its transaction already: nothing to do.
        await two.commit()


async def test_units_of_work_at_one_level_share_one_bind(sqlite_file: str) -> None:
    """The engine with a level's options is made once per engine, sharing its pool."""
    async with (
        GadgetsUnitOfWork(isolation=Isolation.SERIALIZABLE) as one,
        GadgetsUnitOfWork(isolation=Isolation.SERIALIZABLE) as two,
        GadgetsUnitOfWork(isolation=Isolation.READ_COMMITTED) as three,
    ):
        binds = [uow.session.bind for uow in (one, two, three)]

    assert binds[0] is binds[1]
    assert binds[2] is not binds[0]
    assert all(
        isinstance(bind, AsyncEngine)
        and bind.sync_engine.pool is connections.engine().sync_engine.pool
        for bind in binds
    )


async def test_sqlite_begins_without_a_statement_of_the_work(
    sqlite_file: str,
) -> None:
    """``BEGIN IMMEDIATE`` is the framework's: ``assert_num_queries`` skips it."""
    async with GadgetsUnitOfWork(isolation=Isolation.SERIALIZABLE) as one:
        with assert_num_queries(1):
            await one.gadgets.count()
    async with GadgetsUnitOfWork() as two:
        with assert_num_queries(1):
            await two.lock("x")
            await two.gadgets.count()


async def in_transaction(uow: GadgetsUnitOfWork) -> bool:
    """Whether SQLite's driver holds a transaction open for ``uow``'s session."""
    connection = await uow.session.connection()
    raw = await connection.get_raw_connection()
    return bool(raw.driver_connection.in_transaction)  # type: ignore[union-attr]


async def test_sqlite_begins_before_a_first_savepoint(sqlite_file: str) -> None:
    """Its ``RELEASE`` no longer commits: work left uncommitted leaves nothing.

    SQLite's driver begins a transaction only before a write, so a
    ``SAVEPOINT`` sent first began one, and its ``RELEASE`` committed it.
    The ``BEGIN`` is the framework's: ``assert_num_queries`` skips it.
    """
    async with GadgetsUnitOfWork() as uow:
        with assert_num_queries(3):  # SAVEPOINT, INSERT, RELEASE.
            async with uow.savepoint():
                await uow.gadgets.add(make_gadget("a", stock=5))
        assert await in_transaction(uow)
    assert await total_stock() == 0

    async with GadgetsUnitOfWork() as uow:
        await uow.gadgets.count()  # A read: still outside a transaction.
        assert not await in_transaction(uow)
        async with uow.savepoint():
            await uow.gadgets.add(make_gadget("b", stock=2))
        await uow.commit()
    assert await total_stock() == 2


async def test_sqlite_savepoints_inside_a_transaction_begin_nothing(
    sqlite_file: str,
) -> None:
    """After a write, or ``BEGIN IMMEDIATE``, the transaction is begun already."""
    async with GadgetsUnitOfWork() as uow:
        await uow.gadgets.add(make_gadget("a", stock=1))
        await uow.session.flush()
        with assert_num_queries(3):  # SAVEPOINT, INSERT, RELEASE: no BEGIN.
            async with uow.savepoint():
                await uow.gadgets.add(make_gadget("b", stock=1))
        await uow.commit()
    async with GadgetsUnitOfWork(isolation=Isolation.SERIALIZABLE) as uow:
        async with uow.savepoint():
            await uow.gadgets.add(make_gadget("c", stock=1))
        assert await in_transaction(uow)
    assert await total_stock() == 2


async def test_sqlite_autocommit_savepoints_are_left_as_they_are(
    sqlite_file: str,
) -> None:
    """A connection outside transactions gets no ``BEGIN``: its ``RELEASE`` commits."""
    await store(make_gadget("a", stock=0))
    async with connections.autocommit() as connection:
        async with connection.begin_nested():
            await connection.execute(gadgets.update().values(stock=3))
        raw = await connection.get_raw_connection()
        assert not raw.driver_connection.in_transaction  # type: ignore[union-attr]
    assert await total_stock() == 3


async def test_sqlite_keeps_a_rule_across_rows_when_serializable(
    sqlite_file: str,
) -> None:
    """The write skew of the proof cannot happen: the second sees the first's change."""
    one, two = make_gadget("one", stock=1), make_gadget("two", stock=1)
    await store(one, two)

    async def take(gadget_id: GadgetId) -> str:
        async with GadgetsUnitOfWork(isolation=Isolation.SERIALIZABLE) as uow:
            total = await uow.session.scalar(select(func.sum(gadgets.c.stock)))
            await asyncio.sleep(0.01)
            if total < 2:
                return "refused"
            gadget = await uow.gadgets.get(gadget_id)
            gadget.stock = (gadget.stock or 0) - 1
            await uow.commit()
            return "took"

    await connections.close_all()  # The fixture's engine waits 0.1 seconds.
    with override_settings(
        DATABASES={"default": {"URL": sqlite_file, "CONNECT_ARGS": {"timeout": 5}}}
    ):
        outcomes = await asyncio.gather(take(one.id), take(two.id))
        left = await total_stock()
        await connections.close_all()

    assert sorted(outcomes) == ["refused", "took"]
    assert left == 1


# Write skew, and serialization failures run again -------------------------------------


async def take_if_two_left(
    gadget_id: GadgetId, uow: GadgetsUnitOfWork, both_read: asyncio.Barrier
) -> str:
    """Take one of the gadget's stock if, across gadgets, at least two are left."""
    async with uow:
        total = await uow.session.scalar(select(func.sum(gadgets.c.stock)))
        await both_read.wait()
        if total < 2:
            return "refused"
        gadget = await uow.gadgets.get(gadget_id)
        gadget.stock = (gadget.stock or 0) - 1
        await uow.commit()
        return "took"


@postgres_only
async def test_write_skew_under_the_default_isolation(tables: None) -> None:
    """The proof of FIN-78: each checks the rule, changes another row, and commits.

    Versions do not help: no row is changed twice. The rule (at least one
    left across gadgets) is broken.
    """
    one, two = make_gadget("one", stock=1), make_gadget("two", stock=1)
    await store(one, two)
    both_read = asyncio.Barrier(2)

    outcomes = await asyncio.gather(
        take_if_two_left(one.id, GadgetsUnitOfWork(), both_read),
        take_if_two_left(two.id, GadgetsUnitOfWork(), both_read),
    )

    assert list(outcomes) == ["took", "took"]
    assert await total_stock() == 0


@postgres_only
async def test_serializable_work_run_again_keeps_the_rule(
    tables: None, waits: list[float]
) -> None:
    """One transaction fails to serialize; run again, it sees the other's change."""
    one, two = make_gadget("one", stock=1), make_gadget("two", stock=1)
    await store(one, two)
    both_read = asyncio.Barrier(2)
    settled = asyncio.Event()
    runs: list[str] = []

    async def take(gadget_id: GadgetId) -> str:
        uow = GadgetsUnitOfWork(isolation=Isolation.SERIALIZABLE)

        async def work() -> str:
            runs.append(str(gadget_id))
            if len(runs) > 2:  # Run again: once the winner is done, as a wait would.
                await settled.wait()
                return await take_if_two_left(gadget_id, uow, asyncio.Barrier(1))
            outcome = await take_if_two_left(gadget_id, uow, both_read)
            settled.set()
            return outcome

        return await uow.run(work, retry=NOW)

    outcomes = await asyncio.gather(take(one.id), take(two.id))

    assert sorted(outcomes) == ["refused", "took"]
    assert len(runs) == 3
    assert waits == [0.0]
    assert await total_stock() == 1


@postgres_only
async def test_conflicts_are_postgresqls_serialization_failures_and_deadlocks(
    tables: None,
) -> None:
    """``40001`` and ``40P01`` run again; other database errors do not."""
    uow = GadgetsUnitOfWork()

    assert uow._conflicted(conflict("40001"))
    assert uow._conflicted(conflict("40P01"))
    assert not uow._conflicted(conflict("55P03"))
    assert not uow._conflicted(Lost())


# Locking several rows in order --------------------------------------------------------


@postgres_only
async def test_transfers_locking_in_opposite_orders_deadlock(tables: None) -> None:
    """The proof: each locks its first row, then waits for the other's."""
    a, b = make_gadget("a", stock=10), make_gadget("b", stock=10)
    await store(a, b)
    both_locked = asyncio.Barrier(2)

    async def transfer(source: GadgetId, target: GadgetId) -> str:
        try:
            async with GadgetsUnitOfWork() as uow:
                first = await uow.gadgets.get_for_update(source)
                await both_locked.wait()
                second = await uow.gadgets.get_for_update(target)
                first.stock = (first.stock or 0) - 1
                second.stock = (second.stock or 0) + 1
                await uow.commit()
                return "moved"
        except DBAPIError as exc:
            return f"SQLSTATE {getattr(exc.orig, 'sqlstate', None)}"

    outcomes = await asyncio.gather(transfer(a.id, b.id), transfer(b.id, a.id))

    assert sorted(outcomes) == ["SQLSTATE 40P01", "moved"]


@postgres_only
async def test_locking_in_id_order_cannot_deadlock(tables: None) -> None:
    """``get_many_for_update`` locks by id, whatever order the ids come in."""
    a, b = make_gadget("a", stock=10), make_gadget("b", stock=10)
    await store(a, b)
    both_asked = asyncio.Barrier(2)

    async def transfer(source: GadgetId, target: GadgetId) -> str:
        async with GadgetsUnitOfWork() as uow:
            await both_asked.wait()
            found = await uow.gadgets.get_many_for_update([source, target])
            found[source].stock = (found[source].stock or 0) - 1
            found[target].stock = (found[target].stock or 0) + 1
            await uow.commit()
            return "moved"

    async with asyncio.timeout(10):
        outcomes = await asyncio.gather(transfer(a.id, b.id), transfer(b.id, a.id))

    assert list(outcomes) == ["moved", "moved"]
    assert await total_stock() == 20


@postgres_only
async def test_a_deadlocked_transfer_is_run_again(
    tables: None, waits: list[float]
) -> None:
    """With ``run``, the transfer PostgreSQL chose to fail runs again and moves."""
    a, b = make_gadget("a", stock=10), make_gadget("b", stock=10)
    await store(a, b)
    both_locked = asyncio.Barrier(2)
    settled = asyncio.Event()

    async def transfer(source: GadgetId, target: GadgetId) -> None:
        uow = GadgetsUnitOfWork()
        runs = 0

        async def work() -> None:
            nonlocal runs
            runs += 1
            if runs > 1:  # Once the winner is done, as a wait would: run again
                await settled.wait()  # at once, it could lock its first row
            async with uow:  # before the winner relocks it, and deadlock again.
                first = await uow.gadgets.get_for_update(source)
                if runs == 1:
                    await both_locked.wait()
                second = await uow.gadgets.get_for_update(target)
                first.stock = (first.stock or 0) - 1
                second.stock = (second.stock or 0) + 2
                await uow.commit()
            settled.set()

        await uow.run(work, retry=NOW)

    async with asyncio.timeout(10):
        await asyncio.gather(transfer(a.id, b.id), transfer(b.id, a.id))

    assert waits == [0.0]
    assert await total_stock() == 22


async def test_several_rows_are_read_in_sql_as_in_memory(tables: None) -> None:
    """By ascending id; missing ones are ``NotFound`` unless skipped."""
    first, second = sorted(
        (make_gadget("a"), make_gadget("b")), key=lambda gadget: gadget.id
    )
    await store(first, second)
    async with GadgetsUnitOfWork() as uow:
        found = await uow.gadgets.get_many_for_update([second.id, first.id])
        skipped = await uow.gadgets.get_many_for_update(
            [GadgetId(UUID(int=0)), second.id], skip_locked=True
        )
        with pytest.raises(NotFound):
            await uow.gadgets.get_many_for_update([GadgetId(UUID(int=0))])

    assert [(key, each.name) for key, each in found.items()] == [
        (first.id, first.name),
        (second.id, second.name),
    ]
    assert list(skipped) == [second.id]


@postgres_only
async def test_rows_another_holds_are_refused_or_skipped(tables: None) -> None:
    """``nowait`` and ``skip_locked``, as for one row."""
    a, b = make_gadget("a"), make_gadget("b")
    await store(a, b)
    async with GadgetsUnitOfWork() as one, GadgetsUnitOfWork() as two:
        await one.gadgets.get_many_for_update([a.id])
        with pytest.raises(LockNotAvailable):
            await two.gadgets.get_many_for_update([b.id, a.id], nowait=True)
        free = await two.gadgets.get_many_for_update([a.id, b.id], skip_locked=True)

    assert list(free) == [b.id]


# Named locks in the database ----------------------------------------------------------


def test_lock_keys_are_stable_64_bit_hashes() -> None:
    """The same in every process and release: ``pg_locks`` can be searched for them."""
    assert lock_key("payout:acct-1") == lock_key("payout:acct-1")
    assert lock_key("payout:acct-1") != lock_key("payout:acct-2")
    assert lock_key("payout:acct-1") == 8519040223794619761
    assert all(-(2**63) <= lock_key(name) < 2**63 for name in ("", "é", "x" * 999))


@postgres_only
async def test_named_locks_are_advisory_locks_of_the_transaction(
    tables: None,
) -> None:
    """Held until commit or rollback; another waits, or is refused with nowait."""
    async with GadgetsUnitOfWork() as one, GadgetsUnitOfWork() as two:
        await one.lock("payout:acct-1")
        with pytest.raises(
            LockNotAvailable,
            match=r"^The data is being changed by another request; try again\.$",
        ):
            await two.lock("payout:acct-1", nowait=True)
        await two.lock("payout:acct-2", nowait=True)
        held = await two.session.scalar(
            text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                "AND objsubid = 1 AND ((classid::bigint << 32) | objid::bigint) "
                "= (:key & x'FFFFFFFFFFFFFFFF'::bigint)"
            ),
            {"key": lock_key("payout:acct-1")},
        )
        waiting = asyncio.create_task(two.lock("payout:acct-1"))
        done, _ = await asyncio.wait({waiting}, timeout=0.2)
        assert not done
        await one.commit()
        async with asyncio.timeout(5):
            await waiting
    async with GadgetsUnitOfWork() as three:
        await three.lock("payout:acct-1", nowait=True)  # Gone with two's rollback.

    assert held == 1


async def test_named_locks_refuse_read_only_work(tables: None) -> None:
    """As row locks do."""
    async with GadgetsUnitOfWork(readonly=True) as uow:
        with pytest.raises(ReadOnlyError):
            await uow.lock("x")
        with pytest.raises(ValueError, match="non-empty string"):
            await uow.lock("")


async def test_a_named_lock_on_sqlite_is_the_write_lock(sqlite_file: str) -> None:
    """``BEGIN IMMEDIATE``: another writer waits (here past 0.1 s, and fails)."""
    async with GadgetsUnitOfWork() as one:
        await one.lock("payout:acct-1", nowait=True)
        async with GadgetsUnitOfWork() as two:
            with pytest.raises(OperationalError, match="database is locked"):
                await two.lock("anything")
        await one.commit()
    async with GadgetsUnitOfWork() as two:
        await two.lock("anything")


# Command routes that declare retries --------------------------------------------------


class Take(Command):
    """Take one of a gadget's stock."""

    gadget_id: UUID


class Restock(Command):
    """Add stock to every gadget."""

    amount: int


class Amount(BaseModel):
    """How much stock to add."""

    amount: int


def gadget_app(
    service: Any,  # noqa: ANN401 - each test's service.
    *,
    retry: Retry | None,
    unit_of_work: type[GadgetsUnitOfWork] = GadgetsUnitOfWork,
    scope: Callable[[HTTPConnection], Awaitable[Any]] | None = None,
) -> FastAPI:
    """The gadget resource with ``take`` and ``restock`` routes run by ``service``."""
    resource = ResourceRouter(
        "/gadgets",
        model=Gadget,
        unit_of_work=unit_of_work,
        repository=lambda uow: uow.gadgets,
        dependencies=[Public],
        scope=scope,
    )
    resource.command_route(Take, service=service, path="/take", retry=retry)
    resource.command_route(
        Restock,
        service=service,
        body=Amount,
        path="/restock",
        returns=None,
        retry=retry,
    )
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(resource)
    app.add_middleware(RequestContextMiddleware)
    return app


@postgres_only
@pytest.mark.parametrize("retry", [None, NOW])
async def test_a_serialization_failure_is_run_again_by_a_route_that_retries(
    tables: None, waits: list[float], retry: Retry | None
) -> None:
    """The proof: without ``retry`` the loser answers 503; with it, both succeed."""
    one, two = make_gadget("one", stock=5), make_gadget("two", stock=5)
    await store(one, two)
    both_read = asyncio.Barrier(2)
    settled = asyncio.Event()
    runs: list[UUID] = []

    async def take(command: Take, uow: GadgetsUnitOfWork) -> Gadget:
        runs.append(command.gadget_id)
        first = len(runs) <= 2
        if not first:  # Run again: once the winner is done, as a wait would.
            await settled.wait()
        async with uow:
            await uow.session.scalar(select(func.sum(gadgets.c.stock)))
            if first:
                await both_read.wait()
            found = await uow.gadgets.get(GadgetId(command.gadget_id))
            found.stock = (found.stock or 0) - 1
            await uow.commit()
        settled.set()
        return found

    app = gadget_app(take, retry=retry, unit_of_work=SerializableGadgets)
    async with AsyncTestClient(app) as client:
        answers = await asyncio.gather(
            client.post(f"/gadgets/{one.id}/take", json={}),
            client.post(f"/gadgets/{two.id}/take", json={}),
        )
    statuses = sorted(answer.status_code for answer in answers)

    if retry is None:
        assert statuses == [200, 503]
        assert {answer.json().get("code") for answer in answers} >= {"database_busy"}
        assert (len(runs), await total_stock()) == (2, 9)
    else:
        assert statuses == [200, 200]
        assert (len(runs), waits, await total_stock()) == (3, [0.0], 8)


async def test_a_route_runs_its_check_and_service_again_in_a_new_transaction(
    tables: None, waits: list[float]
) -> None:
    """The reach check and ``If-Match`` are made again with each run."""
    gadget = make_gadget("one", stock=5)
    await store(gadget)
    checks: list[str] = []
    lose = losing(1, conflict)

    async def scope(request: HTTPConnection) -> Any:  # noqa: ANN401 - criteria.
        checks.append("scope")
        return F("active") == True  # noqa: E712 - a condition, not a test.

    async def take(command: Take, uow: GadgetsUnitOfWork) -> Gadget:
        async with uow:
            found = await uow.gadgets.get(GadgetId(command.gadget_id))
            found.stock = (found.stock or 0) - 1
            lose()
            await uow.commit()
            return found

    async def restock(command: Restock, uow: GadgetsUnitOfWork) -> None:
        async with uow:
            for found in await uow.gadgets.filter():
                found.stock = (found.stock or 0) + command.amount
            lose_again()
            await uow.commit()

    async with AsyncTestClient(gadget_app(take, retry=NOW, scope=scope)) as client:
        etag = (await client.get(f"/gadgets/{gadget.id}")).headers["etag"]
        checks.clear()
        taken = await client.post(
            f"/gadgets/{gadget.id}/take", json={}, headers={"If-Match": etag}
        )
        stale = await client.post(
            f"/gadgets/{gadget.id}/take", json={}, headers={"If-Match": etag}
        )
    lose_again = losing(1, conflict)
    async with AsyncTestClient(gadget_app(restock, retry=NOW)) as client:
        restocked = await client.post("/gadgets/restock", json={"amount": 3})

    assert (taken.status_code, taken.json()["stock"]) == (200, 4)
    assert stale.status_code == 412
    assert checks == ["scope", "scope", "scope"]
    assert restocked.status_code == 204
    assert waits == [0.0, 0.0]
    assert await total_stock() == 7


class SeesGadgets(BasePermission):
    """A rule that looks at each gadget it is asked about, and allows it."""

    seen: ClassVar[list[str]] = []

    async def has_object_permission(
        self,
        request: HTTPConnection,
        user: Any,  # noqa: ANN401 - any principal.
        obj: object,
    ) -> bool:
        """Note the gadget's name."""
        self.seen.append(getattr(obj, "name", ""))
        return True


async def test_a_rule_that_looks_at_the_aggregate_is_asked_in_each_run(
    tables: None, waits: list[float]
) -> None:
    """Where a rule looks at objects, each run reads the aggregate and asks it."""
    gadget = make_gadget("one", stock=5)
    await store(gadget)
    lose = losing(1, conflict)
    SeesGadgets.seen.clear()

    async def take(command: Take, uow: GadgetsUnitOfWork) -> Gadget:
        async with uow:
            found = await uow.gadgets.get(GadgetId(command.gadget_id))
            found.stock = (found.stock or 0) - 1
            lose()
            await uow.commit()
            return found

    resource = ResourceRouter(
        "/gadgets",
        model=Gadget,
        unit_of_work=GadgetsUnitOfWork,
        repository=lambda uow: uow.gadgets,
        dependencies=[Public],
    )
    resource.command_route(
        Take, service=take, path="/take", rule=SeesGadgets, retry=NOW
    )
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(resource)
    app.add_middleware(Anonymous)
    async with AsyncTestClient(app) as client:
        taken = await client.post(f"/gadgets/{gadget.id}/take", json={})

    assert (taken.status_code, taken.json()["stock"]) == (200, 4)
    assert SeesGadgets.seen == ["one", "one"]
    assert waits == [0.0]


# Batch routes that declare retries ------------------------------------------------


class Taken(BaseModel):
    """A batch item's body: nothing but the gadget it takes from."""


def batch_app(
    service: Any,  # noqa: ANN401 - each test's service.
    *,
    retry: Retry | None,
    unit_of_work: type[GadgetsUnitOfWork] = GadgetsUnitOfWork,
) -> FastAPI:
    """The gadget resource with ``POST /gadgets/batch`` run by ``service``."""
    resource = ResourceRouter(
        "/gadgets",
        model=Gadget,
        unit_of_work=unit_of_work,
        repository=lambda uow: uow.gadgets,
        dependencies=[Public],
    )
    resource.batch_route(Take, service=service, retry=retry)
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(resource)
    app.add_middleware(RequestContextMiddleware)
    return app


def taking(lose: Callable[[], None], runs: list[str]) -> Any:  # noqa: ANN401 - a service.
    """A service that takes one of a gadget's stock, and may lose a race first."""

    async def take(command: Take, uow: GadgetsUnitOfWork) -> Gadget:
        async with uow:
            found = await uow.gadgets.get(GadgetId(command.gadget_id))
            runs.append(found.name)
            found.stock = (found.stock or 0) - 1
            if found.name == "two":
                lose()
            await uow.commit()
            return found

    return take


@pytest.mark.parametrize("atomic", [False, True])
@pytest.mark.parametrize("retry", [None, NOW])
async def test_a_batch_that_retries_runs_a_lost_item_again(
    tables: None, waits: list[float], retry: Retry | None, atomic: bool
) -> None:
    """Without ``retry`` the item that lost answers 503; with it, every item 200.

    Apart, the item that lost runs again; atomically, the whole batch does,
    in a new transaction, since the race ended it.
    """
    one, two = make_gadget("one", stock=5), make_gadget("two", stock=5)
    await store(one, two)
    runs: list[str] = []
    app = batch_app(taking(losing(1, conflict), runs), retry=retry)
    items = [{"gadget_id": str(one.id)}, {"gadget_id": str(two.id)}]

    async with AsyncTestClient(app) as client:
        answer = await client.post(
            "/gadgets/batch", json={"items": items, "atomic": atomic}
        )
    statuses = [each["status"] for each in answer.json()["results"]]

    assert answer.status_code == 207
    if retry is None:
        assert statuses == ([424, 503] if atomic else [200, 503])
        assert answer.json()["results"][1]["error"]["code"] == "database_busy"
        assert (runs, waits) == (["one", "two"], [])
        assert await total_stock() == (10 if atomic else 9)
    else:
        assert statuses == [200, 200]
        again = ["one", "two", "one", "two"] if atomic else ["one", "two", "two"]
        assert (runs, waits) == (again, [0.0])
        assert await total_stock() == 8


async def test_an_atomic_batch_lost_at_its_commit_runs_again(
    tables: None, waits: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A race lost as the batch commits runs the batch again; without retry, 503."""
    one = make_gadget("one", stock=5)
    await store(one)
    runs: list[str] = []
    commit = GadgetsUnitOfWork._commit
    lose = losing(1, conflict)

    async def committing(self: GadgetsUnitOfWork) -> None:
        lose()
        await commit(self)

    items = {"items": [{"gadget_id": str(one.id)}], "atomic": True}
    monkeypatch.setattr(GadgetsUnitOfWork, "_commit", committing)
    async with AsyncTestClient(
        batch_app(taking(lambda: None, runs), retry=NOW)
    ) as client:
        answer = await client.post("/gadgets/batch", json=items)
    lose = losing(1, conflict)
    async with AsyncTestClient(
        batch_app(taking(lambda: None, runs), retry=None)
    ) as client:
        busy = await client.post("/gadgets/batch", json=items)

    assert (answer.status_code, answer.json()["results"][0]["status"]) == (207, 200)
    assert (busy.status_code, busy.json()["code"]) == (503, "database_busy")
    assert (runs, waits) == (["one", "one", "one"], [0.0])
    assert await total_stock() == 4


@pytest.mark.parametrize("atomic", [False, True])
async def test_a_batch_that_keeps_losing_answers_as_one_that_does_not_retry(
    tables: None, waits: list[float], atomic: bool
) -> None:
    """Once ``attempts`` runs lost, the item answers 503; refusals never run again."""
    one, two = make_gadget("one", stock=5), make_gadget("two", stock=5)
    await store(one, two)
    runs: list[str] = []
    retry = Retry(attempts=2, backoff=0)
    app = batch_app(taking(losing(9, conflict), runs), retry=retry)
    items = [
        {"gadget_id": str(one.id)},
        {"gadget_id": str(two.id)},
        {"gadget_id": str(uuid4())},
    ]

    async with AsyncTestClient(app) as client:
        answer = await client.post(
            "/gadgets/batch", json={"items": items, "atomic": atomic}
        )
    results = answer.json()["results"]

    assert [each["status"] for each in results] == (
        [424, 503, 424] if atomic else [200, 503, 404]
    )
    assert results[1]["error"]["code"] == "database_busy"
    assert runs == (["one", "two", "one", "two"] if atomic else ["one", "two", "two"])
    assert waits == [0.0]
    assert await total_stock() == (10 if atomic else 9)


async def test_a_batch_runs_each_items_checks_again_with_it(
    tables: None, waits: list[float]
) -> None:
    """The reach check and the item's ``If-Match`` are made again in each run."""
    one = make_gadget("one", stock=5)
    two = make_gadget("two", stock=5)
    await store(one, two)
    checks: list[str] = []

    async def scope(request: HTTPConnection) -> Any:  # noqa: ANN401 - criteria.
        checks.append("scope")
        return F("active") == True  # noqa: E712 - a condition, not a test.

    resource = ResourceRouter(
        "/gadgets",
        model=Gadget,
        unit_of_work=GadgetsUnitOfWork,
        repository=lambda uow: uow.gadgets,
        dependencies=[Public],
        scope=scope,
    )
    runs: list[str] = []
    resource.batch_route(Take, service=taking(losing(1, conflict), runs), retry=NOW)
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(resource)
    app.add_middleware(RequestContextMiddleware)
    async with AsyncTestClient(app) as client:
        tag = (await client.get(f"/gadgets/{two.id}")).headers["etag"]
        checks.clear()
        answer = await client.post(
            "/gadgets/batch",
            json={"items": [{"gadget_id": str(two.id), "if_match": tag}]},
        )
        stale = await client.post(
            "/gadgets/batch",
            json={"items": [{"gadget_id": str(two.id), "if_match": tag}]},
        )

    assert [each["status"] for each in answer.json()["results"]] == [200]
    assert [each["status"] for each in stale.json()["results"]] == [412]
    assert checks == ["scope", "scope", "scope"]
    assert runs == ["two", "two"]
