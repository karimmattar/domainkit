"""Ordering keys: a key's messages run one at a time, in commit order (FIN-19).

Messages with one ordering key run one after another, in the order their
commits happened, across every worker sharing the outbox; other keys run
beside them. Only the first unfinished message of a key is ``pending``:
the claim, which reads the same index as before, never sees the rest.
"""

import asyncio
import io
import logging
import random
import uuid
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import UNSET, override_settings
from domainkit.contrib.messaging import (
    MessagingSettings,
    Worker,
    admin,
    chain,
    current_attempt,
    dbclock,
    enqueue,
    ordered,
    task,
)
from domainkit.contrib.messaging import ordering as ordering_module
from domainkit.contrib.messaging import registry as registry_module
from domainkit.contrib.messaging import worker as worker_module
from domainkit.contrib.messaging.budget import Budget
from domainkit.contrib.messaging.dispatch import BrokerWorkers
from domainkit.contrib.messaging.ordering import Promoted
from domainkit.contrib.messaging.outbox import message_row
from domainkit.contrib.messaging.tables import (
    CANCELLED,
    DEAD,
    DONE,
    HELD,
    ORDERING_INDEX,
    PENDING,
    messages,
    ordering_keys,
)
from domainkit.contrib.messaging.testing import Outbox, QueuedMessage
from domainkit.db import Isolation, connections
from domainkit.ddd import Retry, event_bus
from domainkit.exceptions import ImproperlyConfigured
from domainkit.management import call_command
from domainkit.testing import freeze_time
from domainkit_tests.test_db import Note, NoteId, NotesUnitOfWork, NoteWritten, notes
from domainkit_tests.test_messaging import (  # noqa: F401 - fixtures.
    ALPHA,
    BROKERED,
    FAST,
    MESSAGING,
    SLOW,
    broker_tables,
    brokered,
    immediate_retries,
    messaging,
    tables,
)
from domainkit_tests.test_messaging_inbox import expire_locks

pytestmark = pytest.mark.anyio

WAIT: Final = 30.0
"""The longest a test waits for its workers; a regression fails, never hangs."""


class Runs:
    """What the ordered tasks did: starts, finishes, and overlaps per key."""

    def __init__(self) -> None:
        """Nothing yet."""
        self.started: list[tuple[str, str]] = []
        self.finished: list[tuple[str, str]] = []
        self.active: Counter[str] = Counter()
        self.overlaps: list[tuple[str, str]] = []
        self.running = 0
        self.peak = 0
        self.broken: set[str] = set()

    def clear(self) -> None:
        """Forget everything."""
        self.__init__()  # type: ignore[misc]

    def begin(self, key: str, label: str) -> None:
        """Note a start; a second message of ``key`` running is an overlap."""
        self.started.append((key, label))
        self.active[key] += 1
        if self.active[key] > 1:
            self.overlaps.append((key, label))
        self.running += 1
        self.peak = max(self.peak, self.running)

    def end(self, key: str, label: str, *, ok: bool) -> None:
        """Note an end (a finish, if ``ok``)."""
        self.active[key] -= 1
        self.running -= 1
        if ok:
            self.finished.append((key, label))

    def labels(self, key: str, of: str = "finished") -> list[str]:
        """The labels of ``key``'s runs, started or finished, in order."""
        return [label for found, label in getattr(self, of) if found == key]


runs = Runs()


@task(name="tests.ordering.step", max_attempts=5)
@ordered("key")
async def step(key: str, label: str, pause: float = 0.0, fail: int = 0) -> None:
    """Run under ``key``; pause, and fail the first ``fail`` attempts."""
    attempt = current_attempt()
    assert attempt is not None
    runs.begin(key, label)
    ok = False
    try:
        await asyncio.sleep(pause)
        if attempt.number <= fail or label in runs.broken:
            raise RuntimeError(f"{label} fails (attempt {attempt.number})")
        ok = True
    finally:
        runs.end(key, label, ok=ok)


@task(name="tests.ordering.doomed", max_attempts=2)
@ordered("key")
async def doomed(key: str, label: str) -> None:
    """Fail while ``label`` is broken; run otherwise."""
    await step(key=key, label=label)


@task(name="tests.ordering.plain")
async def plain(label: str) -> None:
    """A task that declares no key."""
    runs.begin(label, label)
    runs.end(label, label, ok=True)


@ordered(lambda key, **_: f"by-function:{key}")
@task(name="tests.ordering.by_function")
async def by_function(key: str, label: str) -> dict[str, str]:
    """Derive the key with a function; hand the arguments on in a chain."""
    await step(key=key, label=label)
    return {"key": key, "label": f"{label}+next"}


@ordered(lambda event: f"note:{event.note_id}")
async def note_written(event: NoteWritten) -> None:
    """A durable handler whose key is the note's id."""
    runs.begin(f"note:{event.note_id}", str(event.event_id))
    runs.end(f"note:{event.note_id}", str(event.event_id), ok=True)


class Recorder:
    """A durable handler that is a method (marked through its function)."""

    @ordered("note_id")
    async def handle(self, event: NoteWritten) -> None:
        """Run under the note's id."""
        runs.begin(str(event.note_id), "method")
        runs.end(str(event.note_id), "method", ok=True)


@pytest.fixture(autouse=True)
def fresh_runs() -> Iterator[None]:
    """Each test starts with no runs."""
    runs.clear()
    yield
    runs.clear()


async def rows() -> list[dict[str, Any]]:
    """Every outbox row, in key order, then sequence."""
    async with connections.engine().connect() as connection:
        found = await connection.execute(
            select(messages).order_by(
                messages.c.ordering_key.nulls_first(),
                messages.c.sequence,
                messages.c.created_at,
            )
        )
        return [dict(row._mapping) for row in found]


async def last_sequence(key: str) -> int:
    """The sequence ``key``'s row has given out last."""
    async with connections.engine().connect() as connection:
        found = await connection.scalar(
            select(ordering_keys.c.sequence).where(ordering_keys.c.key == key)
        )
    assert found is not None
    return int(found)


async def statuses(key: str) -> list[str]:
    """The statuses of ``key``'s messages, in their order."""
    return [row["status"] for row in await rows() if row["ordering_key"] == key]


def config(**options: object) -> MessagingSettings:
    """``FAST`` with ``options``."""
    return MessagingSettings(**{**FAST, **options})


async def drain(worker: Worker) -> int:
    """Run ``worker`` until nothing is due; return how many it ran."""
    return await worker.run_once()


async def until_done(count: int, within: float = WAIT) -> None:
    """Return once ``count`` messages are done; fail after ``within`` seconds."""
    async with asyncio.timeout(within):
        for _ in range(int(within / 0.02)):
            if sum(row["status"] == DONE for row in await rows()) >= count:
                return
            await asyncio.sleep(0.02)
    raise AssertionError(f"{count} messages were never done")


# Writing: keys, sequences, and who is first.


@pytest.mark.usefixtures("tables")
async def test_a_keys_first_message_is_pending_and_the_rest_are_held() -> None:
    """Each key's line: one pending, then held ones, numbered as they committed."""
    for label in ("a0", "a1", "a2"):
        await enqueue(step, key="a", label=label)
    await enqueue(step, key="b", label="b0")
    await enqueue(plain, label="free")

    found = await rows()
    lines = [(r["ordering_key"], r["sequence"], r["status"]) for r in found]
    assert lines == [
        (None, None, PENDING),
        ("a", 1, PENDING),
        ("a", 2, HELD),
        ("a", 3, HELD),
        ("b", 1, PENDING),
    ]
    async with connections.engine().connect() as connection:
        keys = (await connection.execute(select(ordering_keys))).all()
    assert sorted((key, sequence) for key, sequence, _ in keys) == [("a", 3), ("b", 1)]
    await Worker(config()).run_once()
    assert await last_sequence("a") == 3  # Promotions take no new place.


@pytest.mark.usefixtures("tables")
async def test_keys_are_given_declared_or_derived() -> None:
    """``ordering_key=`` wins; a name or a function derives one; None leaves none."""
    async with NotesUnitOfWork() as uow:
        uow.defer(step, key="declared", label="x")
        uow.defer(step, key="declared", label="y", ordering_key="given")
        uow.defer(plain, label="z", ordering_key="given")
        uow.defer(plain, label="none")
        uow.defer(by_function, key="k", label="f")
        uow.defer(step, key=uuid.UUID(int=7), label="uuid")
        uow.defer(plain, label="once", dedupe_key="once")
        await uow.commit()
    async with NotesUnitOfWork() as uow:
        uow.defer(plain, label="twice", dedupe_key="once")  # Skipped.
        await uow.commit()

    keys = {r["payload"]["label"]: r["ordering_key"] for r in await rows()}
    assert keys == {
        "x": "declared",
        "y": "given",
        "z": "given",
        "none": None,
        "f": "by-function:k",
        "uuid": str(uuid.UUID(int=7)),
        "once": None,
    }
    assert await statuses("given") == [PENDING, HELD]
    given = [row["sequence"] for row in await rows() if row["ordering_key"] == "given"]
    assert given == [1, 2]  # One commit, one turn, two places.


@pytest.mark.usefixtures("tables")
async def test_a_key_must_fit_its_column() -> None:
    """Empty keys and keys over 255 characters are refused, the commit too."""
    with pytest.raises(ValueError, match="1 to 255 characters, not 0"):
        await enqueue(plain, label="x", ordering_key="")
    with pytest.raises(ValueError, match="1 to 255 characters, not 256"):
        await enqueue(step, key="k" * 256, label="x")
    assert await rows() == []


def test_keys_of_one_to_255_characters_are_kept_as_they_are() -> None:
    """The bounds are the column's; a value that is not a string becomes one."""
    assert registry_module.checked_key("k") == "k"
    assert registry_module.checked_key("k" * 255) == "k" * 255
    assert registry_module.checked_key(42) == "42"
    assert registry_module.checked_key(None) is None
    rows = [{"ordering_key": key} for key in ("a", None, "b", "a")]
    assert ordering_module.counts_of(rows) == {"a": 2, "b": 1}


def test_keys_need_postgresql_or_sqlite() -> None:
    """A key's turn is an upsert, which only those two are written for."""
    with pytest.raises(ImproperlyConfigured, match="PostgreSQL or SQLite, not mysql"):
        ordering_module._turn("mysql", "k", 1)


def test_a_key_source_is_a_name_or_a_function() -> None:
    """``ordered`` takes an identifier or a callable, nothing else."""
    for wrong in ("not a name", "", 3):
        with pytest.raises(
            ImproperlyConfigured,
            match=(
                r"^An ordering key is the name of an argument or attribute, or "
                r"a function that returns the key, not "
            ),
        ):
            ordered(wrong)  # type: ignore[arg-type]
    assert not registry_module.task_key(plain, {"label": "x"})


def test_ordered_marks_functions_and_the_function_of_a_method() -> None:
    """A name or a function, on a function or a bound method: the key is found."""

    class Handlers:
        async def by_name(self, event: NoteWritten) -> None:
            """Keyed by an attribute."""

        async def by_function(self, event: NoteWritten) -> None:
            """Keyed by a function."""

    async def task_function(account: str) -> None:
        """Keyed by an argument."""

    event = NoteWritten(note_id=uuid.UUID(int=5))
    marked = ordered("note_id")(Handlers().by_name)
    ordered(lambda event: f"n{event.note_id.int}")(Handlers().by_function)
    ordered(lambda account: account.upper())(task_function)

    assert registry_module.event_key(marked, event) == str(uuid.UUID(int=5))
    assert registry_module.event_key(Handlers().by_name, event) == str(event.note_id)
    assert registry_module.event_key(Handlers().by_function, event) == "n5"
    assert registry_module.task_key(task_function, {"account": "ab"}) == "AB"


@pytest.mark.usefixtures("tables")
async def test_durable_handlers_derive_their_key_from_the_event() -> None:
    """A function of the event, or an attribute; methods too; others have none."""
    recorder = Recorder()

    async def unordered(event: NoteWritten) -> None:
        """No key."""

    note_id = uuid.uuid4()
    with event_bus.subscriptions_kept():
        event_bus.subscribe(NoteWritten, note_written, durable=True)
        event_bus.subscribe(NoteWritten, recorder.handle, durable=True)
        event_bus.subscribe(NoteWritten, unordered, durable=True)
        async with NotesUnitOfWork() as uow:
            note = Note(id=NoteId(note_id), text="t")
            note.record(NoteWritten(note_id=note.id))
            note.record(NoteWritten(note_id=note.id))
            await uow.notes.add(note)
            await uow.commit()
        keys = sorted(
            (r["target"].rsplit(".", 1)[-1], r["ordering_key"] or "", r["status"])
            for r in await rows()
        )
        assert keys == [
            ("handle", str(note_id), HELD),
            ("handle", str(note_id), PENDING),
            ("note_written", f"note:{note_id}", HELD),
            ("note_written", f"note:{note_id}", PENDING),
            ("unordered", "", PENDING),
            ("unordered", "", PENDING),
        ]
        assert await Worker(config()).run_once() == 6
    assert runs.labels(str(note_id)) == ["method", "method"]
    assert not runs.overlaps


def test_the_outbox_fixture_keeps_each_messages_key() -> None:
    """``QueuedMessage`` has every column a test asserts on, the key included."""
    seen = Outbox()
    row = message_row(
        kind="task",
        target="t",
        payload={"a": 1},
        headers={},
        max_attempts=3,
        dedupe_key="d",
        ordering_key="k",
    )
    asyncio.run(seen.watch("default", [row]))

    assert seen.messages == [
        QueuedMessage(
            id=row["id"],
            database="default",
            kind="task",
            target="t",
            arguments={"a": 1},
            queue="default",
            priority=0,
            available_at=row["available_at"],
            dedupe_key="d",
            headers={},
            ordering_key="k",
        )
    ]


@pytest.mark.usefixtures("tables")
async def test_a_deduplicated_message_takes_no_place_in_its_key() -> None:
    """A message skipped for its ``dedupe_key`` leaves the next one first."""
    await enqueue(step, key="d", label="first", dedupe_key="once")
    await Worker(config()).run_once()
    await enqueue(step, key="d", label="again", dedupe_key="once")
    await enqueue(step, key="d", label="next")

    assert await statuses("d") == [DONE, PENDING]


# Running: one at a time, in order, across workers.


@pytest.mark.usefixtures("tables")
async def test_a_keys_messages_run_one_at_a_time_in_commit_order_across_workers() -> (
    None
):
    """Four workers, six keys interleaved: each key in order, never two at once.

    Messages of different keys run side by side; more are committed while
    the workers run. A message that finishes wakes its worker for the
    key's next, so no key waits for a poll.
    """
    keys = [f"k{number}" for number in range(6)]
    rng = random.Random(19)  # noqa: S311 - test data, not secrets.
    expected: dict[str, list[str]] = {key: [] for key in keys}

    async def commit(rounds: range) -> None:
        for number in rounds:
            for key in rng.sample(keys, len(keys)):
                label = f"{key}-{number}"
                expected[key].append(label)
                await enqueue(step, key=key, label=label, pause=rng.random() / 200)

    await commit(range(4))
    settings = config(POLL_INTERVAL=0.05, **SLOW)
    workers = [Worker(settings, budget=Budget(4)) for _ in range(4)]
    stop = asyncio.Event()
    running = [asyncio.create_task(worker.run(stop)) for worker in workers]
    try:
        await commit(range(4, 8))
        await until_done(len(keys) * 8)
    finally:
        stop.set()
        async with asyncio.timeout(WAIT):
            await asyncio.gather(*running)

    assert runs.overlaps == []
    for key in keys:
        assert runs.labels(key) == expected[key]
        assert runs.labels(key, "started") == expected[key]
    assert runs.peak > 1  # Keys ran side by side.
    assert {row["status"] for row in await rows()} == {DONE}


@pytest.mark.usefixtures("tables")
async def test_a_finished_message_wakes_its_worker_for_the_keys_next() -> None:
    """The worker polls every 30 seconds; a key of three runs in a moment."""
    for label in ("a0", "a1", "a2"):
        await enqueue(step, key="a", label=label)
    worker = Worker(config(POLL_INTERVAL=30, NOTIFY=False, **SLOW))
    stop = asyncio.Event()
    running = asyncio.create_task(worker.run(stop))
    try:
        await until_done(3, within=10)  # Without the wake-up, 30 s each.
    finally:
        stop.set()
        async with asyncio.timeout(WAIT):
            await running

    assert runs.labels("a") == ["a0", "a1", "a2"]


@pytest.mark.usefixtures("tables")
async def test_a_failing_message_holds_its_key_back_until_it_succeeds() -> None:
    """``a1`` fails twice: ``a2`` waits for it, while key ``b`` runs to the end."""
    await enqueue(step, key="a", label="a0")
    await enqueue(step, key="a", label="a1", fail=2)
    await enqueue(step, key="a", label="a2")
    for label in ("b0", "b1", "b2"):
        await enqueue(step, key="b", label=label)

    await Worker(config()).run_once()

    assert runs.labels("a", "started") == ["a0", "a1", "a1", "a1", "a2"]
    assert runs.labels("a") == ["a0", "a1", "a2"]
    assert runs.labels("b") == ["b0", "b1", "b2"]
    assert runs.finished.index(("b", "b2")) < runs.started.index(("a", "a2"))
    attempts = {row["payload"].get("label"): row["attempts"] for row in await rows()}
    assert attempts == {None: 1}  # Arguments are blanked once done.
    assert [row["attempts"] for row in await rows() if row["ordering_key"] == "a"] == [
        1,
        3,
        1,
    ]


@pytest.mark.usefixtures("tables")
async def test_a_dead_letter_lets_the_rest_of_its_key_run() -> None:
    """The key goes on after a message dies; given back, it runs next in its place.

    ``a1`` dies, so ``a2`` and ``a3`` run. Retried while ``a4`` runs and
    ``a5`` waits, ``a1`` is held, before ``a5``: it runs once ``a4``
    finishes, then ``a5``.
    """
    runs.broken.add("a1")
    await enqueue(step, key="a", label="a0")
    await enqueue(doomed, key="a", label="a1")
    await enqueue(step, key="a", label="a2")
    await enqueue(step, key="a", label="a3")
    worker = Worker(config())
    await worker.run_once()
    assert runs.labels("a") == ["a0", "a2", "a3"]
    assert await statuses("a") == [DONE, DEAD, DONE, DONE]

    await enqueue(step, key="a", label="a4")
    await enqueue(step, key="a", label="a5")
    (a4,) = await worker.claim(5)
    runs.broken.clear()
    settings = registry.settings(MessagingSettings)
    assert await admin.retry_dead(settings, None) == 1
    assert await statuses("a") == [DONE, HELD, DONE, DONE, PENDING, HELD]
    await worker.process(a4)
    await worker.run_once()

    assert runs.labels("a") == ["a0", "a2", "a3", "a4", "a1", "a5"]
    assert set(await statuses("a")) == {DONE}


@pytest.mark.usefixtures("tables")
async def test_dead_letters_given_back_to_an_idle_key_run_in_their_order() -> None:
    """Two dead letters of one key, retried together: the older runs first."""
    runs.broken.update({"x1", "x2"})
    for label in ("x1", "x2"):
        await enqueue(doomed, key="x", label=label)
    await enqueue(plain, label="free")
    worker = Worker(config())
    await worker.run_once()
    assert await statuses("x") == [DEAD, DEAD]
    runs.broken.clear()

    async with connections.engine().begin() as connection:  # x2 came first.
        for label, place in (("x1", 2), ("x2", 1)):
            await connection.execute(
                update(messages)
                .where(messages.c.payload["label"].as_string() == label)
                .values(sequence=place)
            )

    settings = registry.settings(MessagingSettings)
    assert await admin.retry_dead(settings, None) == 2
    assert await statuses("x") == [PENDING, HELD]
    await worker.run_once()
    assert runs.labels("x") == ["x2", "x1"]
    assert await last_sequence("x") == 2  # Giving back takes no new place.


@pytest.mark.usefixtures("tables")
async def test_each_key_of_a_retry_decides_by_its_own_messages() -> None:
    """Key ``a`` has a message running, key ``b`` none: ``a``'s is held, ``b``'s not."""
    runs.broken.update({"a-dead", "b-dead"})
    await enqueue(doomed, key="a", label="a-dead")
    await enqueue(doomed, key="b", label="b-dead")
    worker = Worker(config())
    await worker.run_once()
    await enqueue(step, key="a", label="a-later")
    (running,) = await worker.claim(5)
    runs.broken.clear()

    settings = registry.settings(MessagingSettings)
    assert await admin.retry_dead(settings, None) == 2
    assert await statuses("a") == [HELD, PENDING]
    assert await statuses("b") == [PENDING]
    await worker.process(running)
    await worker.run_once()
    assert runs.labels("a") == ["a-later", "a-dead"]
    assert runs.labels("b") == ["b-dead"]


@pytest.mark.usefixtures("tables")
async def test_a_worker_that_dies_mid_message_holds_its_key_until_its_lease_ends() -> (
    None
):
    """Its message stays locked: the key waits, other keys run; then it resumes.

    Once the lease ends, another worker runs the message again, first, and
    the rest of the key after it, in order.
    """
    for label in ("a0", "a1", "a2"):
        await enqueue(step, key="a", label=label)
    await enqueue(step, key="b", label="b0")
    dead = Worker(config(**SLOW))
    (a0,) = await dead.claim(1)  # Its process dies here: never finished.
    assert a0.ordering_key == "a"
    alive = Worker(config(**SLOW))
    assert await alive.run_once() == 1
    assert runs.labels("b") == ["b0"]
    assert runs.labels("a", "started") == []

    await expire_locks()
    assert await alive.run_once() == 3

    assert runs.labels("a") == ["a0", "a1", "a2"]
    a_rows = [row for row in await rows() if row["ordering_key"] == "a"]
    assert [row["attempts"] for row in a_rows] == [2, 1, 1]


@pytest.mark.usefixtures("tables")
async def test_a_retried_message_keeps_its_key_while_it_waits_for_its_time() -> None:
    """A retry due later, or a task over its rate, holds its key back."""
    await enqueue(step, key="a", label="a0", fail=1)
    await enqueue(step, key="a", label="a1")
    worker = Worker(config())
    with pytest.MonkeyPatch.context() as later:
        later.setattr(worker_module, "backoff", lambda attempts, config: 3600.0)
        await worker.run_once()
    assert await statuses("a") == [PENDING, HELD]
    assert runs.labels("a", "started") == ["a0"]


@pytest.mark.usefixtures("tables")
async def test_a_chains_steps_keep_its_key_and_its_place() -> None:
    """The next step is written with the key, after what the key committed meanwhile."""
    await enqueue(chain(by_function, step), key="c", label="c0")
    await enqueue(by_function, key="c", label="between")
    worker = Worker(config())
    (first,) = await worker.claim(5)
    await worker.process(first)
    found = [r for r in await rows() if r["ordering_key"] == "by-function:c"]
    assert [(r["payload"].get("label"), r["status"]) for r in found] == [
        (None, DONE),
        ("between", PENDING),
        ("c0+next", HELD),
    ]
    await worker.run_once()
    assert runs.labels("c") == ["c0", "between", "c0+next"]


@pytest.mark.usefixtures("tables")
async def test_cancelling_a_keys_first_message_lets_the_next_run() -> None:
    """A held message can be cancelled; cancelling the first runs the next.

    ``--held`` lists held messages by key, then in their order, which a
    sequence written by hand here tells apart from the order of insertion.
    """
    await enqueue(step, key="z", label="z0")  # Another key, written first.
    for label in ("a0", "a1", "a2"):
        await enqueue(step, key="a", label=label)
    settings = registry.settings(MessagingSettings)
    found = await rows()
    assert await admin.cancel(settings, found[1]["id"])  # Held.
    assert await statuses("a") == [PENDING, CANCELLED, HELD]
    held = await admin.list_messages(settings, "held", 10)
    assert [(row.id, row.ordering_key) for row in held] == [(found[2]["id"], "a")]
    for label in ("b0", "b1", "b2"):
        await enqueue(step, key="0b", label=label)
    async with connections.engine().begin() as connection:
        for label, place in (("b1", 3), ("b2", 2)):
            await connection.execute(
                update(messages)
                .where(messages.c.payload["label"].as_string() == label)
                .values(sequence=place)
            )
    listed = await admin.list_messages(settings, "held", 10)
    labels = {row["id"]: row["payload"].get("label") for row in await rows()}
    assert [(row.ordering_key, labels[row.id]) for row in listed] == [
        ("0b", "b2"),
        ("0b", "b1"),
        ("a", "a2"),
    ]
    assert await admin.cancel(settings, found[0]["id"])  # The first.
    assert await statuses("a") == [CANCELLED, CANCELLED, PENDING]
    assert await last_sequence("a") == 3  # Cancelling takes no new place.
    await Worker(config()).run_once()
    assert runs.labels("a") == ["a2"]


@pytest.mark.usefixtures("messaging")
def test_the_messages_command_counts_and_lists_held_messages() -> None:
    """``messages`` counts held messages; ``--held`` lists them with their key."""

    async def prepare() -> list[uuid.UUID]:
        async with connections.engine().begin() as connection:
            for table in (messages, ordering_keys):
                await connection.run_sync(table.create)
        for label in ("a0", "a1"):
            await enqueue(step, key="a", label=label)
        ids = [row["id"] for row in await rows()]
        await connections.close_all()
        return ids

    first, held = asyncio.run(prepare())
    counts = io.StringIO()
    call_command("messages", stdout=counts)
    listed = io.StringIO()
    call_command("messages", "--held", stdout=listed)
    pending = io.StringIO()
    call_command("messages", "--pending", stdout=pending)

    assert counts.getvalue().splitlines()[:2] == ["pending  1", "held     1"]
    assert listed.getvalue() == (
        f"{held}  task tests.ordering.step  queue=default key=a  x0/5\n"
    )
    assert [line.split()[0] for line in pending.getvalue().splitlines()] == [str(first)]


@pytest.mark.usefixtures("tables")
async def test_idle_keys_are_deleted_once_no_message_needs_them() -> None:
    """A key with a dead or waiting message stays; so does one written lately."""
    runs.broken.add("dead")
    await enqueue(doomed, key="dead", label="dead")
    await enqueue(step, key="waiting", label="w0", fail=99)
    await enqueue(step, key="done", label="d0")
    await enqueue(step, key="recent", label="r0")
    worker = Worker(config(CLEANUP_BATCH_SIZE=1))
    with pytest.MonkeyPatch.context() as later:
        later.setattr(worker_module, "backoff", lambda attempts, config: 3600.0)
        await worker.run_once()
    long_ago = datetime.now(UTC) - timedelta(days=30)
    async with connections.engine().begin() as connection:
        await connection.execute(
            update(ordering_keys)
            .where(ordering_keys.c.key != "recent")
            .values(touched_at=long_ago)
        )

    stopped = asyncio.Event()
    stopped.set()
    assert await worker._prune_keys(stopped) == 0  # Stopping: no batch at all.
    assert await worker.prune_keys() == 1
    async with connections.engine().connect() as connection:
        kept = sorted((await connection.execute(select(ordering_keys.c.key))).scalars())
    assert kept == ["dead", "recent", "waiting"]
    await enqueue(step, key="done", label="d1")  # A key used again starts anew.
    assert await statuses("done") == [DONE, PENDING]


@pytest.mark.usefixtures("tables")
async def test_a_cleanup_statement_deletes_a_batch_of_keys_idle_long_enough() -> None:
    """Up to its size; a key written exactly ``RETENTION_SECONDS`` ago stays."""
    with freeze_time("2026-10-30 12:00:00"):
        for key in ("a", "b", "c"):
            await enqueue(step, key=key, label=key)
        worker = Worker(config(RETENTION_SECONDS=60))
        await worker.run_once()
        async with connections.engine().begin() as connection:
            for key, age in (("a", 61), ("b", 61), ("c", 60)):
                await connection.execute(
                    update(ordering_keys)
                    .where(ordering_keys.c.key == key)
                    .values(
                        touched_at=datetime(2026, 10, 30, 12, tzinfo=UTC)
                        - timedelta(seconds=age)
                    )
                )
            deleted = await connection.execute(worker.keys_statement("sqlite", 1))
        assert deleted.rowcount == 1
        assert await worker.prune_keys() == 1
        async with connections.engine().connect() as connection:
            left = (await connection.execute(select(ordering_keys.c.key))).scalars()
            assert list(left) == ["c"]


@pytest.mark.usefixtures("tables")
async def test_cleanup_runs_delete_idle_keys(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``clean_until`` and ``run_cleanup_when_due`` delete idle keys too."""
    for key in ("a", "b"):
        await enqueue(step, key=key, label=key)
    worker = Worker(config(CLEANUP_INTERVAL=0.01, RETENTION_SECONDS=0.001))
    await worker.run_once()
    await asyncio.sleep(0.01)
    stop = asyncio.Event()
    prune = worker._prune_keys

    async def prune_then_stop(stopping: asyncio.Event | None) -> int:
        stop.set()
        return await prune(None)

    monkeypatch.setattr(worker, "_prune_keys", prune_then_stop)
    with caplog.at_level(logging.INFO, "domainkit.contrib.messaging.worker"):
        async with asyncio.timeout(WAIT):
            await worker.clean_until(stop)
    assert "Deleted 2 idle ordering key(s) from 'default'" in caplog.messages
    await enqueue(step, key="c", label="c")
    await worker.run_once()
    await asyncio.sleep(0.01)
    await worker.run_cleanup_when_due()
    await worker.run_cleanup_when_due()  # Not due again yet: nothing to do.
    async with connections.engine().connect() as connection:
        assert (await connection.execute(select(ordering_keys))).all() == []


@pytest.mark.usefixtures("tables")
async def test_a_failed_publication_of_the_next_message_is_left_to_the_relay(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The finished message is recorded; the next of its key is pending, logged."""
    await enqueue(step, key="a", label="a0")
    await enqueue(step, key="a", label="a1")
    worker = Worker(config())
    published: list[Promoted] = []

    async def fails(promoted: Promoted) -> None:
        published.append(promoted)
        raise ConnectionError("broker down")

    worker.relay_promoted = fails
    (a0,) = await worker.claim(5)
    with caplog.at_level(logging.ERROR, "domainkit.contrib.messaging.worker"):
        await worker.process(a0)

    assert await statuses("a") == [DONE, PENDING]
    assert [p.queue for p in published] == ["default"]
    next_id = (await rows())[1]["id"]
    assert caplog.messages == [
        f"Could not publish message {next_id}, next of its key; the relay will"
    ]


# Writers of one key take turns.


@pytest.mark.usefixtures("tables")
async def test_writers_of_one_key_take_turns() -> None:
    """Twenty commits at once: twenty places, one pending, run in their order."""
    await asyncio.gather(
        *(enqueue(step, key="hot", label=f"w{number}") for number in range(20))
    )
    found = [row for row in await rows() if row["ordering_key"] == "hot"]
    assert [row["sequence"] for row in found] == list(range(1, 21))
    assert [row["status"] for row in found] == [PENDING] + [HELD] * 19
    await Worker(config()).run_once()
    assert runs.labels("hot") == [row["payload"]["label"] for row in found]


@pytest.mark.usefixtures("tables")
async def test_a_snapshot_older_than_the_keys_last_turn_cannot_write_it() -> None:
    """Under REPEATABLE READ, a stale snapshot fails and runs again (PostgreSQL).

    The unit of work read before another commit wrote the key's first
    message; deciding on its snapshot, its message would be pending too.
    Writing the key's row fails instead, and ``run`` runs it again, held.
    """
    if connections.engine().dialect.name != "postgresql":
        return  # SQLite runs one writer at a time.
    attempts = 0

    async def work() -> None:
        nonlocal attempts
        attempts += 1
        async with NotesUnitOfWork(isolation=Isolation.REPEATABLE_READ) as uow:
            await uow.session.execute(select(notes.c.id))  # The snapshot starts.
            if attempts == 1:
                await enqueue(step, key="rr", label="other")
            uow.defer(step, key="rr", label="mine")
            await uow.commit()

    with pytest.raises(DBAPIError, match="could not serialize access"):
        await NotesUnitOfWork().run(work, retry=Retry(attempts=1))
    attempts = 0
    await NotesUnitOfWork().run(work, retry=Retry(attempts=2, backoff=0))

    found = [row for row in await rows() if row["ordering_key"] == "rr"]
    assert [(r["payload"]["label"], r["status"]) for r in found] == [
        ("other", PENDING),
        ("other", HELD),
        ("mine", HELD),
    ]
    assert attempts == 2


# The broker: the database decides, and the next message goes at once.


@pytest.mark.usefixtures("broker_tables")
async def test_brokered_keys_run_in_order_and_publish_their_next_at_once() -> None:
    """Many consumers, keys interleaved: in order, and no key waits for a poll.

    The relays poll every 30 seconds; each key's next message is published
    by the consumer that finished the one before, so all run in a moment.
    """
    with override_settings(
        MESSAGING={**BROKERED, **SLOW, "CONCURRENCY": 8, "POLL_INTERVAL": 30}
    ):
        settings = registry.settings(MessagingSettings)
        keys = [f"k{number}" for number in range(4)]
        for number in range(5):
            for key in keys:
                await enqueue(step, key=key, label=f"{key}-{number}", pause=0.02)
        workers = BrokerWorkers([Worker(settings)], ["default"], settings)
        stop = asyncio.Event()
        running = asyncio.create_task(workers.run(stop))
        try:
            await until_done(20)
        finally:
            stop.set()
            async with asyncio.timeout(WAIT):
                await running

    assert runs.overlaps == []
    for key in keys:
        assert runs.labels(key) == [f"{key}-{number}" for number in range(5)]
    assert runs.peak > 1


@pytest.mark.usefixtures("broker_tables")
async def test_a_consumer_of_another_outbox_publishes_its_next_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker made for a delivery's outbox relays the key's next message.

    It is made with the consumers' settings and budget.
    """
    settings = MessagingSettings(**BROKERED)
    workers = BrokerWorkers([], ["default"], settings, relay=False)
    worker = workers.outbox("default")
    assert worker is not None
    assert (worker.config, worker.budget) == (settings, workers.budget)
    assert worker.config is settings
    assert worker.budget is workers.budget
    relayed: list[list[uuid.UUID]] = []

    async def relay_ids(broker: object, ids: list[uuid.UUID]) -> int:
        relayed.append(list(ids))
        return len(ids)

    monkeypatch.setattr(worker, "relay_ids", relay_ids)
    with override_settings(MESSAGING={**BROKERED, "PUBLISH_ON_COMMIT": False}):
        await enqueue(step, key="a", label="a0")
        await enqueue(step, key="a", label="a1")
    (a0,) = await worker.claim(5)
    await worker.process(a0)

    assert relayed == [[(await rows())[1]["id"]]]


# The claim did not change; the new statements read the new index.


async def generic_plan(statement: Any) -> str:  # noqa: ANN401 - any statement.
    """The plan PostgreSQL makes for ``statement`` as a generic prepared statement.

    asyncpg prepares every statement, and after five runs PostgreSQL may
    keep a plan made without the values: the one that must read the index.
    """
    engine = connections.engine()
    compiled = statement.compile(
        dialect=engine.dialect, compile_kwargs={"render_postcompile": True}
    )
    values = ", ".join(
        str(value) if isinstance(value, int) else f"'{value}'"
        for value in (compiled.params[name] for name in compiled.positiontup)
    )
    async with engine.begin() as connection:
        await connection.execute(text("SET LOCAL plan_cache_mode = force_generic_plan"))
        await connection.exec_driver_sql(f"PREPARE probe AS {compiled}")
        plan = "\n".join(
            (
                await connection.exec_driver_sql(
                    f"EXPLAIN EXECUTE probe({values})"
                    if values
                    else "EXPLAIN EXECUTE probe"
                )
            ).scalars()
        )
        await connection.exec_driver_sql("DEALLOCATE probe")
    return plan


@pytest.mark.usefixtures("tables")
async def test_the_claim_and_the_key_statements_read_their_indexes() -> None:
    """The claim reads the priority index, as before; a key's line, its own.

    20,000 finished messages, 2,000 held behind one in a hot key: the claim
    never reads them, and finding the key's first waiting message, or its
    next held one, reads the ordering index, never the table.
    """
    if connections.engine().dialect.name != "postgresql":
        return  # SQLite plans without statistics.
    async with connections.engine().begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO domainkit_messages (id, kind, target, queue, priority, "
                "payload, headers, status, attempts, max_attempts, available_at, "
                "created_at, ordering_key, sequence) SELECT gen_random_uuid(), "
                "'task', 't', 'default', 0, '{}', '{}', CASE WHEN g <= 20000 THEN "
                "'done' WHEN g = 20001 THEN 'pending' ELSE 'held' END, 0, 3, now(), "
                "now(), CASE WHEN g > 20000 THEN 'hot' END, CASE WHEN g > 20000 "
                "THEN g - 20000 END FROM generate_series(1, 22000) g"
            )
        )
        await connection.execute(text("ANALYZE domainkit_messages"))
    worker = Worker(config())
    claim = select(messages.c.id).where(
        messages.c.id.in_(worker._due(dbclock.now("postgresql"), 50))
    )
    claim_plan = await generic_plan(claim)
    busy_plan = await generic_plan(select(ordering_module.busy("hot")))
    held = ordering_module._first("hot", (HELD,), messages.c.id)
    next_plan = await generic_plan(select(held))
    stalled_plan = await generic_plan(ordering_module.stalled(50))

    assert "ix_domainkit_messages_priority" in claim_plan
    assert "ordering" not in claim_plan
    for plan in (busy_plan, next_plan, stalled_plan):
        assert "ix_domainkit_messages_ordering" in plan
        assert "Seq Scan" not in plan


# Upgrading: the outbox's new columns and index, and the keys' table.


@contextmanager
def at_0_13(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The outbox as 0.13 had it: no key, no sequence, no index, no keys' table."""
    added = [messages.c.ordering_key, messages.c.sequence]
    with monkeypatch.context() as patched:
        patched.setattr(messages, "indexes", messages.indexes - {ORDERING_INDEX})
        patched.setitem(ordering_keys.info, "domain", "not-installed")
        for column in added:
            messages._columns.remove(column)
        try:
            yield
        finally:
            for column in added:
                messages._columns.add(column)


def test_an_outbox_of_0_13_gets_its_ordering_columns_from_the_next_revision(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``makemigrations`` adds two columns, the index, and the keys' table.

    Nothing else: the revision changes no other table, and drops nothing.
    """
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[MESSAGING],
        DATABASES={"default": {"URL": database_url}},
        MIGRATIONS_DIR=tmp_path / "migrations",
        ALPHA=UNSET,
    ):
        setup()
        with at_0_13(monkeypatch):
            call_command("makemigrations", "-m", "outbox", stdout=io.StringIO())
            call_command("migrate", stdout=io.StringIO())
        assert list(messages.c.keys())[-2:] == ["ordering_key", "sequence"]
        output = io.StringIO()
        call_command("makemigrations", "-m", "ordering", stdout=output)
        revision = Path(output.getvalue().split()[1].rstrip("."))
        call_command("migrate", stdout=io.StringIO())
        checked = io.StringIO()
        call_command("makemigrations", "--check", stdout=checked)
    registry.clear()

    upgrade = revision.read_text(encoding="utf-8").split("def downgrade")[0]
    assert upgrade.count("add_column(") == 2
    assert upgrade.count("create_index(") == 1
    assert upgrade.count("create_table(") == 1
    assert '"domainkit_ordering_keys"' in upgrade
    assert '"ix_domainkit_messages_ordering"' in upgrade
    assert "postgresql_where" in upgrade
    assert "sqlite_where" in upgrade
    assert "drop_" not in upgrade
    assert checked.getvalue() == "No changes detected.\n"
