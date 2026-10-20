"""Closing what a process opened: tasks, caches, databases, ledgers, and brokers.

The application lifespan and management commands end with ``close_resources``.
The loop's background tasks (``domainkit.tasks``) are cancelled first, since
they use the rest, then caches close, since their computations use the
others. Each step gets ``CLOSE_TIMEOUT`` seconds; one that fails or hangs is
logged, and the rest still close, so a broken backend never keeps the
others open.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Final

from domainkit import tasks
from domainkit.broker import brokers
from domainkit.cache import caches
from domainkit.db.connections import connections
from domainkit.ledger import ledgers

logger = logging.getLogger(__name__)

CLOSE_TIMEOUT: Final = 5.0
"""Seconds each kind of resource gets to close at shutdown."""


async def close_resources() -> None:
    """Stop the loop's tasks, then close caches, databases, ledgers, and brokers."""
    loop = asyncio.get_running_loop()
    if stuck := await tasks.shutdown(loop.time() + CLOSE_TIMEOUT):
        logger.error(
            "Background tasks still running after %.1fs: %s",
            CLOSE_TIMEOUT,
            ", ".join(stuck),
        )
    closers: dict[str, Callable[[], Awaitable[None]]] = {
        "caches": caches.close_all,
        "databases": connections.close_all,
        "ledgers": ledgers.close_all,
        "brokers": brokers.close_all,
    }
    for name, close_all in closers.items():
        try:
            async with asyncio.timeout(CLOSE_TIMEOUT):
                await close_all()
        except Exception:
            logger.exception("Could not close the %s; closing the rest", name)
