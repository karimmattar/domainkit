"""How long a test waits for something to happen, and how.

Long enough never to fail on a slow runner, short enough that a regression
fails instead of hanging: ``WAIT`` seconds, for every test that waits.
``until`` asks a condition again until it holds; ``within`` awaits what it
is given (A28-TEST-06).
"""

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, Final

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


async def within(awaitable: Awaitable[Any]) -> Any:  # noqa: ANN401 - any awaitable.
    """Await ``awaitable``, failing after ``WAIT`` seconds.

    Its answer is untyped, as the tests' copies had it: typeshed types some
    awaitables otherwise than they answer (``asyncio.gather`` a tuple).
    """
    async with asyncio.timeout(WAIT):
        return await awaitable
