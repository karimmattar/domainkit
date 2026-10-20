"""Closing what a process opened: caches, databases, ledgers, and brokers.

The application lifespan and management commands end with ``close_resources``.
Caches close first, since their computations use the others. Each kind gets
``CLOSE_TIMEOUT`` seconds; one that fails or hangs is logged, and the rest
still close, so a broken backend never keeps the others open. Then error
reports and logs waiting for their threads are written, in the same time.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Final

from domainkit.broker import brokers
from domainkit.cache import caches
from domainkit.db.connections import connections
from domainkit.ledger import ledgers
from domainkit.log import aflush_logs
from domainkit.reporting import aflush_reports

logger = logging.getLogger(__name__)

CLOSE_TIMEOUT: Final = 5.0
"""Seconds each kind of resource gets to close at shutdown."""


async def close_resources() -> None:
    """Close caches, databases, ledgers, and brokers; then flush reports and logs."""
    closers: dict[str, Callable[[], Awaitable[None]]] = {
        "caches": caches.close_all,
        "databases": connections.close_all,
        "ledgers": ledgers.close_all,
        "brokers": brokers.close_all,
        # Last, so what closing the others reported and logged is sent too.
        "error reports": aflush_reports,
        "logs": aflush_logs,
    }
    for name, close_all in closers.items():
        try:
            async with asyncio.timeout(CLOSE_TIMEOUT):
                await close_all()
        except Exception:
            logger.exception("Could not close the %s; closing the rest", name)
