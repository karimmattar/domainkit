"""The pytest fixtures for events and queued work (``messaging.testing``)."""

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta

import pytest

from domainkit import clock
from domainkit.contrib.messaging import chain, enqueue, task
from domainkit.contrib.messaging import outbox as outbox_module
from domainkit.contrib.messaging import testing as kit
from domainkit.contrib.messaging.tables import DEAD, DONE, messages
from domainkit.db import connections
from domainkit.ddd import DomainEvent, event_bus
from domainkit_tests.sample_domains.alpha.events import GreetingSent
from domainkit_tests.test_db import Note, NoteId, NotesUnitOfWork, NoteWritten
from domainkit_tests.test_messaging import (  # noqa: F401 - fixtures.
    always_fails,
    durable_handler,
    fails_once,
    immediate_retries,
    messaging,
    outbox,
    ran,
    remember,
    send_email,
    tables,
)

pytestmark = pytest.mark.anyio


@task(name="tests.again_and_again")
async def again_and_again() -> None:
    """Queue itself, forever."""
    await enqueue(again_and_again)


@task(name="tests.add_one")
async def add_one(number: int) -> dict[str, int]:
    """A chain step."""
    ran.append(number)
    return {"number": number + 1}


@pytest.fixture
def seen() -> Iterator[kit.Outbox]:
    """An Outbox watching commits, as the ``outbox`` fixture sets one up."""
    watcher = kit.Outbox()
    outbox_module.watchers.append(watcher.watch)
    yield watcher
    outbox_module.watchers.remove(watcher.watch)


@pytest.fixture
async def eager() -> AsyncIterator[kit.DeferredExecutor]:
    """A DeferredExecutor running each commit's messages."""
    executor = kit.DeferredExecutor()
    outbox_module.watchers.append(executor.watch)
    yield executor
    outbox_module.watchers.remove(executor.watch)


def test_captured_events_are_kept_in_order() -> None:
    """Events published on the bus are kept; helpers find and assert them."""
    captured = kit.CapturedEvents()
    note = NoteWritten(note_id=NoteId(uuid.uuid4()))
    hello = GreetingSent(to="ann")

    async def publish() -> None:
        event_bus.subscribe(DomainEvent, captured.record)
        try:
            await event_bus.publish_all([note, hello, GreetingSent(to="bob")])
        finally:
            event_bus.unsubscribe(DomainEvent, captured.record)

    asyncio.run(publish())

    assert list(captured) == [note, hello, captured.events[2]]
    assert len(captured) == 3
    assert captured.of_type(GreetingSent) == [hello, captured.events[2]]
    assert captured.assert_published(GreetingSent, to="ann") is hello
    assert captured.assert_published(NoteWritten) is note
    with pytest.raises(
        AssertionError, match=r"Expected one GreetingSent with \{\}, found 2"
    ):
        captured.assert_published(GreetingSent)
    with pytest.raises(AssertionError, match="found 0 among"):
        captured.assert_published(GreetingSent, to="eve")
    with pytest.raises(AssertionError, match="found 0"):
        captured.assert_published(GreetingSent, nonexistent="ann")
    with pytest.raises(AssertionError, match="Expected no NoteWritten, found 1"):
        captured.assert_not_published(NoteWritten)
    captured.clear()
    captured.assert_not_published(NoteWritten)
    assert len(captured) == 0


@pytest.mark.usefixtures("tables")
async def test_the_outbox_sees_committed_messages(seen: kit.Outbox) -> None:
    """Tasks and durable handler calls, from units of work and enqueue."""
    event_bus.subscribe(NoteWritten, durable_handler, durable=True)
    try:
        note = Note(id=NoteId(uuid.uuid4()), text="t")
        note.record(NoteWritten(note_id=note.id))
        async with NotesUnitOfWork() as uow:
            await uow.notes.add(note)
            uow.defer(remember, value=note.id, label="x")
            await uow.commit()
        async with NotesUnitOfWork() as uow:  # Rolled back: never seen.
            uow.defer(remember, value=uuid.uuid4())
        await enqueue(send_email, to="ada@example.com", delay=timedelta(hours=1))
        await enqueue(chain(add_one, add_one), number=1)
    finally:
        event_bus.unsubscribe(NoteWritten, durable_handler)

    assert [m.kind for m in seen.queued()] == ["event", "task", "task", "task"]
    handled = seen.assert_queued(durable_handler, note_id=str(note.id))
    assert (handled.kind, handled.database) == ("event", "default")
    queued = seen.assert_queued(remember, value=note.id)
    assert queued.arguments == {"value": str(note.id), "label": "x"}
    assert (queued.queue, queued.priority, queued.dedupe_key) == ("default", 0, None)
    assert (
        seen.assert_queued("domainkit_tests.test_messaging.send_email").queue
        == "emails"
    )
    assert seen.assert_queued(chain(add_one)).headers["chain"] == ["tests.add_one"]
    with pytest.raises(AssertionError, match=r"with \{'label': 'y'\}, found 0 among"):
        seen.assert_queued(remember, label="y")
    seen.assert_not_queued(always_fails)
    with pytest.raises(AssertionError, match=r"Expected no .*remember, found 1"):
        seen.assert_not_queued(remember)
    seen.clear()
    assert seen.queued() == []


@pytest.mark.usefixtures("tables")
async def test_run_queued_runs_everything_due(seen: kit.Outbox) -> None:
    """Due messages run, chained steps too; delayed ones wait unless asked."""
    await enqueue(chain(add_one, add_one, add_one), number=1)
    await enqueue(remember, value=uuid.uuid4(), delay=timedelta(hours=1))

    assert await seen.arun_queued() == 3
    assert ran == [1, 2, 3]
    assert await seen.arun_queued(delayed=True) == 1
    assert {row["status"] for row in await outbox()} == {DONE}
    assert [m.target for m in seen.queued()][-2:] == ["tests.add_one", "tests.add_one"]


@pytest.mark.usefixtures("tables")
async def test_run_queued_reports_failures(seen: kit.Outbox) -> None:
    """A failed run raises, naming the message and the error; or is returned."""
    await enqueue(always_fails)

    with pytest.raises(kit.QueuedWorkFailed) as failed:
        await seen.arun_queued(delayed=True)

    assert str(failed.value).splitlines() == [
        "Queued work failed:",
        "task tests.always_fails (retry): RuntimeError: still broken",
        "task tests.always_fails (retry): RuntimeError: still broken",
        "task tests.always_fails (dead): RuntimeError: still broken",
    ]
    (row,) = await outbox()
    assert row["status"] == DEAD
    await enqueue(fails_once, key="k")
    assert await seen.arun_queued(raise_errors=False) == 2  # Failed, then ran.
    assert (await outbox())[-1]["status"] == DONE


@pytest.mark.usefixtures("tables")
async def test_run_queued_stops_work_that_never_ends(seen: kit.Outbox) -> None:
    """A task that queues itself fails the run after max_rounds."""
    await enqueue(again_and_again)

    with pytest.raises(kit.QueuedWorkFailed, match="kept coming after 3 rounds"):
        await seen.arun_queued(max_rounds=3)


@pytest.mark.usefixtures("tables")
async def test_run_queued_works_from_sync_code_inside_a_loop(seen: kit.Outbox) -> None:
    """Called while a loop runs, the sync form runs in a thread of its own."""
    await enqueue(remember, value=uuid.uuid4())

    assert seen.run_queued() == 1
    assert len(ran) == 1


@pytest.mark.usefixtures("messaging")
def test_run_queued_works_from_sync_tests() -> None:
    """Without a running loop, the sync form runs one."""

    async def prepare() -> None:
        async with connections.engine().begin() as connection:
            await connection.run_sync(messages.create)
        await enqueue(remember, value=uuid.uuid4())
        await connections.close_all()

    asyncio.run(prepare())

    assert kit.Outbox().run_queued() == 1
    assert len(ran) == 1


@pytest.mark.usefixtures("tables")
async def test_deferred_work_runs_at_commit(eager: kit.DeferredExecutor) -> None:
    """Each commit runs its messages before commit() returns."""
    async with NotesUnitOfWork() as uow:
        await uow.notes.add(Note(id=NoteId(uuid.uuid4()), text="t"))
        uow.defer(chain(add_one, add_one), number=5)
        await uow.commit()
        assert ran == [5, 6]

    await enqueue(always_fails)

    assert eager.ran == 2 + 3  # Retries are due at once in these tests.
    assert eager.failures == [
        "task tests.always_fails (retry): RuntimeError: still broken",
        "task tests.always_fails (retry): RuntimeError: still broken",
        "task tests.always_fails (dead): RuntimeError: still broken",
    ]


@pytest.mark.usefixtures("messaging")
def test_the_plugin_gives_fixtures_to_sync_and_async_tests(
    pytester: pytest.Pytester,
) -> None:
    """captured_events, outbox, and execute_deferred, loaded with -p."""

    async def prepare() -> None:
        async with connections.engine().begin() as connection:
            await connection.run_sync(messages.create)
        await connections.close_all()

    asyncio.run(prepare())
    pytester.makepyfile(
        test_work="""
        import uuid
        import pytest
        from domainkit.contrib.messaging import enqueue
        from domainkit.management.base import run_async
        from domainkit_tests.test_messaging import always_fails, remember

        def test_sync(outbox, captured_events):
            run_async(enqueue(remember, value=uuid.uuid4()))
            outbox.assert_queued(remember)
            assert outbox.run_queued() == 1
            assert len(captured_events) == 0

        @pytest.mark.anyio
        async def test_async(outbox, execute_deferred):
            await enqueue(remember, value=uuid.uuid4())
            assert execute_deferred.ran == 1
            assert await outbox.arun_queued() == 0

        @pytest.mark.anyio
        async def test_failing(execute_deferred):
            await enqueue(always_fails)
        """
    )

    result = pytester.runpytest_inprocess(
        "-p",
        "domainkit.contrib.messaging.testing",
        "-p",
        "no:randomly",
        "-p",
        "no:cov",
    )

    result.assert_outcomes(passed=3, errors=1)
    result.stdout.fnmatch_lines(
        ["*QueuedWorkFailed: Deferred work failed:*", "*task tests.always_fails*"]
    )
    assert outbox_module.watchers == []


async def _now() -> datetime:
    """The framework's time, read in whatever loop runs this."""
    return clock.now()


async def test_sync_helpers_keep_the_callers_context() -> None:
    """A frozen clock reaches the thread the sync form runs in."""
    with clock.freeze_time("2030-01-01T00:00:00Z"):
        assert kit._sync(_now()) == datetime(2030, 1, 1, tzinfo=UTC)


@pytest.mark.usefixtures("tables")
async def test_delayed_work_runs_at_the_frozen_time(seen: kit.Outbox) -> None:
    """With a frozen clock, delayed=True makes waiting work due at its time."""
    with clock.freeze_time("2030-01-01T00:00:00Z") as frozen:
        await enqueue(remember, value=uuid.uuid4(), delay=timedelta(hours=1))
        assert await seen.arun_queued() == 0
        frozen.tick(timedelta(hours=2))
        assert await seen.arun_queued() == 1
        await enqueue(remember, value=uuid.uuid4(), delay=timedelta(days=1))
        assert seen.run_queued(delayed=True) == 1
    (_, last) = await outbox()
    assert last["finished_at"] == datetime(2030, 1, 1, 2, tzinfo=UTC)
