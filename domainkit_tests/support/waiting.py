"""How long a test waits for something to happen, and how.

Long enough never to fail on a slow runner, short enough that a regression
fails instead of hanging: ``WAIT`` seconds, for every test that waits.
``until`` asks a condition again until it holds; ``within`` awaits what it
is given (TEST-06).
"""

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Final

WAIT: Final = 30.0
"""The longest any test waits for something; a regression fails, never hangs."""
STEP: Final = 0.01
"""How long ``until`` sleeps between two looks at its condition."""


async def until(
    condition: Callable[[], bool | Awaitable[bool]],
    message: str = "Timed out waiting",
) -> None:
    """Wait for ``condition()`` to hold, at most ``WAIT`` seconds.

    Raises:
        AssertionError: It did not hold in time; ``message`` says what.
    """
    loop = asyncio.get_running_loop()
    end = loop.time() + WAIT
    while True:
        held = condition()
        if inspect.isawaitable(held):
            held = await held
        if held:
            return
        if loop.time() > end:
            raise AssertionError(message)
        await asyncio.sleep(STEP)


async def within[T](awaitable: Awaitable[T]) -> T:
    """Await ``awaitable``, failing after ``WAIT`` seconds."""
    async with asyncio.timeout(WAIT):
        return await awaitable
