"""Dead letters go: ``messages drop``, and ``DEAD_RETENTION_SECONDS`` (user report W4).

Mail, text messages, and push notifications are sensitive tasks: their
arguments are blanked when they die, so ``messages retry`` cannot run
them, and ``cancel`` takes only waiting messages. Before 2.4 nothing
removed a dead letter, so the ``dead`` gauge, and an alert on it, never
went back to zero.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import update

from domainkit.apps import registry
from domainkit.clock import freeze_time
from domainkit.contrib.messaging import (
    MessagingSettings,
    Worker,
    admin,
    enqueue,
    periodic,
)
from domainkit.contrib.messaging import registry as tasks
from domainkit.contrib.messaging.tables import DEAD, DONE, messages
from domainkit.db import connections
from domainkit.exceptions import ImproperlyConfigured
from domainkit.management import CommandError
from domainkit_tests.test_messaging import (
    FAST,
    always_fails,
    immediate_retries,
    messaging,
    outbox,
    remember,
    run,
    sends_a_code,
    tables,
)

__all__ = ["immediate_retries", "messaging", "tables"]  # The fixtures the tests take.

pytestmark = pytest.mark.anyio


def config(**options: object) -> MessagingSettings:
    """``FAST`` with ``options``."""
    return MessagingSettings(**{**FAST, **options})


async def run_to_their_end() -> dict[str, uuid.UUID]:
    """A task that fails, a sensitive one that fails, one that works; ids by target."""
    await enqueue(always_fails)
    await enqueue(sends_a_code, to="+33612345678", code="493021")
    await enqueue(remember, value=uuid.uuid4())
    await Worker().run_once()
    return {str(row["target"]): row["id"] for row in await outbox()}  # type: ignore[misc]


async def statuses() -> dict[str, str]:
    """Each row's status, by target."""
    return {str(row["target"]): str(row["status"]) for row in await outbox()}


async def died(seconds_ago: float) -> None:
    """Make every dead message have died ``seconds_ago``."""
    when = datetime.now(UTC) - timedelta(seconds=seconds_ago)
    async with connections.engine().begin() as connection:
        await connection.execute(
            update(messages).where(messages.c.status == DEAD).values(finished_at=when)
        )


@pytest.mark.usefixtures("tables")
async def test_drop_deletes_dead_messages_and_nothing_else() -> None:
    """``--blanked`` first takes those a retry cannot run; a done one stays."""
    ids = await run_to_their_end()
    done = next(each for target, each in ids.items() if "remember" in target)
    settings = registry.settings(MessagingSettings)

    assert await admin.drop_dead(settings, None, blanked_only=True) == 1
    assert "tests.sends_a_code" not in await statuses()
    assert await admin.drop_dead(settings, done) == 0
    assert await admin.drop_dead(settings, ids["tests.always_fails"]) == 1

    assert list((await statuses()).values()) == [DONE]


@pytest.mark.usefixtures("tables")
async def test_dead_messages_stay_unless_a_retention_is_set() -> None:
    """``None``, the default, keeps them; a retention deletes those that died before."""
    await run_to_their_end()
    await died(30 * 86400)

    assert await Worker(config()).cleanup() == 0
    assert list((await statuses()).values()).count(DEAD) == 2

    keeping = Worker(config(DEAD_RETENTION_SECONDS=31 * 86400))
    assert await keeping.cleanup() == 0  # Dead for less than the retention.
    assert await Worker(config(DEAD_RETENTION_SECONDS=86400)).cleanup() == 2

    assert DEAD not in (await statuses()).values()


@pytest.mark.usefixtures("tables")
async def test_a_long_slots_dead_run_is_kept_until_the_slot_ends() -> None:
    """Its row is what stops workers from queuing the slot again (CONC-13)."""

    @periodic(every=timedelta(days=30), name="tests.dead_letters.monthly")
    async def monthly() -> None: ...

    seconds = 30 * 86400
    slot = int(datetime(2026, 11, 1, tzinfo=UTC).timestamp() / seconds)
    start = datetime.fromtimestamp(slot * seconds, UTC)
    try:
        with freeze_time(start + timedelta(hours=1)) as clock:
            worker = Worker(config(DEAD_RETENTION_SECONDS=86400))
            await worker.run_once()
            async with connections.engine().begin() as connection:
                await connection.execute(update(messages).values(status=DEAD))
            clock.tick(timedelta(days=2))
            assert await worker.cleanup() == 0
            clock.move_to(start + timedelta(days=30))  # The slot has ended.
            assert await worker.cleanup() == 1
    finally:
        tasks.unregister_task("tests.dead_letters.monthly")


def test_the_dead_statement_needs_a_retention() -> None:
    """Cleanup never builds it without one; a caller of its own is told why."""
    with pytest.raises(ImproperlyConfigured, match="DEAD_RETENTION_SECONDS"):
        Worker(config()).dead_statement("sqlite", 10)
    with pytest.raises(ValidationError, match="DEAD_RETENTION_SECONDS"):
        config(DEAD_RETENTION_SECONDS=0)


def test_messages_drop_says_what_went_and_why_one_did_not(messaging: None) -> None:
    """One id or ``all``, ``--blanked`` or not; refused ids say what to do instead."""

    async def prepare() -> dict[str, uuid.UUID]:
        async with connections.engine().begin() as connection:
            await connection.run_sync(messages.create)
        ids = await run_to_their_end()
        await connections.close_all()
        return ids

    ids = asyncio.run(prepare())
    failed = ids["tests.always_fails"]

    with pytest.raises(CommandError) as refused:
        run("messages", "drop", str(failed), "--blanked")
    assert str(refused.value) == (
        f"{failed} was not dropped: it is not dead with its arguments blanked, or "
        "not in this outbox. A waiting message is cancelled: messages cancel ID."
    )
    assert run("messages", "drop", "all", "--blanked") == "Dropped 1 dead message(s).\n"
    assert run("messages", "drop", str(failed)) == "Dropped 1 dead message(s).\n"
    with pytest.raises(CommandError, match="it is not dead, or not in this outbox"):
        run("messages", "drop", str(failed))
    with pytest.raises(CommandError, match="not a message id or 'all'"):
        run("messages", "drop", "x")
    assert run("messages", "drop", "all") == "Dropped 0 dead message(s).\n"
    assert "dead     0\n" in run("messages", "status")
