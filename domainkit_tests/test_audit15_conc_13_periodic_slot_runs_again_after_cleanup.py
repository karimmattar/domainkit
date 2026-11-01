"""CONC-13: a periodic slot longer than RETENTION_SECONDS runs again after cleanup.

``@periodic(every=...)`` promises one run per interval across all workers.
``schedule_periodic`` queues the *current* slot, due at its start, under
``dedupe_key = periodic:<task>:<slot>``, and relies on that key to stop
other workers (and restarted ones, whose in-memory ``_scheduled`` is empty)
from queuing it again. But the key lives only as long as the row: cleanup
deletes done messages ``RETENTION_SECONDS`` (7 days by default) after they
finish. With an interval longer than that (``every=timedelta(days=30)``, a
monthly statement or purge), any worker that starts after the cleanup,
during the same slot (every deploy, every crash restart, every autoscaled
replica), finds no key and queues the slot again, due at once: the
"monthly" task runs again, once per restart for the rest of the month.
"""

from datetime import UTC, datetime, timedelta

import pytest

from domainkit.clock import freeze_time
from domainkit.contrib.messaging import Worker, periodic
from domainkit.contrib.messaging import registry as tasks
from domainkit_tests.test_messaging import messaging, tables  # noqa: F401

pytestmark = pytest.mark.anyio

MONTH = timedelta(days=30)


@pytest.mark.usefixtures("tables")
async def test_a_monthly_task_runs_once_per_month_across_worker_restarts() -> None:
    """A worker started after cleanup must not queue the same slot again."""
    runs: list[datetime] = []

    @periodic(every=MONTH, name="tests.audit15.monthly")
    async def monthly() -> None:
        runs.append(datetime.now(UTC))

    seconds = MONTH.total_seconds()
    slot = int(datetime(2026, 11, 1, tzinfo=UTC).timestamp() / seconds)
    start = datetime.fromtimestamp(slot * seconds, UTC) + timedelta(hours=1)
    try:
        with freeze_time(start) as frozen:
            first = Worker()
            assert await first.run_once() == 1

            # Eight days on, still in the same 30-day slot: cleanup runs
            # (every worker does it hourly) and deletes the finished run.
            frozen.tick(timedelta(days=8))
            assert await first.cleanup() == 1

            # A deploy (or a crash restart): a new worker process.
            restarted = Worker()
            ran_again = await restarted.run_once()
    finally:
        tasks._tasks.pop("tests.audit15.monthly")

    # Safe behaviour: the slot already ran; on d126394 it runs a second time.
    assert ran_again == 0
    assert len(runs) == 1
