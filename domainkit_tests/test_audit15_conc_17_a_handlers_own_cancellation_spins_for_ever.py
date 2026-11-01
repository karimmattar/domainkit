"""CONC-17: a CancelledError the handler raises is retried at once, uncounted, for ever.

``Worker.process`` takes every ``asyncio.CancelledError`` out of a run for
the worker stopping: the message goes back, due at once, its attempt not
counted (``_interrupted``), and the error is re-raised. But the worker's
task was not cancelled when the handler itself raised it: a handler that
awaits a task or future something else cancelled (a shared computation in
user code, a library that cancels its own sub-task on a closed connection)
gets a ``CancelledError`` while nobody cancels the worker. The task then
ends "cancelled", which ``TaskGroup`` ignores; the loop claims the message
again at once, and so on: the message never spends an attempt, never
dead-letters, never backs off, and the worker spins on it (a hot loop, one
claim and one release per turn, with a warning-free log). The worker
should tell the two apart (``asyncio.current_task().cancelling()``, as
``Budget.acquire`` already does) and count the handler's as a failure.
"""

import asyncio

import pytest

from domainkit.contrib.messaging import Worker, enqueue, task
from domainkit.contrib.messaging import registry as tasks
from domainkit.contrib.messaging.tables import DEAD
from domainkit_tests.test_messaging import messaging, outbox, tables  # noqa: F401

pytestmark = pytest.mark.anyio

GUARD = 20
"""Runs after which the task stops failing, so the test ends on d126394."""


@pytest.mark.usefixtures("tables")
async def test_a_cancelled_error_from_the_handler_spends_attempts() -> None:
    """It is a failure of the run: retried with attempts counted, then dead."""
    runs: list[int] = []

    @task(name="tests.audit15.cancels_itself", max_attempts=3)
    async def cancels_itself() -> None:
        runs.append(len(runs) + 1)
        if len(runs) < GUARD:
            inner = asyncio.ensure_future(asyncio.sleep(10))
            inner.cancel()
            await inner  # A sub-task something else cancelled.

    try:
        await enqueue(cancels_itself)
        async with asyncio.timeout(20):
            await Worker().run_once()
    finally:
        tasks._tasks.pop("tests.audit15.cancels_itself")

    (row,) = await outbox()
    # Safe behaviour: three counted attempts, then dead. On d126394 it ran
    # GUARD times back to back with attempts never above 1, and was "done"
    # only because the guard stopped the failure.
    assert (len(runs), row["status"], row["attempts"]) == (3, DEAD, 3)
