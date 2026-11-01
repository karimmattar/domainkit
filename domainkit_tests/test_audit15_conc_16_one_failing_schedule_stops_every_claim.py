"""CONC-16: a periodic task the outbox cannot store stops the worker claiming anything.

``Worker.run`` queues periodic and cron runs (``schedule_periodic``) at the
top of each round, and a failure there sends the round to the outage pause
and ``continue``: the claim below it never runs. A failure that is not an
outage but permanent therefore stops the home outbox's worker for good,
for every task and handler, while it logs "Could not reach the outbox" and
"trying again" forever. One way in: a task name that fits the outbox's
``target`` (255 characters) but whose dedupe key, ``periodic:<name>:<slot>``
(or ``cron:<name>:<timestamp>``), does not; nothing checks either length
when the task registers, and SQLite does not enforce them, so it shows only
on PostgreSQL. Scheduling should be isolated from claiming (one bad
schedule logged and skipped), and keys checked when tasks register.
"""

import asyncio
import os
import uuid
from datetime import timedelta

import pytest

from domainkit.contrib.messaging import Worker, enqueue, periodic
from domainkit.contrib.messaging import registry as tasks
from domainkit_tests.conftest import POSTGRES_VARIABLE
from domainkit_tests.test_messaging import (  # noqa: F401
    messaging,
    ran,
    remember,
    tables,
)

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        not os.environ.get(POSTGRES_VARIABLE), reason=f"set {POSTGRES_VARIABLE}"
    ),
]

NAME = "tests.audit15." + "x" * 226  # 240 characters: a valid target.


@pytest.mark.usefixtures("tables")
async def test_a_schedule_that_cannot_be_written_does_not_stop_other_messages() -> None:
    """The worker still runs the tasks that are due."""

    @periodic(every=timedelta(hours=1), name=NAME)
    async def hourly() -> None:
        """Never reached."""

    value = uuid.uuid4()
    await enqueue(remember, value=value)
    stop = asyncio.Event()
    worker = Worker()
    try:
        running = asyncio.create_task(worker.run(stop))
        await asyncio.sleep(3)  # Three poll intervals, and more.
        stop.set()
        async with asyncio.timeout(10):
            await running
    finally:
        tasks._tasks.pop(NAME)

    # Safe behaviour: the due task ran; on d126394 nothing ever is claimed.
    assert [entry[0] for entry in ran] == [value]
