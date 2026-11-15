"""``raise RunLater(after=...)``: "check again later" is not a failure (user report W6).

A task that polls a provider which has not decided yet raised an error to
be retried: each check was a warning with a traceback, an attempt spent,
and, past ``max_attempts``, a dead letter and a report.
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import select, update

from domainkit.contrib.messaging import RunLater, Worker, enqueue, task
from domainkit.contrib.messaging.retry import MAX_DELAY
from domainkit.contrib.messaging.tables import DONE, PENDING, messages
from domainkit.db import connections
from domainkit.ddd import event_bus
from domainkit_tests.test_db import Note, NoteId, NotesUnitOfWork, NoteWritten
from domainkit_tests.test_messaging import (
    immediate_retries,
    messaging,
    reported,
    tables,
)

__all__ = ["immediate_retries", "messaging", "tables"]  # The fixtures the tests take.

pytestmark = pytest.mark.anyio

checks: list[str] = []


@task(name="tests.run_later.recheck", max_attempts=2, retry_on=(KeyError,))
async def recheck(session: str, pending: int) -> None:
    """Wait for a provider that answers after ``pending`` checks."""
    checks.append(session)
    if len(checks) <= pending:
        raise RunLater(
            after=timedelta(minutes=1), reason="the provider has not decided"
        )


async def on_written(event: NoteWritten) -> None:
    """A durable handler whose world is not ready the first time."""
    checks.append(str(event.note_id))
    if len(checks) == 1:
        raise RunLater(after=30)


@pytest.fixture(autouse=True)
def fresh() -> None:
    """Each test counts its own checks."""
    checks.clear()


async def the_row() -> dict[str, Any]:
    """The outbox's only row."""
    async with connections.engine().connect() as connection:
        return dict((await connection.execute(select(messages))).one()._mapping)


async def make_due() -> None:
    """Make every pending message due now, as time passing would."""
    async with connections.engine().begin() as connection:
        await connection.execute(
            update(messages).values(available_at=datetime.now(UTC))
        )


def later_count() -> float:
    """``messaging_messages_total`` of the ``later`` outcome, for tasks."""
    sample = REGISTRY.get_sample_value(
        "messaging_messages_total", {"kind": "task", "outcome": "later"}
    )
    return sample or 0.0


@pytest.mark.usefixtures("tables")
async def test_a_run_later_keeps_its_attempt_and_says_so_quietly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Pending again, due then, no attempt spent: an INFO line, no report."""
    before = later_count()
    await enqueue(recheck, session="s-1", pending=1)
    caplog.set_level(logging.INFO, logger="domainkit.contrib.messaging.worker")

    await Worker().run_once()

    row = await the_row()
    assert (row["status"], row["attempts"], row["last_error"]) == (PENDING, 0, None)
    wait = (row["available_at"] - datetime.now(UTC)).total_seconds()
    assert 55 < wait <= 60
    (line,) = [r for r in caplog.records if "runs again" in r.getMessage()]
    assert (line.levelno, line.exc_info) == (logging.INFO, None)
    assert line.getMessage() == (
        "Task tests.run_later.recheck runs again in 60s: the provider has not decided"
    )
    assert reported == []
    assert later_count() == before + 1


@pytest.mark.usefixtures("tables")
async def test_more_checks_than_attempts_never_dead_letter() -> None:
    """Thirteen checks of a task allowed two attempts; then it is done."""
    await enqueue(recheck, session="s-2", pending=12)

    for _ in range(13):
        await Worker().run_once()
        await make_due()

    assert len(checks) == 13
    row = await the_row()
    assert (row["status"], row["attempts"]) == (DONE, 1)
    assert reported == []


@pytest.mark.usefixtures("tables")
async def test_a_durable_handler_runs_later_too() -> None:
    """The same for an event's handler."""
    event_bus.subscribe(NoteWritten, on_written, durable=True, name="tests.later")
    try:
        note = Note(id=NoteId(uuid.uuid4()), text="t")
        note.record(NoteWritten(note_id=note.id))
        async with NotesUnitOfWork() as uow:
            await uow.notes.add(note)
            await uow.commit()

        await Worker().run_once()
        assert ((await the_row())["status"], len(checks)) == (PENDING, 1)
        await make_due()
        await Worker().run_once()
    finally:
        event_bus.unsubscribe(NoteWritten, on_written)

    assert ((await the_row())["status"], len(checks)) == (DONE, 2)


def test_run_later_takes_a_delay_it_can_keep() -> None:
    """More than nothing, at most a year; seconds or a timedelta."""
    assert RunLater(after=timedelta(minutes=2)).after == 120.0
    assert RunLater(after=5).after == 5.0
    assert str(RunLater(after=5)) == "Run again in 5s."
    for wrong in (0, -1, float("inf"), float("nan"), MAX_DELAY + 1):
        with pytest.raises(ValueError, match="more than 0 and at most"):
            RunLater(after=wrong)
