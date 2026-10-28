"""The inbox and the lease fence: a message's effects in the database, once.

A message runs at least once: a worker that dies after the handler
committed but before the outcome was recorded leaves it to run again, and a
handler that outlives its claim's lease lets another worker run it at the
same time. These tests show both, and what stops the second effect (FIN-80).
Tests wait on events, never on the wall clock.
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import event, func, insert, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError

from domainkit import clock, setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.contrib.messaging import (
    AlreadyHandled,
    LeaseLost,
    MessagingSettings,
    Worker,
    chain,
    enqueue,
    inbox,
    task,
)
from domainkit.contrib.messaging.outbox import message_row
from domainkit.contrib.messaging.registry import CHAIN, dump_event, qualified_name
from domainkit.contrib.messaging.runs import (
    Run,
    prune_statement,
    record,
    uses_inbox,
)
from domainkit.contrib.messaging.tables import CANCELLED, DEAD, DONE, PENDING, messages
from domainkit.contrib.messaging.tables import inbox as inbox_table
from domainkit.contrib.messaging.worker import Claimed
from domainkit.db import connections
from domainkit.ddd import DuplicateValue, event_bus
from domainkit.exceptions import ImproperlyConfigured
from domainkit.tasks import spawn
from domainkit_tests.conftest import MakeDatabase
from domainkit_tests.test_db import Note, NoteId, NotesUnitOfWork, NoteWritten, notes
from domainkit_tests.test_messaging import (  # noqa: F401 - fixtures.
    ALPHA,
    FAST,
    MESSAGING,
    SLOW,
    immediate_retries,
    messaging,
    outbox,
    reporter,
    tables,
)

pytestmark = pytest.mark.anyio

WAIT: Final = 30.0
"""The longest any test waits for something; a regression fails, never hangs."""


async def within(awaitable: Any) -> Any:  # noqa: ANN401 - any awaitable.
    """Await ``awaitable``, failing after ``WAIT`` seconds."""
    async with asyncio.timeout(WAIT):
        return await awaitable


async def credits(database: str = "default") -> int:
    """How many notes the handlers wrote (one per effect)."""
    async with connections.engine(database).connect() as connection:
        found = await connection.scalar(
            select(func.count()).select_from(notes).where(notes.c.text == "credit")
        )
    return int(found or 0)


async def inbox_rows(database: str = "default") -> list[tuple[str, uuid.UUID]]:
    """Every ``(target, message id)`` the inbox of ``database`` holds."""
    async with connections.engine(database).connect() as connection:
        found = await connection.execute(
            select(inbox_table.c.target, inbox_table.c.message_id)
        )
        return [(row.target, row.message_id) for row in found]


async def write_credit(database: str | None = None) -> None:
    """The handlers' effect: one note, in a unit of work of their own."""
    async with NotesUnitOfWork(database=database) as uow:
        await uow.notes.add(Note(id=NoteId(uuid.uuid4()), text="credit"))
        await uow.commit()


@inbox
async def credit(event: NoteWritten) -> None:
    """A durable handler whose whole effect is one commit, under the inbox."""
    await write_credit()


async def plain_credit(event: NoteWritten) -> None:
    """The same handler, without the inbox."""
    await write_credit()


@task(name="tests.inbox.credit_task")
@inbox
async def credit_task() -> None:
    """A task under the inbox (marked before it is registered)."""
    await write_credit()


@inbox
@task(name="tests.inbox.last_step")
async def last_step(**arguments: object) -> None:
    """A chain's last step under the inbox (marked after it is registered)."""
    await write_credit()


@task(name="tests.inbox.rated_credit", rate="1/day")
@inbox
async def rated_credit(number: int) -> None:
    """A task under the inbox that runs once a day at most."""
    await write_credit()


@task(name="tests.inbox.first_step")
async def first_step() -> dict[str, object]:
    """A chain's first step, which hands its result on."""
    await write_credit()
    return {}


@pytest.fixture
def subscribed() -> Iterator[Callable[[Callable[..., Any]], None]]:
    """``subscribed(handler)`` makes it a durable handler of ``NoteWritten``."""
    handlers: list[Callable[..., Any]] = []

    def subscribe(handler: Callable[..., Any]) -> None:
        event_bus.subscribe(NoteWritten, handler, durable=True)
        handlers.append(handler)

    yield subscribe
    for handler in handlers:
        event_bus.unsubscribe(NoteWritten, handler)


async def publish_note_written() -> None:
    """Commit a note whose event has durable handlers."""
    note = Note(id=NoteId(uuid.uuid4()), text="t")
    note.record(NoteWritten(note_id=note.id))
    async with NotesUnitOfWork() as uow:
        await uow.notes.add(note)
        await uow.commit()


async def expire_locks(database: str = "default") -> None:
    """Make every lock look expired, as ``VISIBILITY_TIMEOUT`` later."""
    past = datetime.now(UTC) - timedelta(seconds=1)
    async with connections.engine(database).begin() as connection:
        await connection.execute(update(messages).values(locked_until=past))


def slow_worker(database: str | None = None) -> Worker:
    """A worker whose runs and leases outlast any wait of these tests.

    A run blocked at an event must not time out, nor its lease run out,
    before the test says so, however loaded the machine is.
    """
    return Worker(MessagingSettings(**{**FAST, **SLOW}), database=database)


def failing_once(failing: Worker) -> None:
    """Make ``failing`` fail to record its first outcome, as a crash would."""
    original = failing._done
    failed: list[Claimed] = []

    async def done(message: Claimed, follow_up: dict[str, Any] | None) -> bool:
        if not failed:
            failed.append(message)
            raise OperationalError("UPDATE", {}, Exception("the outbox went away"))
        return await original(message, follow_up)

    failing._done = done  # type: ignore[method-assign]


def outcomes(outcome: str) -> float:
    """Durable handler runs counted under ``outcome`` so far."""
    labels = {"kind": "event", "outcome": outcome}
    return REGISTRY.get_sample_value("messaging_messages_total", labels) or 0


async def events_rows() -> list[dict[str, object]]:
    """The outbox rows of durable handler calls."""
    return [row for row in await outbox() if row["kind"] == "event"]


# The inbox ---------------------------------------------------------------------------


@pytest.mark.usefixtures("tables")
async def test_a_message_run_again_after_its_commit_applies_its_effect_once(
    subscribed: Callable[[Callable[..., Any]], None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The handler committed, the outcome was lost, the lease ran out: one effect.

    The second run finds the inbox row before it calls the handler at all.
    """
    calls: list[NoteWritten] = []

    @inbox
    async def counted(event: NoteWritten) -> None:
        calls.append(event)
        await write_credit()

    subscribed(counted)
    await publish_note_written()
    first = slow_worker()
    failing_once(first)
    assert await first.run_once() == 1  # Committed; its outcome was lost.
    await expire_locks()
    handled = outcomes("handled")
    caplog.clear()

    with caplog.at_level(logging.INFO, "domainkit.contrib.messaging.worker"):
        assert await slow_worker().run_once() == 1  # Delivered again, and skipped.

    assert (len(calls), await credits()) == (1, 1)
    (row,) = await events_rows()
    assert (row["status"], row["attempts"], row["payload"]) == (DONE, 2, {})
    assert await inbox_rows() == [(qualified_name(counted), row["id"])]
    assert outcomes("handled") == handled + 1
    assert caplog.messages == [
        f"Skipped event {qualified_name(counted)}: a run of it committed before (inbox)"
    ]


@pytest.mark.usefixtures("tables")
async def test_a_row_of_another_target_or_message_never_skips_a_run(
    subscribed: Callable[[Callable[..., Any]], None],
) -> None:
    """The look-up before a later attempt matches both the target and the id."""
    calls: list[int] = []

    @inbox
    async def fails_first(event: NoteWritten) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("not yet")
        await write_credit()

    subscribed(fails_first)
    await publish_note_written()
    (row,) = await events_rows()
    others = [
        ("someone.else", row["id"]),
        (qualified_name(fails_first), uuid.uuid4()),
    ]
    async with connections.engine().begin() as connection:
        await connection.execute(
            insert(inbox_table),
            [
                {
                    "target": target,
                    "message_id": message_id,
                    "outbox": "default",
                    "handled_at": clock.now(),
                }
                for target, message_id in others
            ],
        )

    await slow_worker().run_once()  # Fails, then the retry looks, and runs.

    assert (len(calls), await credits()) == (2, 1)
    assert sorted(await inbox_rows()) == sorted(
        [*others, (qualified_name(fails_first), row["id"])]
    )


@pytest.mark.usefixtures("tables")
async def test_without_the_inbox_a_message_run_again_commits_again(
    subscribed: Callable[[Callable[..., Any]], None],
) -> None:
    """Delivery stays at least once for a handler not marked ``inbox``."""
    subscribed(plain_credit)
    await publish_note_written()
    first = slow_worker()
    failing_once(first)
    await first.run_once()
    await expire_locks()
    await slow_worker().run_once()

    assert await credits() == 2
    assert await inbox_rows() == []


@pytest.mark.usefixtures("tables")
async def test_a_commit_that_finds_the_inbox_row_is_refused(
    subscribed: Callable[[Callable[..., Any]], None],
) -> None:
    """A first attempt is not looked up first; its commit finds the row and fails.

    So it goes for a dead letter retried after its handler committed: the
    retry starts again at attempt 1.
    """
    seen: list[BaseException] = []

    @inbox
    async def records_the_refusal(event: NoteWritten) -> None:
        try:
            await write_credit()
        except AlreadyHandled as exc:
            seen.append(exc)
            raise

    subscribed(records_the_refusal)
    await publish_note_written()
    (row,) = await events_rows()
    async with connections.engine().begin() as connection:
        await connection.execute(
            insert(inbox_table).values(
                target=row["target"],
                message_id=row["id"],
                outbox="default",
                handled_at=clock.now(),
            )
        )

    assert await slow_worker().run_once() == 1

    assert await credits() == 0
    assert [str(exc) for exc in seen] == [
        f"Message {row['id']} ({row['target']}) was handled already."
    ]
    (done,) = await events_rows()
    assert (done["status"], done["attempts"]) == (DONE, 1)


@pytest.mark.usefixtures("tables")
async def test_the_run_knows_it_was_handled_whatever_the_handler_raises(
    subscribed: Callable[[Callable[..., Any]], None],
) -> None:
    """A handler that turns the refusal into an error of its own is still skipped."""

    @inbox
    async def wraps_errors(event: NoteWritten) -> None:
        try:
            await write_credit()
        except AlreadyHandled as exc:
            raise RuntimeError("could not credit") from exc

    subscribed(wraps_errors)
    await publish_note_written()
    (row,) = await events_rows()
    async with connections.engine().begin() as connection:
        await connection.execute(
            insert(inbox_table).values(
                target=row["target"],
                message_id=row["id"],
                outbox="default",
                handled_at=clock.now(),
            )
        )

    await slow_worker().run_once()

    (done,) = await events_rows()
    assert (done["status"], done["last_error"]) == (DONE, None)


@pytest.mark.usefixtures("tables")
async def test_a_retry_after_a_failure_before_its_commit_runs_the_handler(
    subscribed: Callable[[Callable[..., Any]], None],
) -> None:
    """A later attempt finds no inbox row, so it runs; its commit writes the row."""
    calls: list[int] = []

    @inbox
    async def fails_first(event: NoteWritten) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("not yet")
        await write_credit()

    subscribed(fails_first)
    await publish_note_written()

    await slow_worker().run_once()  # Fails, then the retry (due at once) runs.

    assert (len(calls), await credits()) == (2, 1)
    (row,) = await events_rows()
    assert (row["status"], row["attempts"]) == (DONE, 2)
    assert await inbox_rows() == [(qualified_name(fails_first), row["id"])]


@pytest.mark.usefixtures("tables")
async def test_only_the_first_commit_of_a_run_writes_the_inbox_row(
    subscribed: Callable[[Callable[..., Any]], None],
) -> None:
    """Later commits of the same run go through; a later run is skipped whole."""

    @inbox
    async def commits_twice(event: NoteWritten) -> None:
        await write_credit()
        await write_credit()

    subscribed(commits_twice)
    await publish_note_written()
    first = slow_worker()
    failing_once(first)
    await first.run_once()
    await expire_locks()
    await slow_worker().run_once()

    assert await credits() == 2
    (row,) = await events_rows()
    assert await inbox_rows() == [(qualified_name(commits_twice), row["id"])]


@pytest.mark.usefixtures("tables")
async def test_a_failed_commit_leaves_the_inbox_row_to_the_next_commit(
    subscribed: Callable[[Callable[..., Any]], None],
) -> None:
    """The row rolls back with a commit that fails; the run's next commit writes it."""
    taken = NoteId(uuid.uuid4())
    async with NotesUnitOfWork() as uow:
        await uow.notes.add(Note(id=taken, text="taken"))
        await uow.commit()

    @inbox
    async def tries_twice(event: NoteWritten) -> None:
        try:
            async with NotesUnitOfWork() as uow:
                await uow.notes.add(Note(id=taken, text="credit"))
                await uow.commit()
        except DuplicateValue:
            await write_credit()

    subscribed(tries_twice)
    await publish_note_written()
    first = slow_worker()
    failing_once(first)
    await first.run_once()
    await expire_locks()
    await slow_worker().run_once()

    assert await credits() == 1
    assert len(await inbox_rows()) == 1


@pytest.mark.usefixtures("tables")
async def test_tasks_and_chains_use_the_inbox() -> None:
    """A task under the inbox, before or after ``@task``, and a chain's last step."""

    async def run_twice() -> None:
        first = slow_worker()
        failing_once(first)
        await first.run_once()  # Committed; the outcome was lost.
        await expire_locks()
        await slow_worker().run_once()  # Skipped.

    await enqueue(credit_task)
    await run_twice()
    async with NotesUnitOfWork() as uow:
        uow.defer(chain(first_step, last_step))
        await uow.commit()
    worker = slow_worker()
    (step,) = await worker.claim(10)
    await worker.process(step)  # The first step, which writes the last.
    await run_twice()

    assert await credits() == 3  # The task, and each step of the chain.
    targets = sorted(target for target, _ in await inbox_rows())
    assert targets == ["tests.inbox.credit_task", "tests.inbox.last_step"]
    assert [row["status"] for row in await outbox()] == [DONE] * 3


def test_methods_and_marks() -> None:
    """A method is marked by its function; unmarked callables are not."""

    class Handlers:
        @inbox
        async def marked(self, event: NoteWritten) -> None: ...

        async def unmarked(self, event: NoteWritten) -> None: ...

    class Later:
        async def handle(self, event: NoteWritten) -> None: ...

    handlers = Handlers()
    later = Later()
    assert inbox(later.handle) == later.handle  # A bound method: its function.
    assert uses_inbox(later.handle)
    assert uses_inbox(Later.handle)
    assert uses_inbox(handlers.marked)
    assert uses_inbox(Handlers.marked)
    assert not uses_inbox(handlers.unmarked)
    assert not uses_inbox(plain_credit)
    assert not uses_inbox(object())  # Nothing to mark, nothing marked.

    async def handler(event: NoteWritten) -> None: ...

    assert inbox(handler) is handler  # Returned as it is.


def test_only_a_chains_last_step_may_use_the_inbox() -> None:
    """A step that hands its result on is refused; the last one is not."""
    refused = (
        "tests.inbox.last_step uses the inbox, so only the last step of a chain "
        "may be it: a skipped run has no result to hand on."
    )
    with pytest.raises(ImproperlyConfigured) as caught:
        chain(last_step, first_step)
    assert str(caught.value) == refused
    with pytest.raises(ImproperlyConfigured, match=r"^tests\.inbox\.credit_task uses"):
        chain(first_step, credit_task, last_step)
    assert chain(first_step, last_step).keys == [
        "tests.inbox.first_step",
        "tests.inbox.last_step",
    ]


@pytest.mark.usefixtures("tables")
async def test_a_step_that_hands_its_result_on_runs_without_the_inbox() -> None:
    """A message whose chain goes on (written by hand) never writes an inbox row."""
    row = message_row(
        kind="task",
        target="tests.inbox.credit_task",
        payload={},
        headers={CHAIN: ["tests.inbox.first_step"]},
        max_attempts=1,
    )
    async with connections.engine().begin() as connection:
        await connection.execute(insert(messages).values(row))

    assert await slow_worker().run_once() == 2  # The step, then the next.

    assert await credits() == 2
    assert await inbox_rows() == []


def test_the_inbox_needs_postgresql_or_sqlite() -> None:
    """Another database cannot skip a conflict, so the inbox refuses it."""
    run = Run(uuid.uuid4(), "t", uuid.uuid4(), "default", 1.0)

    with pytest.raises(ImproperlyConfigured, match="not mysql"):
        record(run, "mysql")


# The lease ---------------------------------------------------------------------------


class Blocked:
    """The first call of the handler waits until released; later ones run."""

    def __init__(self, effect: Callable[[], Any] | None = None) -> None:
        """Run ``effect`` (default: a note) in each call, once released."""
        self.effect = effect or write_credit
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, event: NoteWritten) -> None:
        """Block the first call before it commits, then write the effect."""
        self.calls += 1
        if self.calls == 1:
            self.entered.set()
            await self.release.wait()
        await self.effect()


async def outlive_the_lease(blocked: Blocked, database: str = "default") -> None:
    """Block a worker's run; another runs the message; then release the first."""
    stale = asyncio.create_task(slow_worker(database).run_once())
    try:
        await within(blocked.entered.wait())
        await expire_locks(database)  # The first worker's lease ran out.
        assert await slow_worker(database=database).run_once() == 1  # Taken over.
    finally:
        blocked.release.set()
        await within(stale)


@pytest.mark.usefixtures("tables")
async def test_a_handler_blocked_past_its_lease_commits_once(
    subscribed: Callable[[Callable[..., Any]], None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Another worker reclaims the message and runs it; the stale run's commit fails."""
    blocked = Blocked()
    refused: list[BaseException] = []

    async def slow_credit(event: NoteWritten) -> None:
        try:
            await blocked(event)
        except LeaseLost as exc:
            refused.append(exc)
            raise

    subscribed(slow_credit)
    await publish_note_written()
    superseded = outcomes("superseded")

    with caplog.at_level(logging.WARNING, "domainkit.contrib.messaging.worker"):
        await outlive_the_lease(blocked)

    assert blocked.calls == 2
    assert await credits() == 1
    (row,) = await events_rows()
    assert (row["status"], row["attempts"], row["last_error"]) == (DONE, 2, None)
    assert [str(exc) for exc in refused] == [
        (
            f"Message {row['id']} ({row['target']}) was taken over by another "
            "worker; this run's commit is refused."
        )
    ]
    assert outcomes("superseded") == superseded + 1
    assert caplog.messages == [
        (
            f"event {row['target']} lost its lease while it ran: another worker "
            "took it over and runs it"
        )
    ]


@pytest.mark.usefixtures("tables")
async def test_a_run_that_finishes_after_losing_its_lease_changes_nothing(
    subscribed: Callable[[Callable[..., Any]], None],
) -> None:
    """A handler without database work finishes late: the message stays the other's."""
    effects: list[str] = []

    async def remember() -> None:
        effects.append("ran")

    blocked = Blocked(remember)

    async def slow(event: NoteWritten) -> None:
        await blocked(event)

    subscribed(slow)
    await publish_note_written()
    superseded = outcomes("superseded")
    stale = asyncio.create_task(slow_worker().run_once())
    try:
        await within(blocked.entered.wait())
        await expire_locks()
        second = slow_worker()
        (claimed,) = await second.claim(1)  # Taken over, not finished yet.
    finally:
        blocked.release.set()
        await within(stale)

    assert outcomes("superseded") == superseded + 1
    (row,) = await events_rows()
    assert (row["status"], row["lock_id"]) == (PENDING, claimed.lock_id)
    await second.process(claimed)
    assert effects == ["ran", "ran"]
    assert (await events_rows())[0]["status"] == DONE


@pytest.mark.usefixtures("tables")
async def test_a_commit_renews_the_lease(
    subscribed: Callable[[Callable[..., Any]], None],
) -> None:
    """A commit made while the lease had run out keeps it, and takes it back."""

    async def expired_then_commits(event: NoteWritten) -> None:
        await expire_locks()  # As if the run took VISIBILITY_TIMEOUT so far.
        await write_credit()
        async with connections.engine().connect() as connection:
            locked = await connection.scalar(
                select(messages.c.locked_until).where(messages.c.kind == "event")
            )
        renewed.append(locked)

    renewed: list[datetime | None] = []
    subscribed(expired_then_commits)
    await publish_note_written()

    await slow_worker().run_once()

    (locked,) = renewed
    assert locked is not None
    assert locked > datetime.now(UTC)
    assert await credits() == 1
    assert (await events_rows())[0]["status"] == DONE


@pytest.mark.usefixtures("tables")
async def test_a_commit_renews_its_own_messages_lease_only(
    subscribed: Callable[[Callable[..., Any]], None],
) -> None:
    """Messages claimed together share a lock id; a commit renews its own."""

    async def commits(event: NoteWritten) -> None:
        await write_credit()

    subscribed(commits)
    await publish_note_written()
    await publish_note_written()
    worker = slow_worker()
    first, second = await worker.claim(10)
    assert first.lock_id == second.lock_id
    past = datetime.now(UTC) - timedelta(seconds=1)
    async with connections.engine().begin() as connection:
        await connection.execute(
            update(messages).where(messages.c.id == second.id).values(locked_until=past)
        )

    await worker.process(first)

    async with connections.engine().connect() as connection:
        locked = await connection.scalar(
            select(messages.c.locked_until).where(messages.c.id == second.id)
        )
    assert locked == past


@pytest.mark.usefixtures("tables")
async def test_work_left_running_after_the_message_commits_on_its_own(
    subscribed: Callable[[Callable[..., Any]], None],
) -> None:
    """A task the handler started outlives the run; its commits are not fenced.

    One started in a clean context (``spawn``), and one that inherits the
    run's context (``create_task``), which knows the run has ended.
    """
    release = asyncio.Event()
    left: list[asyncio.Task[None]] = []

    async def later() -> None:
        await release.wait()
        await write_credit()

    async def starts_work(event: NoteWritten) -> None:
        left.append(spawn(later(), name="tests.inbox.later"))
        left.append(asyncio.get_running_loop().create_task(later()))

    subscribed(starts_work)
    await publish_note_written()
    await slow_worker().run_once()
    release.set()
    await within(asyncio.gather(*left))

    assert await credits() == 2
    assert (await events_rows())[0]["status"] == DONE


# Another database --------------------------------------------------------------------


@pytest.fixture
async def two_databases(
    request: pytest.FixtureRequest, make_database: MakeDatabase
) -> AsyncIterator[None]:
    """Messaging's tables in both, an outbox each; the notes in ``default``."""
    request.getfixturevalue("messaging")
    with override_settings(
        DATABASES={
            "default": {"URL": make_database("main")},
            "other": {"URL": make_database("other")},
        },
        DATABASE_ROUTING={"alpha": "other"},  # So "other" holds an outbox.
    ):
        registry.clear()
        setup()
        async with connections.engine("default").begin() as connection:
            await connection.run_sync(messages.create)
            await connection.run_sync(inbox_table.create)
            await connection.run_sync(notes.create)
        async with connections.engine("other").begin() as connection:
            await connection.run_sync(messages.create)
            await connection.run_sync(inbox_table.create)
        yield
        await connections.close_all()


async def queue_in_other(handler: Callable[..., Any]) -> None:
    """Write a durable call of ``handler`` to the outbox of ``other``."""
    event = NoteWritten(note_id=uuid.uuid4())
    row = message_row(
        kind="event",
        target=qualified_name(handler),
        payload=dump_event(event),
        headers={"event_type": qualified_name(NoteWritten)},
        max_attempts=5,
    )
    async with connections.engine("other").begin() as connection:
        await connection.execute(insert(messages).values(row))


async def test_the_inbox_on_another_database_than_the_outbox(
    two_databases: None, subscribed: Callable[[Callable[..., Any]], None]
) -> None:
    """No lease to fence there; the inbox row refuses the stale run's commit."""
    blocked = Blocked()

    @inbox
    async def credit_here(event: NoteWritten) -> None:
        await blocked(event)

    subscribed(credit_here)
    await queue_in_other(credit_here)

    await outlive_the_lease(blocked, "other")

    assert blocked.calls == 2
    assert await credits() == 1
    ((target, _),) = await inbox_rows()
    assert target == qualified_name(credit_here)
    assert await inbox_rows("other") == []  # Where the handler committed only.
    async with connections.engine("other").connect() as connection:
        (row,) = (await connection.execute(select(messages))).all()
    assert (row.status, row.attempts) == (DONE, 2)
    assert await slow_worker().prune_inbox() == 1  # Its message is done there.
    assert await inbox_rows() == []


# Pruning -----------------------------------------------------------------------------


async def handled(
    *statuses: str | None,
    database: str = "default",
    outbox: str = "default",
    keys: Sequence[tuple[str, uuid.UUID]] = (),
) -> list[uuid.UUID]:
    """A message in each of ``statuses`` (None: none) in ``outbox``, a row for each.

    The rows go to the inbox of ``database``, handled a year ago: their age
    never matters. ``keys`` gives their ``(target, message_id)`` (default:
    ``t`` and a new id). Return the messages' ids, in order.
    """
    long_ago = datetime.now(UTC) - timedelta(days=365)
    keys = keys or [("t", uuid.uuid4()) for _ in statuses]
    ids = [message_id for _, message_id in keys]
    kept = [
        {
            **message_row(
                kind="task", target="t", payload={}, headers={}, max_attempts=1
            ),
            "id": message_id,
            "status": status,
        }
        for message_id, status in zip(ids, statuses, strict=True)
        if status is not None
    ]
    if kept:
        async with connections.engine(outbox).begin() as connection:
            await connection.execute(insert(messages), kept)
    async with connections.engine(database).begin() as connection:
        await connection.execute(
            insert(inbox_table),
            [
                {
                    "target": target,
                    "message_id": message_id,
                    "outbox": outbox,
                    "handled_at": long_ago,
                }
                for target, message_id in keys
            ],
        )
    return ids


@pytest.mark.usefixtures("tables")
async def test_a_row_lives_as_long_as_its_message_may_run() -> None:
    """Pending and dead messages keep their rows, whatever their age; others go.

    So do rows of messages the outbox no longer holds (cleaned up), and
    never rows of another outbox's messages, which that outbox decides on.
    """
    pending, dead, *_ = await handled(PENDING, DEAD, DONE, CANCELLED, None)
    (elsewhere,) = await handled(None, outbox="gone")

    assert await slow_worker().prune_inbox() == 3

    assert sorted(message_id for _, message_id in await inbox_rows()) == sorted(
        [pending, dead, elsewhere]
    )


@pytest.mark.usefixtures("tables")
async def test_a_held_back_message_commits_once_whatever_its_wait() -> None:
    """A message held back by its rate for days keeps its row until it is done.

    Eight messages of a ``1/day`` task: the run of one commits and loses its
    outcome (a crash), and it goes back behind the other seven, a day
    apart. Pruning every day must not forget it (SEC-11b-1).
    """
    start = datetime.now(UTC).replace(microsecond=0)
    with clock.freeze_time(start) as frozen:
        for number in range(8):
            await enqueue(rated_credit, number=number)
        worker = slow_worker()
        failing_once(worker)  # The run that commits loses its outcome.

        # Day 0: one runs and commits, the other seven are held a day apart.
        assert await worker.run_once() == 8
        assert await credits() == 1
        (handled_row,) = await inbox_rows()

        # Its lease runs out; claimed again, it is held behind the seven.
        frozen.tick(SLOW["VISIBILITY_TIMEOUT"] + 1)
        assert await worker.run_once() == 1
        assert await credits() == 1

        # Days 1 to 7: the seven run, one a day; the inbox is pruned each day.
        for day in range(1, 8):
            frozen.move_to(start + timedelta(days=day, minutes=5))
            await worker.run_once()
            await worker.prune_inbox()
        assert await credits() == 8
        assert handled_row in await inbox_rows()  # Its message is pending.

        # Day 8: the held-back message comes due: the inbox skips it.
        frozen.move_to(start + timedelta(days=8, minutes=5))
        await worker.run_once()
        await worker.prune_inbox()

    async with connections.engine().connect() as connection:
        found = await connection.execute(select(messages.c.status, messages.c.attempts))
        assert sorted(tuple(row) for row in found) == [(DONE, 1)] * 7 + [(DONE, 2)]
    assert await credits() == 8, "the committed message committed its work again"
    assert await inbox_rows() == []  # Every message is done: every row went.


def statements(database: str, starting: str) -> tuple[list[str], Callable[[], None]]:
    """Collect the statements ``database``'s engine runs that start with ``starting``.

    Returns the list, and a function that stops collecting.
    """
    seen: list[str] = []
    engine = connections.engine(database).sync_engine

    def collect(*args: Any) -> None:  # noqa: ANN401 - before_cursor_execute's.
        if str(args[2]).lstrip().startswith(starting):
            seen.append(str(args[2]))

    def stop() -> None:
        event.remove(engine, "before_cursor_execute", collect)

    event.listen(engine, "before_cursor_execute", collect)
    return seen, stop


def test_a_prune_batch_is_bounded_and_skips_locked_rows() -> None:
    """The size is written into the SQL, as cleanup's is (PERF-02)."""
    dialect = postgresql.dialect()  # type: ignore[no-untyped-call]
    compiled = prune_statement("default", 1000).compile(
        dialect=dialect, compile_kwargs={"render_postcompile": True}
    )
    sql = str(compiled)

    assert "LIMIT 1000" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "domainkit_inbox.outbox = " in sql
    assert "NOT (EXISTS (SELECT domainkit_messages.id" in sql


@pytest.mark.usefixtures("tables")
async def test_rows_are_pruned_in_batches() -> None:
    """Up to CLEANUP_BATCH_SIZE rows per statement, until none is left."""
    await handled(*[DONE] * 5)
    worker = Worker(MessagingSettings(**{**FAST, "CLEANUP_BATCH_SIZE": 2}))
    deletes, stop = statements("default", "DELETE FROM domainkit_inbox")
    try:
        assert await worker.prune_inbox() == 5
    finally:
        stop()

    assert len(deletes) == 3  # 2, 2, then 1.

    assert await inbox_rows() == []
    assert (worker._prune_batch, worker._cleanup_batch) == (2, 2)
    assert await worker.prune_inbox() == 0


@pytest.mark.usefixtures("tables")
async def test_a_failed_prune_halves_the_next_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The inbox's batches shrink and grow on their own, as the outbox's do."""
    await handled(DONE, DONE, DONE)
    worker = Worker(MessagingSettings(**{**FAST, "CLEANUP_BATCH_SIZE": 5}))
    statement = worker.inbox_statement

    def broken(dialect: str, size: int) -> Any:  # noqa: ANN401 - a statement.
        raise OperationalError("DELETE", {}, Exception("statement timeout"))

    monkeypatch.setattr(worker, "inbox_statement", broken)
    with pytest.raises(OperationalError):
        await worker.prune_inbox()
    assert (worker._prune_batch, worker._cleanup_batch) == (2, 5)  # 5 // 2.
    assert isinstance(worker._prune_batch, int)
    monkeypatch.setattr(worker, "inbox_statement", statement)
    assert await worker.prune_inbox() == 3  # Batches of 2, then 1 of up to 4.
    assert worker._prune_batch == 4


@pytest.mark.usefixtures("tables")
async def test_cleanup_prunes_the_inbox(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``clean_until`` and ``run_cleanup_when_due`` prune the inbox too."""
    await handled(DONE, CANCELLED)
    worker = Worker(MessagingSettings(**{**FAST, "CLEANUP_INTERVAL": 0.01}))
    stop = asyncio.Event()
    prune = worker._prune

    async def prune_then_stop(stopping: asyncio.Event | None) -> int:
        stop.set()
        return await prune(None)

    monkeypatch.setattr(worker, "_prune", prune_then_stop)
    with caplog.at_level(logging.INFO, "domainkit.contrib.messaging.worker"):
        await within(worker.clean_until(stop))

    assert caplog.messages == [
        "Deleted 2 handled message id(s) from the inbox of 'default'"
    ]
    await handled(DONE)
    await worker.run_cleanup_when_due()
    assert await inbox_rows() == []


@pytest.mark.usefixtures("two_databases")
async def test_rows_of_another_outbox_are_pruned_by_what_it_holds() -> None:
    """That outbox says which messages finished; a batch at a time, in key order.

    Keys run ``(a, 3) (a, 4) (b, 1) (b, 2) (b, 5) (b, 6)``, written in the
    reverse order: read by key, not by id nor as written, or some rows
    would never be read.
    """
    keys = [
        ("a", uuid.UUID(int=3)),
        ("a", uuid.UUID(int=4)),
        ("b", uuid.UUID(int=1)),
        ("b", uuid.UUID(int=2)),
        ("b", uuid.UUID(int=5)),
        ("b", uuid.UUID(int=6)),
    ]
    statuses = [DONE, CANCELLED, PENDING, None, DEAD, DONE]
    await handled(*statuses[::-1], outbox="other", keys=keys[::-1])
    await handled(DONE)  # This outbox's own, in the same pass.
    worker = Worker(MessagingSettings(**{**FAST, **SLOW, "CLEANUP_BATCH_SIZE": 3}))
    stopped = asyncio.Event()
    stopped.set()

    assert await worker._prune(stopped) == 0  # Stopped: nothing more starts.
    asked, stop = statements("other", "SELECT domainkit_messages.id")
    try:
        assert await worker.prune_inbox() == 5
    finally:
        stop()

    assert len(asked) == 2  # Two batches of three, each asked once.
    assert sorted(message_id for _, message_id in await inbox_rows()) == [
        uuid.UUID(int=1),  # Pending in "other".
        uuid.UUID(int=5),  # Dead there.
    ]
    assert await worker.prune_inbox() == 0  # Nothing finished is left.
