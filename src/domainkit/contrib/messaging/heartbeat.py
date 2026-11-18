"""The worker's health signals: a file it touches while it lives, one while it is ready.

``runworker --heartbeat-file /tmp/worker-alive`` rewrites the file every
``--heartbeat-interval`` seconds, from the worker's own event loop: a loop
that is stuck lets it age. A container's health check reads its age::

    HEALTHCHECK CMD test $(( $(date +%s) - $(stat -c %Y /tmp/worker-alive) )) -lt 60

It says that the worker lives, not that what it calls answers. While the
broker is away, the relaying workers run the outbox themselves (ADR 0142),
and while a database or a cache is away, the worker waits for it: a
restart would help neither (ADR 0187). So the file is touched on a
schedule of its own, which no check holds up, however slow.

Beside it, rounds of checks run, ``--heartbeat-interval`` apart: what
``/ready`` checks for the application (the domains, the databases, the
ledgers, the caches), and the broker the worker consumes from. A failing
check is logged, and so is a check that raises. ``--ready-file
/tmp/worker-ready`` is rewritten while every check holds, and removed
when one fails: a readiness probe, or an alert, reads it. A fresh
heartbeat beside a missing ready file is a degraded worker.

A server's ``--metrics-port`` could not say as much: it answers from a
thread of its own, however stuck the loop is. Both files are removed when
the worker stops.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from functools import partial
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
    find: it is the worker's liveness. A check that raises counts as
    failing, and is logged: the round never raises.
    """
    if path is not None:
        await _touch(path)
    held = await _held(broker)
    if ready is not None:
        try:
            if held:
                await _touch(ready)
            else:
                await asyncio.to_thread(ready.unlink, missing_ok=True)
        except OSError:
            logger.exception("Could not write or remove the ready file %s", ready)
    return held


async def _held(broker: str | None) -> bool:
    """Whether every check holds; a failing one, or one that raises, is logged."""
    try:
        failing = await failures(broker)
    except Exception:
        logger.exception("The worker's checks raised; it is not ready, and carries on")
        return False
    if failing:
        logger.warning(
            "The worker is not ready, and carries on: failing %s", ", ".join(failing)
        )
        return False
    return True


async def heartbeat(
    path: Path | None,
    interval: float,
    stop: asyncio.Event,
    *,
    broker: str | None,
    ready: Path | None = None,
) -> None:
    """Touch ``path`` and check every ``interval`` seconds until ``stop``.

    The touches keep a schedule of their own: no round of checks holds
    them up, and a round under way at ``stop`` is cut short. Both files
    are removed at the end.
    """
    try:
        async with asyncio.TaskGroup() as group:
            if path is not None:
                group.create_task(_every(interval, stop, partial(_live, path)))
            checking = group.create_task(
                _every(interval, stop, partial(beat, None, broker, ready))
            )
            await stop.wait()
            checking.cancel()
    finally:
        for each in (path, ready):
            if each is not None:
                await asyncio.to_thread(each.unlink, missing_ok=True)


async def _every(
    interval: float, stop: asyncio.Event, step: Callable[[], Awaitable[object]]
) -> None:
    """Await ``step()``, then wait ``interval`` seconds, until ``stop``."""
    while not stop.is_set():
        await step()
        with suppress(TimeoutError):
            async with asyncio.timeout(interval):
                await stop.wait()


async def _live(path: Path) -> None:
    """Touch ``path``; a failed write is logged, and the file ages."""
    try:
        await _touch(path)
    except OSError:
        logger.exception("Could not touch the heartbeat file %s", path)


async def _touch(path: Path) -> None:
    """Write the time to ``path``."""
    await asyncio.to_thread(path.write_text, f"{clock.now().isoformat()}\n")
