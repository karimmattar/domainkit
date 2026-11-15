"""Durable handlers keep a name of their own (user report W5).

A durable handler's messages carry its key. Until 2.4 that was always
``module.QualName``: renaming or moving the function, or making it
in-process, orphaned the messages queued for it, which waited
``UNKNOWN_TARGET_GRACE`` and died. ``name=`` gives it a key that stays,
and ``formerly=`` keeps running the messages queued under its old ones.
"""

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import select

from domainkit.conf import settings
from domainkit.contrib.messaging import Worker
from domainkit.contrib.messaging import apps as apps_module
from domainkit.contrib.messaging import outbox as outbox_module
from domainkit.contrib.messaging import testing as kit
from domainkit.contrib.messaging.apps import MessagingConfig
from domainkit.contrib.messaging.tables import DONE, messages
from domainkit.db import connections
from domainkit.ddd import DomainEvent, EventBus, HandlerKey, event_bus, handles
from domainkit.ddd.events import subscribe_marked
from domainkit_tests.test_db import Note, NoteId, NotesUnitOfWork, NoteWritten
from domainkit_tests.test_messaging import immediate_retries, messaging, tables

__all__ = ["immediate_retries", "messaging", "tables"]  # The fixtures the tests take.

pytestmark = pytest.mark.anyio

OLD_KEY = f"{__name__}.on_written_before"
"""The key the handler's messages carried before it was named: its old path."""

seen: list[uuid.UUID] = []


class NoteWrittenAgain(NoteWritten):
    """Another event the handler takes."""


async def on_written(event: NoteWritten) -> None:
    """The handler, as it is now."""
    seen.append(event.note_id)


async def other(event: DomainEvent) -> None:
    """Another handler."""


@pytest.fixture(autouse=True)
def clean() -> Iterator[None]:
    """Each test's subscriptions and runs are its own."""
    seen.clear()
    with event_bus.subscriptions_kept():
        yield


@pytest.fixture
def watched() -> Iterator[kit.Outbox]:
    """An Outbox watching commits, as the ``outbox`` fixture sets one up."""
    watcher = kit.Outbox()
    outbox_module.watchers.append(watcher.watch)
    yield watcher
    outbox_module.watchers.remove(watcher.watch)


async def write_note() -> NoteId:
    """Commit a note, which records ``NoteWritten``."""
    note = Note(id=NoteId(uuid.uuid4()), text="t")
    note.record(NoteWritten(note_id=note.id))
    async with NotesUnitOfWork() as uow:
        await uow.notes.add(note)
        await uow.commit()
    return note.id


async def targets() -> list[str]:
    """Each outbox row's target, oldest first."""
    async with connections.engine().connect() as connection:
        rows = await connection.execute(
            select(messages.c.target).order_by(messages.c.created_at)
        )
        return list(rows.scalars())


async def statuses() -> list[str]:
    """Each outbox row's status, oldest first."""
    async with connections.engine().connect() as connection:
        rows = await connection.execute(
            select(messages.c.status).order_by(messages.c.created_at)
        )
        return list(rows.scalars())


def test_a_named_handler_keys_its_messages_by_its_name() -> None:
    """``name=`` is the key; without one it is the module's path."""
    bus = EventBus()
    bus.subscribe(NoteWritten, on_written, durable=True, name="notes.on_written")
    bus.subscribe(NoteWritten, other, durable=True)

    assert bus.durable_targets(NoteWritten) == [
        ("notes.on_written", on_written),
        (f"{__name__}.other", other),
    ]
    assert bus.handler_key(on_written) == "notes.on_written"
    assert bus.handler_key(other) == f"{__name__}.other"
    assert EventBus().handler_key(on_written) == f"{__name__}.on_written"
    assert bus.handler_keys() == [
        HandlerKey("notes.on_written", NoteWritten, on_written, True, True),
        HandlerKey(f"{__name__}.other", NoteWritten, other, False, True),
    ]


def test_handles_takes_a_name_and_former_keys() -> None:
    """``@handles`` subscribes as ``subscribe`` would, with both."""
    module = type(uuid)(__name__)

    @handles(NoteWritten, durable=True, name="notes.marked", formerly=[OLD_KEY])
    async def marked(event: NoteWritten) -> None: ...

    module.marked = marked  # type: ignore[attr-defined]
    bus = EventBus()

    assert subscribe_marked(module, bus) == 1
    assert [(key.key, key.current) for key in bus.handler_keys()] == [
        ("notes.marked", True),
        (OLD_KEY, False),
    ]


def test_a_name_and_its_keys_are_refused_where_a_worker_could_not_tell() -> None:
    """In-process handlers have no name; no two handlers share a key."""
    bus = EventBus()
    bus.subscribe(NoteWritten, on_written, durable=True, name="notes.on_written")

    with pytest.raises(ValueError, match="in-process handler has none"):
        bus.subscribe(NoteWritten, other, name="notes.other")
    with pytest.raises(ValueError, match=r"a key of .*on_written already"):
        bus.subscribe(NoteWritten, other, durable=True, name="notes.on_written")
    with pytest.raises(ValueError, match=r"a key of .*on_written already"):
        bus.subscribe(NoteWritten, other, formerly=["notes.on_written"])
    with pytest.raises(ValueError, match="one handler has one name"):
        bus.subscribe(NoteWrittenAgain, on_written, durable=True, name="notes.renamed")
    with pytest.raises(ValueError, match="cannot be empty"):
        bus.subscribe(NoteWritten, other, durable=True, name="")
    with pytest.raises(ValueError, match=r"A former name .* at most 255"):
        bus.subscribe(NoteWritten, other, formerly=["x" * 256])

    bus.subscribe(NoteWrittenAgain, on_written, durable=True, name="notes.on_written")
    bus.subscribe(  # Its own key and a repeat are not former ones.
        NoteWritten, other, durable=True, formerly=[f"{__name__}.other", "a", "a"]
    )
    assert [key.key for key in bus.handler_keys() if key.handler == other] == [
        f"{__name__}.other",
        "a",
    ]


@pytest.mark.usefixtures("tables")
async def test_a_renamed_handler_runs_what_was_queued_under_its_old_key(
    watched: kit.Outbox,
) -> None:
    """New events go under the name; the old key's messages still run."""
    on_written.__qualname__ = "on_written_before"  # As it was named, then.
    try:
        event_bus.subscribe(NoteWritten, on_written, durable=True)
        before = await write_note()
    finally:
        on_written.__qualname__ = "on_written"
    event_bus.unsubscribe(NoteWritten, on_written)
    event_bus.subscribe(
        NoteWritten,
        on_written,
        durable=True,
        name="notes.on_written",
        formerly=[OLD_KEY],
    )
    after = await write_note()

    assert await targets() == [OLD_KEY, "notes.on_written"]
    assert watched.assert_queued(on_written, note_id=str(after)).target == (
        "notes.on_written"
    )
    await Worker().run_once()
    assert sorted(seen) == sorted([before, after])
    assert await statuses() == [DONE, DONE]


@pytest.mark.usefixtures("tables")
async def test_a_handler_made_in_process_drains_its_durable_messages() -> None:
    """In-process now, it still runs what was queued for it when it was durable."""
    event_bus.subscribe(NoteWritten, on_written, durable=True, name="notes.durable")
    queued = await write_note()
    event_bus.unsubscribe(NoteWritten, on_written)
    event_bus.subscribe(NoteWritten, on_written, formerly=["notes.durable"])

    await write_note()  # Run in-process at once: nothing queued.
    assert await targets() == ["notes.durable"]
    assert len(seen) == 1
    await Worker().run_once()

    assert seen[-1] == queued
    assert await statuses() == [DONE]


@pytest.mark.usefixtures("messaging")
def test_unnamed_durable_handlers_are_warned_about_where_deployed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``messaging.W002``, once per handler; a named one, and former keys, are not."""
    event_bus.subscribe(NoteWritten, other, durable=True)
    event_bus.subscribe(DomainEvent, other, durable=True)
    event_bus.subscribe(
        NoteWritten, on_written, durable=True, name="notes.on", formerly=[OLD_KEY]
    )
    config = MessagingConfig.__new__(MessagingConfig)
    assert config.unnamed_handlers() == []
    # The test's environment, counted as deployed: production's own checks
    # would refuse the test database's settings.
    monkeypatch.setattr(
        apps_module, "DEPLOYED_ENVIRONMENTS", frozenset({settings.ENVIRONMENT})
    )

    found = [each for each in config.unnamed_handlers() if __name__ in each.message]

    key = f"{__name__}.other"
    assert [each.id for each in found] == ["messaging.W002"]
    assert found[0].message == (
        f"The durable handler {key!r} is named after its module's path: moving "
        "or renaming it, or making it in-process, renames its messages, which "
        "then wait for a handler of the old name and are dead-lettered after "
        "MESSAGING.UNKNOWN_TARGET_GRACE."
    )
    assert found[0].hint == (
        "Give it a name of its own: @handles(..., durable=True, name=...), with "
        f"formerly=[{key!r}] for the messages queued under this one."
    )
