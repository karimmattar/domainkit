"""SEC-14c-3: a key whose first message left the line unpromoted stalls forever.

Only the transaction that finishes, kills, or cancels a key's message makes
the next one ``pending`` (``ordering.promote``); nothing else ever does. A
first message that finishes any other way leaves the key with ``held``
messages and no ``pending`` one, and from then on:

- no worker claims or relays the held ones;
- every new message of the key is ``held`` behind them (``first_status``
  counts held ones as busy);
- cleanup, ``messages --retry`` and the relay never repair it;
- the only operator action is ``messages --cancel`` of a held message,
  which loses it.

The realistic way in is the documented upgrade: ``migrate``, then deploy.
During a rolling deploy, 0.14 web processes write keyed messages (the first
pending, the rest held) while 0.13 workers still run: a 0.13 worker records
the first as done with 0.13's statement (below, verbatim) and promotes
nothing. Every key used during the overlap is stuck after the deploy, with
no error and no lag on the ``pending`` gauges.
"""

import pytest
from sqlalchemy import update

from domainkit.contrib.messaging import Worker, dbclock, enqueue
from domainkit.contrib.messaging.tables import DONE, HELD, messages
from domainkit_tests.test_messaging import messaging, tables  # noqa: F401
from domainkit_tests.test_messaging_ordering import (
    config,
    drain,
    runs,
    statuses,
    step,
)

pytestmark = pytest.mark.anyio


async def done_by_a_0_13_worker(worker: Worker) -> None:
    """Claim the key's first message and record it as 0.13's ``_done`` did."""
    (message,) = await worker.claim(1)
    async with worker.engine.begin() as connection:  # 0.13.0, Worker._done.
        await connection.execute(
            update(messages)
            .where(messages.c.id == message.id, messages.c.lock_id == message.lock_id)
            .values(
                locked_until=None,
                lock_id=None,
                status=DONE,
                finished_at=dbclock.now(worker.dialect),
                payload={},
            )
        )


@pytest.mark.usefixtures("tables")
async def test_a_key_whose_first_was_finished_without_promotion_runs_again() -> None:
    """After the overlap, 0.14 workers alone must still run the key's messages."""
    runs.clear()
    for label in ("a0", "a1", "a2"):
        await enqueue(step, key="a", label=label)
    old = Worker(config())
    await done_by_a_0_13_worker(old)
    assert await statuses("a") == [DONE, HELD, HELD]

    # The deploy is over: only 0.14 workers run, and the key gets more work.
    await enqueue(step, key="a", label="a3")
    new = Worker(config())
    await drain(new)
    await new.cleanup()
    await new.prune_keys()
    await drain(new)

    # Safe behaviour: the key resumes; on ac71361 it is held forever.
    assert runs.labels("a") == ["a1", "a2", "a3"]
