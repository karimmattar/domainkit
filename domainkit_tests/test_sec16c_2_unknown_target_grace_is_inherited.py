"""SEC-16c-2: the unknown-target first sighting outlives the message it was about.

CONC-15 keeps a message for a task no worker knows yet, for
``UNKNOWN_TARGET_GRACE`` from the header ``unknown_target_since``. That
header stays on the row when the message runs, and is copied into the next
step of its chain (``_follow_up`` copies every header) and kept by
``messages retry``. So the next step, or a retried dead letter, is
dead-lettered at once by an older worker during a rolling deploy: no grace,
and its ordering key moves on without it (CONC-15's case again).
"""

from collections.abc import Iterator
from typing import Any

import pytest

from domainkit.clock import freeze_time
from domainkit.contrib.messaging import MessagingSettings, Worker, admin, enqueue, task
from domainkit.contrib.messaging import registry as tasks
from domainkit.contrib.messaging.registry import chain
from domainkit.contrib.messaging.tables import DEAD, DONE, PENDING
from domainkit_tests.test_messaging import (  # noqa: F401
    FAST,
    messaging,
    outbox,
    tables,
)

pytestmark = pytest.mark.anyio

FIRST = "tests.sec16c_2.first_step"
SECOND = "tests.sec16c_2.second_step"
HOUR = 3600.0
ran: list[str] = []


async def first_step(label: str) -> None:
    """The chain's first step, added by the deploy."""
    ran.append(f"first:{label}")


async def second_step(label: str) -> None:
    """The chain's second step, added by the same deploy."""
    ran.append(f"second:{label}")


@pytest.fixture(autouse=True)
def forget() -> Iterator[None]:
    """Start with nothing run; forget the tasks after."""
    ran.clear()
    yield
    tasks._tasks.pop(FIRST, None)
    tasks._tasks.pop(SECOND, None)


def config() -> MessagingSettings:
    """``FAST``."""
    return MessagingSettings(**FAST)


async def row_of(target: str) -> dict[str, Any]:
    """The outbox row of ``target``."""
    (row,) = [row for row in await outbox() if row["target"] == target]
    return row


@pytest.mark.usefixtures("tables")
async def test_the_next_step_of_a_chain_gets_its_own_grace() -> None:
    """A step first found unknown waits, wherever its chain began."""
    with freeze_time("2026-11-01T09:00:00Z") as clock:
        task(name=FIRST)(first_step)
        task(name=SECOND)(second_step)
        await enqueue(chain(first_step, second_step), label="x")  # New web pods.
        tasks._tasks.pop(FIRST)
        tasks._tasks.pop(SECOND)

        await Worker(config()).run_once()  # An old worker: the first step waits.
        assert (await row_of(FIRST))["status"] == PENDING

        clock.tick(HOUR - 60)
        task(name=FIRST)(first_step)  # A new worker runs it, near the grace's end.
        await Worker(config()).run_once()
        tasks._tasks.pop(FIRST)
        assert (await row_of(FIRST))["status"] == DONE

        clock.tick(120)  # An old worker, still running, claims the second step,
        await Worker(config()).run_once()  # which no worker found unknown before.

    second = await row_of(SECOND)
    assert (second["status"], second["last_error"]) == (
        PENDING,
        f"UnknownTarget: No task {SECOND!r}.",
    ), "the chain's next step was dead-lettered without any grace"


@pytest.mark.usefixtures("tables")
async def test_a_retried_dead_letter_gets_a_grace_again() -> None:
    """``messages retry`` puts it back with a grace of its own."""
    with freeze_time("2026-11-01T09:00:00Z") as clock:
        task(name=FIRST)(first_step)
        await enqueue(first_step, label="y")
        tasks._tasks.pop(FIRST)
        await Worker(config()).run_once()
        clock.tick(HOUR)
        await Worker(config()).run_once()
        assert (await row_of(FIRST))["status"] == DEAD

        clock.tick(HOUR)  # The operator puts it back as the next deploy rolls out.
        assert await admin.retry_dead(config(), None) == 1
        await Worker(config()).run_once()  # An old worker claims it first.

    assert (await row_of(FIRST))["status"] == PENDING, (
        "the retried message was dead-lettered again at once"
    )
