"""CONC-14: a claim cut off as it commits leaves its messages locked, an attempt spent.

The relay knows that "the driver may commit the lease as its caller is
cancelled" (aiosqlite commits in its own thread; a PostgreSQL ``COMMIT``
may finish on the server after the caller is cancelled), and gives such a
lease back (``Worker._relay``, ``_unlock``, 0.12.1). The polling worker's
``claim``, and a consumer's ``take_over``, do not: a stop that lands then
(``runworker``'s second signal, or the task running ``Worker.run`` being
cancelled by the code that embeds it) leaves the claimed messages locked
for ``VISIBILITY_TIMEOUT`` (300 s) with ``attempts`` already counted. A
keyed message holds its whole key for that time, and one claimed on its
last attempt is dead-lettered as ``AttemptsExhausted`` without ever
running.

The test commits the claim, then holds the claiming task until it is
cancelled, as ``test_shutdown_gives_back_a_lease_cut_off_as_it_commits``
does for the relay.
"""

import asyncio
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncTransaction

from domainkit.contrib.messaging import Worker, enqueue
from domainkit_tests.test_messaging import (  # noqa: F401
    messaging,
    outbox,
    remember,
    tables,
)

pytestmark = pytest.mark.anyio


@pytest.mark.usefixtures("tables")
async def test_a_claim_cancelled_as_it_commits_gives_its_messages_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The claimed message is unlocked, its attempt not counted, as the relay does."""
    await enqueue(remember, value=uuid.uuid4())
    exit_transaction = AsyncTransaction.__aexit__
    committed = asyncio.Event()

    async def committed_then_held(self: AsyncTransaction, *exc: object) -> None:
        await exit_transaction(self, *exc)
        task = asyncio.current_task()
        if task is not None and task.get_name() == "claiming":
            committed.set()
            await asyncio.sleep(5)  # Cancelled: the stop lands now.

    monkeypatch.setattr(AsyncTransaction, "__aexit__", committed_then_held)
    worker = Worker()
    claiming = asyncio.create_task(worker.claim(10), name="claiming")
    async with asyncio.timeout(5):
        await committed.wait()
    claiming.cancel()
    with pytest.raises(asyncio.CancelledError):
        await claiming
    monkeypatch.undo()
    await asyncio.sleep(0.2)  # Let any give-back finish.

    (row,) = await outbox()
    # Safe behaviour: given back, due at once, the attempt not spent; on
    # d126394 it stays locked for VISIBILITY_TIMEOUT with attempts == 1.
    assert (row["lock_id"], row["locked_until"], row["attempts"]) == (None, None, 0)
