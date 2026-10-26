"""SEC-11b-1: an inbox task held back by its rate comes back after its row is pruned.

The inbox forgets a handled message after ``INBOX_RETENTION_SECONDS``, by age
alone. ``messaging.E001`` checks that retention against a "redelivery
window" made of attempts, retry delays, and leases; but a task over its
``rate`` goes back to the outbox without spending an attempt, one interval
later for each message held back before it. A ``1/day`` task with a backlog
of eight is held for eight days; a message of it whose run committed, and
whose outcome was lost (the case the inbox exists for), then runs again
after its inbox row was pruned, and commits its work a second time. The
check finds nothing to refuse.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from domainkit import clock
from domainkit.contrib.messaging import enqueue, inbox, task
from domainkit.contrib.messaging.tables import DONE, messages
from domainkit.db import connections
from domainkit_tests.test_messaging import (  # noqa: F401 - fixtures.
    messaging,
    tables,
)
from domainkit_tests.test_messaging_inbox import (
    credits,
    failing_once,
    inbox_rows,
    slow_worker,
    write_credit,
)

pytestmark = pytest.mark.anyio
KEY = "tests.sec11b.rated_credit"


@task(name=KEY, rate="1/day")
@inbox
async def rated_credit(number: int) -> None:
    """A rate-limited task whose whole effect is one commit, under the inbox."""
    await write_credit()


async def statuses() -> list[tuple[str, int]]:
    """Each message's status and attempts."""
    async with connections.engine().connect() as connection:
        rows = await connection.execute(select(messages.c.status, messages.c.attempts))
        return sorted((row.status, row.attempts) for row in rows)


@pytest.mark.usefixtures("tables")
async def test_a_held_back_message_commits_once_whatever_its_wait() -> None:
    """Eight messages of a 1/day task, one of which committed but lost its outcome."""
    start = datetime.now(UTC).replace(microsecond=0)
    with clock.freeze_time(start) as frozen:
        for number in range(8):
            await enqueue(rated_credit, number=number)
        worker = slow_worker()
        failing_once(worker)  # The run that commits loses its outcome (a crash).

        # Day 0: one runs and commits, the other seven are held a day apart.
        assert await worker.run_once() == 8
        assert await credits() == 1
        (handled,) = await inbox_rows()

        # Its lease runs out; claimed again, it is held behind the seven.
        frozen.tick(61)
        assert await worker.run_once() == 1
        assert await credits() == 1

        # Days 1 to 7: the seven run, one a day; the inbox is pruned as usual.
        for day in range(1, 8):
            frozen.move_to(start + timedelta(days=day, minutes=5))
            await worker.run_once()
            await worker.prune_inbox()
        assert await credits() == 8
        assert handled not in await inbox_rows()  # Pruned: older than 7 days.

        # Day 8: the held-back message comes due, and runs again.
        frozen.move_to(start + timedelta(days=8, minutes=5))
        await worker.run_once()

    assert await statuses() == [(DONE, 1)] * 7 + [(DONE, 2)]
    assert await credits() == 8, "the committed message committed its work again"
