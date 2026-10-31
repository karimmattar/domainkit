"""On PostgreSQL the outbox goes by the database's clock, whatever the host's says.

Hosts that share an outbox share its rows' times (ADR 0043, ``dbclock``):
what a worker, a writer, or an operator writes and judges is by
PostgreSQL's ``statement_timestamp()``. Each test runs the host's clock an
hour off, not frozen (a frozen clock decides on every database).
PostgreSQL only: SQLite's clock is the host's.
"""

import os
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Final

import pytest
from sqlalchemy import func, select, update

from domainkit import clock
from domainkit.apps import registry
from domainkit.contrib.messaging import MessagingSettings, Worker, admin, enqueue
from domainkit.contrib.messaging.tables import DEAD, HELD, PENDING, messages
from domainkit.contrib.messaging.tables import ordering_keys as keys
from domainkit.db import connections
from domainkit_tests.conftest import POSTGRES_VARIABLE
from domainkit_tests.test_messaging import (  # noqa: F401 - fixtures.
    FAST,
    immediate_retries,
    messaging,
    remember,
    tables,
)
from domainkit_tests.test_messaging_ordering import config, statuses, step
from domainkit_tests.test_messaging_ordering_repairs import dead_then_next

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.usefixtures("tables"),
    pytest.mark.skipif(
        not os.environ.get(POSTGRES_VARIABLE), reason=f"set {POSTGRES_VARIABLE}"
    ),
]
HOUR: Final = timedelta(hours=1)
NEAR: Final = timedelta(minutes=5)
"""How far apart two readings of one clock may be, however slow the test runs."""

type Skew = Callable[[timedelta], None]


@pytest.fixture
def skew(monkeypatch: pytest.MonkeyPatch) -> Skew:
    """Set how far the host's clock runs from the real one (none to start with)."""
    real = clock.now
    off = [timedelta(0)]
    monkeypatch.setattr(clock, "now", lambda: real() + off[0])

    def set_off(by: timedelta) -> None:
        off[0] = by

    return set_off


async def database_now() -> datetime:
    """PostgreSQL's clock."""
    async with connections.engine().connect() as connection:
        moment = await connection.scalar(select(func.statement_timestamp()))
    assert isinstance(moment, datetime)
    return moment


async def stored(column: str) -> datetime:
    """The time ``column`` of the only message holds."""
    async with connections.engine().connect() as connection:
        moment = await connection.scalar(select(messages.c[column]))
    assert isinstance(moment, datetime)
    return moment


async def stamped_now(column: str) -> bool:
    """Whether ``column`` of the only message is PostgreSQL's now, near enough."""
    return abs(await stored(column) - await database_now()) < NEAR


async def test_a_finished_message_is_stamped_by_the_database(skew: Skew) -> None:
    """``finished_at`` is PostgreSQL's time, not the worker's an hour ahead."""
    await enqueue(remember, value=uuid.uuid4())
    skew(HOUR)
    worker = Worker(MessagingSettings(**FAST))

    (claimed,) = await worker.claim(1)
    await worker.process(claimed)

    assert await stamped_now("finished_at")


async def test_a_cancel_is_stamped_by_the_database(skew: Skew) -> None:
    """An operator's host an hour ahead cancels at PostgreSQL's time."""
    await enqueue(remember, value=uuid.uuid4())
    async with connections.engine().connect() as connection:
        message_id = await connection.scalar(select(messages.c.id))
    assert isinstance(message_id, uuid.UUID)
    skew(HOUR)

    assert await admin.cancel(registry.settings(MessagingSettings), message_id)
    assert await stamped_now("finished_at")


async def test_a_retried_dead_letter_is_due_by_the_database(skew: Skew) -> None:
    """Given back by a host an hour ahead, it is due now, not in an hour."""
    await enqueue(remember, value=uuid.uuid4())
    async with connections.engine().begin() as connection:
        await connection.execute(update(messages).values(status=DEAD))
    skew(HOUR)

    assert await admin.retry_dead(registry.settings(MessagingSettings), None) == 1
    assert await stamped_now("available_at")


async def test_a_keys_turn_is_stamped_by_the_database(skew: Skew) -> None:
    """The key's row says when PostgreSQL saw it written."""
    skew(HOUR)
    await enqueue(step, key="a", label="a0")

    async with connections.engine().connect() as connection:
        touched = await connection.scalar(select(keys.c.touched_at))
    assert isinstance(touched, datetime)
    assert abs(touched - await database_now()) < NEAR


async def test_a_key_written_just_now_is_kept_by_a_host_ahead(skew: Skew) -> None:
    """Idle for ``RETENTION_SECONDS`` by PostgreSQL's clock, not by the host's."""
    await enqueue(step, key="a", label="a0")
    async with connections.engine().begin() as connection:
        await connection.execute(update(messages).values(status="done"))
    skew(HOUR)

    assert await Worker(config(RETENTION_SECONDS=1800)).prune_keys() == 0
    async with connections.engine().connect() as connection:
        assert await connection.scalar(select(func.count()).select_from(keys)) == 1


@pytest.mark.usefixtures("immediate_retries")
async def test_a_message_held_for_the_broker_keeps_its_turn(skew: Skew) -> None:
    """Its relay's lock is alive by PostgreSQL's clock: the dead letter waits."""
    await dead_then_next(lock=timedelta(minutes=30), started=False)
    skew(HOUR)

    assert await admin.retry_dead(registry.settings(MessagingSettings), None) == 1
    assert await statuses("a") == [HELD, PENDING]


async def test_a_keyed_message_written_ahead_is_due_at_once(skew: Skew) -> None:
    """A writer an hour ahead writes a message of a key due by PostgreSQL's clock."""
    skew(HOUR)
    await enqueue(step, key="a", label="a0")
    skew(timedelta(0))

    assert len(await Worker(config()).claim(1)) == 1


async def locked_for(minutes: int) -> None:
    """Lock the only message, started, until ``minutes`` from PostgreSQL's now."""
    await enqueue(remember, value=uuid.uuid4())
    now = func.statement_timestamp()
    async with connections.engine().begin() as connection:
        await connection.execute(
            update(messages).values(
                locked_until=now + timedelta(minutes=minutes),
                lock_id=uuid.uuid4(),
                started_at=now,
            )
        )


async def test_a_lease_alive_by_the_database_is_listed_by_a_host_ahead(
    skew: Skew,
) -> None:
    """The earlier of the two clocks judges a lease: here, PostgreSQL's."""
    await locked_for(30)
    skew(HOUR)

    listed = await admin.list_messages(
        registry.settings(MessagingSettings), "locked", 5
    )

    assert [each.target for each in listed] == [
        "domainkit_tests.test_messaging.remember"
    ]


async def test_a_lease_alive_by_the_hosts_clock_is_listed_too(skew: Skew) -> None:
    """A host an hour behind still sees the lease its clock says is alive."""
    await locked_for(-30)
    skew(-HOUR)

    listed = await admin.list_messages(
        registry.settings(MessagingSettings), "locked", 5
    )

    assert [each.target for each in listed] == [
        "domainkit_tests.test_messaging.remember"
    ]


async def test_lag_counts_what_is_due_by_the_database(skew: Skew) -> None:
    """A message due in half an hour has no lag, however far ahead the host is."""
    await enqueue(remember, value=uuid.uuid4(), delay=timedelta(minutes=30))
    skew(HOUR)

    assert await admin.queue_lag(registry.settings(MessagingSettings)) == {}
