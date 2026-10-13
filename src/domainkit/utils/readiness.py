"""Run readiness checks concurrently, each with its own time limit."""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping

logger = logging.getLogger(__name__)


async def failing_checks(
    checks: Mapping[str, Callable[[], Awaitable[None]]], seconds: float
) -> list[str]:
    """Return the names of checks that raised or took over ``seconds``, in order.

    A separate limit per check keeps one slow dependency from hiding the
    state of the others. Failures are logged with their traceback.
    """

    async def passes(name: str, check: Callable[[], Awaitable[None]]) -> bool:
        try:
            async with asyncio.timeout(seconds):
                await check()
        except Exception:
            logger.exception("Readiness check %r failed", name)
            return False
        return True

    results = await asyncio.gather(
        *(passes(name, check) for name, check in checks.items())
    )
    return [name for name, ok in zip(checks, results, strict=True) if not ok]
