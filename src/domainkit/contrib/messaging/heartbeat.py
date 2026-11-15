"""The worker's health signal: a file it touches while every check holds.

``runworker --heartbeat-file /tmp/worker-alive`` checks, from the worker's
own event loop, what ``/ready`` checks for the application (the domains,
the databases, the ledgers, the caches), and the broker it consumes from.
While all hold, it rewrites the file every ``--heartbeat-interval``
seconds; a check that fails, or a loop that is stuck, lets it age. A
container's health check reads its age::

    HEALTHCHECK CMD test $(( $(date +%s) - $(stat -c %Y /tmp/worker-alive) )) -lt 60

A server's ``--metrics-port`` could not say as much: it answers from a
thread of its own, however stuck the loop is. The file is removed when
the worker stops.
"""

import asyncio
import logging
from contextlib import suppress
from pathlib import Path

from domainkit import clock
from domainkit.conf import settings

__all__ = ["beat", "failures", "heartbeat"]

logger = logging.getLogger(__name__)


async def failures(broker: str | None) -> list[str]:
    """What fails now: ``/ready``'s checks, and ``broker`` (``broker:<alias>``)."""
    from domainkit.http.health import broker_failures, readiness_failures

    seconds = settings.READINESS_TIMEOUT
    failing = await readiness_failures(seconds)
    if broker is not None and not settings.READINESS_BROKERS:  # Else checked.
        failing += await broker_failures([broker], seconds)
    return failing


async def beat(path: Path, broker: str | None) -> bool:
    """Run the checks once; touch ``path`` if all hold. Whether they did."""
    failing = await failures(broker)
    if failing:
        logger.warning(
            "Left the heartbeat file as it was: failing %s", ", ".join(failing)
        )
        return False
    await asyncio.to_thread(path.write_text, f"{clock.now().isoformat()}\n")
    return True


async def heartbeat(
    path: Path, interval: float, stop: asyncio.Event, *, broker: str | None
) -> None:
    """Beat every ``interval`` seconds until ``stop``; then remove ``path``."""
    try:
        while not stop.is_set():
            await beat(path, broker)
            with suppress(TimeoutError):
                async with asyncio.timeout(interval):
                    await stop.wait()
    finally:
        await asyncio.to_thread(path.unlink, missing_ok=True)
