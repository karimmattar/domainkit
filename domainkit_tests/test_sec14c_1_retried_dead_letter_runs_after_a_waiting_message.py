"""SEC-14c-1: a retried dead letter runs after its key's next waiting message.

The docs (messaging, "A dead letter lets the key go on") and ADR 0118 say
``messages --retry`` puts a dead message "back in its place, held before
the key's messages still waiting, after the one running if there is one".
``ordering.requeue`` decides with ``first_status``: any *pending* message of
the key, running or not, makes the retried one ``held``. So a key's next
message, promoted when the first died but not started yet (no worker free,
a delayed retry, a rate), runs before the older message given back:
``authorized`` arrives after ``captured``.
"""

import pytest

from domainkit.apps import registry
from domainkit.contrib.messaging import MessagingSettings, Worker, admin, enqueue
from domainkit.contrib.messaging.tables import DEAD, PENDING
from domainkit_tests.test_messaging import (  # noqa: F401 - fixtures.
    immediate_retries,
    messaging,
    tables,
)
from domainkit_tests.test_messaging_ordering import (
    config,
    doomed,
    runs,
    statuses,
    step,
)

pytestmark = pytest.mark.anyio


@pytest.mark.usefixtures("tables", "immediate_retries")
async def test_a_retried_dead_letter_runs_before_the_keys_waiting_messages() -> None:
    """``a1`` dies, ``a2`` is promoted but not started; retried, ``a1`` runs first."""
    runs.clear()
    runs.broken.add("a1")
    await enqueue(doomed, key="a", label="a1")  # max_attempts=2
    await enqueue(step, key="a", label="a2")
    worker = Worker(config())
    for _ in range(2):  # Both attempts of a1; a2 is held meanwhile.
        (claimed,) = await worker.claim(1)
        assert claimed.ordering_key == "a"
        await worker.process(claimed)
    assert await statuses("a") == [DEAD, PENDING]  # a2 waits, nobody runs it.
    runs.broken.clear()

    settings = registry.settings(MessagingSettings)
    assert await admin.retry_dead(settings, None) == 1
    await worker.run_once()

    # Safe behaviour (as documented): a1 is put back before a2, still waiting.
    assert runs.labels("a") == ["a1", "a2"]
