"""Closing what a process opened: caches, HTTP clients, databases, ledgers, brokers.

The application lifespan and management commands end with ``close_resources``.
Caches close first, since their computations use the others. Each kind gets
``CLOSE_TIMEOUT`` seconds; one that fails or hangs is logged, and the rest
still close, so a broken backend never keeps the others open.
"""

import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable
from typing import Final

from domainkit.broker import brokers
from domainkit.cache import caches
from domainkit.db.connections import connections
from domainkit.ledger import ledgers

logger = logging.getLogger(__name__)

CLOSE_TIMEOUT: Final = 5.0
"""Seconds each kind of resource gets to close at shutdown."""


async def close_resources() -> None:
    """Close the caches, HTTP clients, databases, ledgers, and brokers, in order."""
    closers: dict[str, Callable[[], Awaitable[None]]] = {
        "caches": caches.close_all,
        "HTTP clients": close_http_clients,
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


async def close_http_clients() -> None:
    """Close ``domainkit.http.client``'s pools, if anything imported it.

    Looked up, not imported, so the lifespan needs no ``httpclient`` extra.
    """
    client = sys.modules.get("domainkit.http.client")
    if client is not None:
        await client.close_all()
