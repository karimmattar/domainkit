"""The worker's health signals: a file it touches while it lives, one while it is ready.

``runworker --heartbeat-file /tmp/worker-alive`` rewrites the file every
``--heartbeat-interval`` seconds, from the worker's own event loop: a loop
that is stuck lets it age. A container's health check reads its age::

    HEALTHCHECK CMD test $(( $(date +%s) - $(stat -c %Y /tmp/worker-alive) )) -lt 60

It says that the worker lives, not that what it calls answers. While the
broker is away, the relaying workers run the outbox themselves (ADR 0142),
and while a database or a cache is away, the worker waits for it: a
restart would help neither (ADR 0187).

Each round also checks what ``/ready`` checks for the application (the
domains, the databases, the ledgers, the caches), and the broker the
worker consumes from. A failing check is logged. ``--ready-file
/tmp/worker-ready`` is rewritten while every check holds, and removed
when one fails: a readiness probe, or an alert, reads it. A fresh
heartbeat beside a missing ready file is a degraded worker.

A server's ``--metrics-port`` could not say as much: it answers from a
thread of its own, however stuck the loop is. Both files are removed when
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


async def beat(
    path: Path | None, broker: str | None, ready: Path | None = None
) -> bool:
    """Touch ``path``, then run the checks: touch ``ready`` if all hold, else remove it.

    Return whether every check held. ``path`` is touched whatever they
    find: it is the worker's liveness.
    """
    if path is not None:
        await _touch(path)
    failing = await failures(broker)
    if failing:
        logger.warning(
            "The worker is not ready, and carries on: failing %s", ", ".join(failing)
        )
        if ready is not None:
            await asyncio.to_thread(ready.unlink, missing_ok=True)
        return False
    if ready is not None:
        await _touch(ready)
    return True


async def heartbeat(
    path: Path | None,
    interval: float,
    stop: asyncio.Event,
    *,
    broker: str | None,
    ready: Path | None = None,
) -> None:
    """Beat every ``interval`` seconds until ``stop``; then remove both files."""
    try:
        while not stop.is_set():
            await beat(path, broker, ready)
            with suppress(TimeoutError):
                async with asyncio.timeout(interval):
                    await stop.wait()
    finally:
        for each in (path, ready):
            if each is not None:
                await asyncio.to_thread(each.unlink, missing_ok=True)


async def _touch(path: Path) -> None:
    """Write the time to ``path``."""
    await asyncio.to_thread(path.write_text, f"{clock.now().isoformat()}\n")
