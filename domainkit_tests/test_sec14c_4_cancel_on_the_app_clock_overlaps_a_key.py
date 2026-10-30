"""SEC-14c-4: ``messages --cancel`` judges "running" by its own clock, not the lock's.

On PostgreSQL the worker's lock is set by the database's clock (ADR 0043,
``dbclock``), but ``admin.cancel`` decides whether the message is running
with ``clock.now()`` of the process that cancels. When that host's clock
runs ahead of the database's by more than what is left of the lease (at
least ``VISIBILITY_TIMEOUT - TASK_TIMEOUT``, 60 s by default), the running
first message of a key is cancelled, and 0.14's cancel then promotes the
key's next message: two messages of one key run at once, the later one
possibly finishing first. (The running one's outcome is then refused, so
it also runs to its end unrecorded.) The safe behaviour: the database's
clock decides, as it does for the worker's claim.
"""

from datetime import timedelta

import pytest

from domainkit import clock
from domainkit.apps import registry
from domainkit.contrib.messaging import MessagingSettings, Worker, admin, enqueue
from domainkit.contrib.messaging.tables import HELD, PENDING
from domainkit.db import connections
from domainkit.testing import freeze_time
from domainkit_tests.test_messaging import SLOW, messaging, tables  # noqa: F401
from domainkit_tests.test_messaging_ordering import config, statuses, step

pytestmark = pytest.mark.anyio


@pytest.mark.usefixtures("tables")
async def test_a_running_keyed_message_is_not_cancelled_by_a_fast_clock() -> None:
    """The cancelling host's clock is 5 minutes ahead of PostgreSQL's."""
    if connections.engine().dialect.name != "postgresql":
        pytest.skip("SQLite's locks use the application's clock")
    for label in ("a0", "a1"):
        await enqueue(step, key="a", label=label)
    worker = Worker(config(**SLOW))  # VISIBILITY_TIMEOUT 60 s.
    (running,) = await worker.claim(1)  # a0 runs, locked by the database's clock.
    settings = registry.settings(MessagingSettings)

    with freeze_time(clock.now() + timedelta(minutes=5)):
        cancelled = await admin.cancel(settings, running.id)

    # Safe behaviour: a0 runs on, a1 stays held. On ac71361, a0 is cancelled
    # and a1 made pending: a worker starts it while a0 still runs.
    assert not cancelled
    assert await statuses("a") == [PENDING, HELD]
