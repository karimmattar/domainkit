"""Ordering keys after their security review (SEC-14c): repairs and refusals.

A key left with held messages and none pending is released by cleanup and
by ``messages --release``; a dead letter given back goes before its key's
pending message while no one has started it; a task's key must name one
of its arguments; a key no outbox can store is refused where it is given;
and operator queries judge leases by the earlier of two clocks.
"""

import asyncio
import io
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.dialects import postgresql

from domainkit.apps import registry
from domainkit.clock import freeze_time
from domainkit.contrib.messaging import (
    MessagingSettings,
    Worker,
    admin,
    dbclock,
    enqueue,
    ordered,
    ordering,
    periodic,
    task,
)
from domainkit.contrib.messaging.tables import DEAD, DONE, HELD, PENDING, messages
from domainkit.db import connections
from domainkit.exceptions import ImproperlyConfigured
from domainkit.management import call_command
from domainkit_tests.test_db import NotesUnitOfWork
from domainkit_tests.test_messaging import (  # noqa: F401 - fixtures.
    immediate_retries,
    messaging,
    tables,
)
from domainkit_tests.test_messaging_ordering import (
    config,
    doomed,
    plain,
    rows,
    runs,
    statuses,
    step,
)

pytestmark = pytest.mark.anyio


async def finished_unpromoted(key: str) -> None:
    """Record ``key``'s pending message done, promoting nothing, as 0.13 did."""
    async with connections.engine().begin() as connection:
        await connection.execute(
            update(messages)
            .where(messages.c.ordering_key == key, messages.c.status == PENDING)
            .values(status=DONE, finished_at=datetime.now(UTC), payload={})
        )


@pytest.fixture(autouse=True)
def fresh_runs() -> None:
    """Each test starts with no runs."""
    runs.clear()


# SEC-14c-3: stalled keys are released.


@pytest.mark.usefixtures("tables")
async def test_stalled_keys_are_released_in_batches_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two stalled keys, batches of one: both get their next message, logged.

    A key whose first message still waits is left as it is.
    """
    for key in ("a", "b", "c"):
        for number in range(2):
            await enqueue(step, key=key, label=f"{key}{number}")
    await finished_unpromoted("a")
    await finished_unpromoted("b")
    worker = Worker(config(CLEANUP_BATCH_SIZE=1))
    relayed: list[uuid.UUID] = []

    async def relay(promoted: ordering.Promoted) -> None:
        relayed.append(promoted.id)

    worker.relay_promoted = relay
    stopped = asyncio.Event()
    stopped.set()
    assert await worker._release_stalled(stopped) == 0

    with caplog.at_level(logging.WARNING, "domainkit.contrib.messaging.worker"):
        assert await worker.release_stalled() == 2

    assert await statuses("a") == [DONE, PENDING]
    assert await statuses("b") == [DONE, PENDING]
    assert await statuses("c") == [PENDING, HELD]
    found = {row["payload"].get("label"): row["id"] for row in await rows()}
    assert caplog.messages == [
        f"Made message {found[label]} pending: its ordering key had held messages "
        "and none pending (a worker before 0.14 finished the one before?)"
        for label in ("a1", "b1")
    ]
    assert relayed == [found["a1"], found["b1"]]  # Started at once, as promoted.
    assert await worker.release_stalled() == 0
    await worker.run_once()
    assert runs.labels("a") == ["a1"]
    assert runs.labels("c") == ["c0", "c1"]


@pytest.mark.usefixtures("tables")
async def test_a_key_that_moved_on_meanwhile_is_left_as_it_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Found stalled, a key whose next is pending under its turn: nothing changes.

    And the round ends there, rather than finding the same key again.
    """
    for number in range(2):
        await enqueue(step, key="a", label=f"a{number}")
    found: list[int] = []

    async def stale(connection: object, size: int) -> list[str]:
        found.append(size)
        return ["a"] * size  # A full batch, as if more were waiting.

    monkeypatch.setattr(ordering, "stalled_keys", stale)
    assert await Worker(config(CLEANUP_BATCH_SIZE=2)).release_stalled() == 0
    assert found == [2]
    assert await statuses("a") == [PENDING, HELD]


@pytest.mark.usefixtures("tables")
async def test_pruning_keys_releases_stalled_ones_first() -> None:
    """``prune_keys``, which every cleanup round runs, repairs a stalled key."""
    for number in range(3):
        await enqueue(step, key="a", label=f"a{number}")
    await finished_unpromoted("a")
    worker = Worker(config())

    await worker.prune_keys()
    await worker.run_once()

    assert runs.labels("a") == ["a1", "a2"]


@pytest.mark.usefixtures("messaging")
def test_messages_release_repairs_stalled_keys_and_says_how_many() -> None:
    """``messages --release`` does at once what cleanup does each round."""

    async def prepare() -> None:
        async with connections.engine().begin() as connection:
            await connection.run_sync(messages.create)
            from domainkit.contrib.messaging.tables import ordering_keys

            await connection.run_sync(ordering_keys.create)
        for number in range(2):
            await enqueue(step, key="a", label=f"a{number}")
        await finished_unpromoted("a")
        await connections.close_all()

    asyncio.run(prepare())
    first, again = io.StringIO(), io.StringIO()
    call_command("messages", "--release", stdout=first)
    call_command("messages", "--release", stdout=again)

    assert first.getvalue() == "Released 1 stalled key(s).\n"
    assert again.getvalue() == "Released 0 stalled key(s).\n"


@pytest.mark.usefixtures("tables")
async def test_admin_releases_stalled_keys_of_an_outbox() -> None:
    """``admin.release_stalled`` is the command's action, for tools of your own."""
    for number in range(2):
        await enqueue(step, key="a", label=f"a{number}")
    await finished_unpromoted("a")
    settings = registry.settings(MessagingSettings)

    assert await admin.release_stalled(settings, "default") == 1
    assert await statuses("a") == [DONE, PENDING]


# SEC-14c-1: a dead letter given back goes before what has not started.


async def dead_then_next(*, lock: timedelta | None, started: bool) -> None:
    """``a1`` dies; ``a2``, pending, gets the lock and start given.

    Key ``z`` has a pending message, not started, later in its own line
    than ``a1`` in ``a``'s: giving ``a1`` back never touches it.
    """
    for label in ("z0", "z1"):
        await enqueue(step, key="z", label=label)
    async with connections.engine().begin() as connection:
        await connection.execute(
            update(messages)
            .where(messages.c.sequence == 1, messages.c.ordering_key == "z")
            .values(status=DONE)
        )
        await connection.execute(
            update(messages)
            .where(messages.c.ordering_key == "z", messages.c.status == HELD)
            .values(status=PENDING, available_at=datetime.now(UTC) + timedelta(1))
        )
    runs.broken.add("a1")
    await enqueue(doomed, key="a", label="a1")
    await enqueue(step, key="a", label="a2")
    worker = Worker(config())
    for _ in range(2):
        (claimed,) = await worker.claim(1)
        await worker.process(claimed)
    runs.broken.clear()
    if lock is not None or started:
        now = datetime.now(UTC)
        async with connections.engine().begin() as connection:
            await connection.execute(
                update(messages)
                .where(messages.c.status == PENDING, messages.c.ordering_key == "a")
                .values(
                    locked_until=None if lock is None else now + lock,
                    lock_id=uuid.uuid4(),
                    started_at=now if started else None,
                )
            )


@pytest.mark.usefixtures("tables", "immediate_retries")
async def test_a_dead_letter_waits_for_a_message_handed_to_the_broker() -> None:
    """A relay holds ``a2`` for the broker (a live lock, not started): it goes first."""
    await dead_then_next(lock=timedelta(minutes=5), started=False)

    assert await admin.retry_dead(registry.settings(MessagingSettings), None) == 1
    assert await statuses("a") == [HELD, PENDING]


@pytest.mark.usefixtures("tables", "immediate_retries")
async def test_a_dead_letter_goes_before_a_message_whose_lock_ran_out() -> None:
    """``a2``'s relay lock expired: it is held again, unlocked, behind ``a1``."""
    await dead_then_next(lock=timedelta(minutes=-5), started=False)

    assert await admin.retry_dead(registry.settings(MessagingSettings), None) == 1
    found = {row["payload"]["label"]: row for row in await rows()}
    assert (found["a1"]["status"], found["a2"]["status"]) == (PENDING, HELD)
    assert (found["a2"]["locked_until"], found["a2"]["lock_id"]) == (None, None)
    assert await statuses("z") == [DONE, PENDING]  # Another key: untouched.
    await Worker(config()).run_once()
    assert runs.labels("a") == ["a1", "a2"]


@pytest.mark.usefixtures("tables", "immediate_retries")
async def test_a_dead_letter_waits_for_a_started_message() -> None:
    """``a2`` started (a worker died on it, its lock gone): it keeps its turn."""
    await dead_then_next(lock=timedelta(minutes=-5), started=True)

    assert await admin.retry_dead(registry.settings(MessagingSettings), None) == 1
    assert await statuses("a") == [HELD, PENDING]


@pytest.mark.usefixtures("tables", "immediate_retries")
async def test_only_the_older_dead_letter_passes_the_pending_message() -> None:
    """``a1`` and ``a3`` die around ``a2``: ``a1`` goes first, ``a3`` after ``a2``."""
    runs.broken.update({"a1", "a3"})
    await enqueue(doomed, key="a", label="a1")
    await enqueue(step, key="a", label="a2", fail=1)
    await enqueue(doomed, key="a", label="a3")
    worker = Worker(config())
    for _ in range(2):
        (claimed,) = await worker.claim(1)
        await worker.process(claimed)  # a1 dies.
    (a2,) = await worker.claim(1)
    with pytest.MonkeyPatch.context() as later:
        from domainkit.contrib.messaging import worker as worker_module

        later.setattr(worker_module, "backoff", lambda attempts, config: 3600.0)
        await worker.process(a2)  # a2 fails, retried in an hour: waiting.
    assert await statuses("a") == [DEAD, PENDING, HELD]
    async with connections.engine().begin() as connection:  # a3 dies too.
        await connection.execute(
            update(messages).where(messages.c.status == HELD).values(status=DEAD)
        )
    runs.broken.clear()

    assert await admin.retry_dead(registry.settings(MessagingSettings), None) == 2
    assert await statuses("a") == [PENDING, HELD, HELD]


# SEC-14c-2: a task's key names one of its arguments.


def test_a_task_key_must_name_one_of_its_arguments() -> None:
    """Refused before or after ``@task``; ``**kwargs`` takes any name."""
    with pytest.raises(
        ImproperlyConfigured,
        match=(
            r"^Task 'tests.repairs.typo_after' is ordered by 'acount', which is "
            r"not one of its arguments: its messages would have no key\.$"
        ),
    ):

        @ordered("acount")
        @task(name="tests.repairs.typo_after")
        async def typo_after(account: str) -> None:
            """Marked after it was registered."""

    with pytest.raises(ImproperlyConfigured, match=r"Task 'tests.repairs.only'"):

        @task(name="tests.repairs.only")
        @ordered("account")
        async def positional_only(account: str, /) -> None:
            """Tasks take keyword arguments: this one never gets ``account``."""

    with pytest.raises(ImproperlyConfigured, match="is not one of its arguments"):

        @periodic(every=timedelta(hours=1), name="tests.repairs.hourly")
        @ordered("account")
        async def hourly() -> None:
            """No arguments at all."""

    @task(name="tests.repairs.any")
    @ordered("account")
    async def takes_any(**arguments: str) -> None:
        """May be given ``account``."""

    @ordered(lambda **arguments: "k")
    @task(name="tests.repairs.function")
    async def by_function(account: str) -> None:
        """A function is not checked: it may read any argument."""


# A key no outbox can store is refused where it is given.


@pytest.mark.usefixtures("tables")
async def test_a_key_no_outbox_can_store_is_refused_where_it_is_given() -> None:
    """Too long, or holding NUL: ``defer`` and ``enqueue`` refuse it at once."""
    async with NotesUnitOfWork() as uow:
        with pytest.raises(ValueError, match="1 to 255 characters, not 256"):
            uow.defer(plain, label="x", ordering_key="k" * 256)
        with pytest.raises(ValueError, match=r"^An ordering key cannot hold a NUL"):
            uow.defer(plain, label="x", ordering_key="a\x00b")
        with pytest.raises(ValueError, match="is a string, not int"):
            uow.defer(plain, label="x", ordering_key=42)  # type: ignore[arg-type]
        uow.defer(plain, label="kept", ordering_key="k" * 255)
        await uow.commit()
    with pytest.raises(ValueError, match="cannot hold a NUL character"):
        await enqueue(step, key="a\x00b", label="derived")

    assert [row["payload"]["label"] for row in await rows()] == ["kept"]


# The dead letters' listing, and the operators' clock.


@pytest.mark.usefixtures("messaging")
def test_a_dead_letter_without_an_error_is_listed() -> None:
    """``last_error`` is NULL (written by hand, or by an older release)."""

    async def prepare() -> uuid.UUID:
        async with connections.engine().begin() as connection:
            await connection.run_sync(messages.create)
        await enqueue(plain, label="x")
        async with connections.engine().begin() as connection:
            await connection.execute(
                update(messages).values(status=DEAD, attempts=1, last_error=None)
            )
        (row,) = await rows()
        await connections.close_all()
        return uuid.UUID(str(row["id"]))

    dead = asyncio.run(prepare())
    listed = io.StringIO()
    call_command("messages", "--dead", stdout=listed)

    assert listed.getvalue() == f"{dead}  task tests.ordering.plain  x1  \n"


def test_leases_are_judged_by_the_earlier_clock_on_postgresql() -> None:
    """SQLite has one clock; PostgreSQL's queries take the least of both."""
    assert isinstance(admin.lease_now("sqlite"), datetime)
    moment = admin.lease_now("postgresql")
    compiled = str(moment.compile())  # type: ignore[union-attr]
    assert compiled.startswith("least(")
    assert "statement_timestamp()" in compiled
    assert dbclock.now("sqlite") <= admin.lease_now("sqlite")


# What the repairs read: statuses written into the SQL, keys stalled, turns kept.


def _sql(statement: Any) -> tuple[str, dict[str, Any]]:  # noqa: ANN401 - a statement.
    """``statement`` as PostgreSQL runs it, and the parameters it still takes."""
    dialect = postgresql.dialect()  # type: ignore[no-untyped-call]
    compiled = statement.compile(
        dialect=dialect, compile_kwargs={"render_postcompile": True}
    )
    return " ".join(str(compiled).split()), dict(compiled.params)


def test_statuses_and_sizes_are_written_into_the_sql() -> None:
    """A prepared statement still reads ``ORDERING_INDEX``: they imply its condition.

    The key and the time remain parameters; the statuses and the batch's
    size are text of the statement.
    """
    stalled, none = _sql(ordering.stalled(7))
    idle, timed = _sql(ordering.idle_keys("postgresql", 7, 60.0))
    first, keyed = _sql(select(messages.c.id).where(ordering.busy("k")))

    assert none == {}
    assert "domainkit_messages.status IN ('held')" in stalled
    assert ".status IN ('pending')" in stalled  # Of the key's own messages.
    assert stalled.endswith("ORDER BY domainkit_messages.ordering_key LIMIT 7")
    assert list(timed) == ["statement_timestamp_1"]
    assert "status IN ('pending', 'held', 'dead')" in idle
    assert idle.endswith("LIMIT 7 FOR UPDATE SKIP LOCKED)")
    assert "status IN ('pending', 'held') ORDER BY" in first
    assert sorted(keyed.values(), key=str) == [1, "k"]  # The key, and LIMIT 1.


@pytest.mark.usefixtures("tables")
async def test_a_key_is_stalled_with_held_messages_and_none_pending() -> None:
    """Its own pending message, not another key's, keeps it from being stalled."""
    for key in ("b", "a", "c"):
        for number in range(2):
            await enqueue(step, key=key, label=f"{key}{number}")
    await finished_unpromoted("b")
    await finished_unpromoted("a")

    async with connections.engine().connect() as connection:
        found = await ordering.stalled_keys(connection, 10)
        first = await ordering.stalled_keys(connection, 1)

    assert (found, first) == (["a", "b"], ["a"])


@pytest.mark.usefixtures("tables", "immediate_retries")
async def test_only_a_dead_letter_takes_a_place_back() -> None:
    """A finished message before it is not given back, nor does it hold anyone."""
    runs.broken.add("a1")
    await enqueue(step, key="a", label="a0")
    await enqueue(doomed, key="a", label="a1")
    await enqueue(step, key="a", label="a2")
    worker = Worker(config())
    for _ in range(3):
        (claimed,) = await worker.claim(1)
        await worker.process(claimed)  # a0 is done; a1 dies, twice.
    assert await statuses("a") == [DONE, DEAD, PENDING]
    runs.broken.clear()

    assert await admin.retry_dead(registry.settings(MessagingSettings), None) == 1
    assert await statuses("a") == [DONE, PENDING, HELD]


@pytest.mark.usefixtures("tables", "immediate_retries")
async def test_a_dead_letter_waits_for_a_lock_that_ends_this_instant() -> None:
    """A lock is alive to its last instant, as ``locked`` judges it."""
    await dead_then_next(lock=None, started=False)
    moment = datetime(2030, 1, 1, tzinfo=UTC)
    async with connections.engine().begin() as connection:
        await connection.execute(
            update(messages)
            .where(messages.c.status == PENDING, messages.c.ordering_key == "a")
            .values(locked_until=moment, lock_id=uuid.uuid4())
        )

    with freeze_time(moment):
        assert await admin.retry_dead(registry.settings(MessagingSettings), None) == 1
    assert await statuses("a") == [HELD, PENDING]


@pytest.mark.usefixtures("tables")
async def test_a_stopped_round_of_pruning_releases_no_stalled_key() -> None:
    """``prune_keys`` stops before the stalled keys when the worker stops."""
    for number in range(2):
        await enqueue(step, key="a", label=f"a{number}")
    await finished_unpromoted("a")
    stopped = asyncio.Event()
    stopped.set()

    assert await Worker(config())._prune_keys(stopped) == 0
    assert await statuses("a") == [DONE, HELD]


@pytest.mark.usefixtures("tables")
async def test_the_next_message_of_a_key_starts_without_a_word(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A worker with no relay to hand it to only wakes its own loop."""
    for number in range(2):
        await enqueue(step, key="a", label=f"a{number}")
    worker = Worker(config())

    with caplog.at_level(logging.WARNING, "domainkit.contrib.messaging.worker"):
        (claimed,) = await worker.claim(1)
        await worker.process(claimed)

    assert await statuses("a") == [DONE, PENDING]
    assert caplog.messages == []


@pytest.mark.usefixtures("tables", "immediate_retries")
async def test_a_retry_takes_the_turns_of_its_dead_letters_keys_only() -> None:
    """Another key, with nothing dead, is not touched: its idle time runs on."""
    from domainkit.contrib.messaging.tables import ordering_keys

    early, later = datetime(2030, 1, 1, tzinfo=UTC), datetime(2030, 1, 2, tzinfo=UTC)
    with freeze_time(early):
        await enqueue(step, key="b", label="b0")
        await enqueue(doomed, key="a", label="a0")
    async with connections.engine().begin() as connection:
        await connection.execute(
            update(messages).where(messages.c.ordering_key == "a").values(status=DEAD)
        )

    with freeze_time(later):
        assert await admin.retry_dead(registry.settings(MessagingSettings), None) == 1

    async with connections.engine().connect() as connection:
        touched = dict(
            (
                await connection.execute(
                    select(ordering_keys.c.key, ordering_keys.c.touched_at)
                )
            ).all()
        )
    assert touched == {"a": later, "b": early}
