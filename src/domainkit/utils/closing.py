"""Close every item of a collection, even after one fails."""

import logging
from collections.abc import Awaitable, Callable, Iterable

logger = logging.getLogger(__name__)


async def close_each[T](
    items: Iterable[T], close: Callable[[T], Awaitable[object]]
) -> None:
    """Close every item in order, then raise the first failure.

    Later failures are logged, since only one can be raised.
    """
    first: Exception | None = None
    for item in items:
        try:
            await close(item)
        except Exception as exc:
            if first is None:
                first = exc
            else:
                logger.error("Could not close %r", item, exc_info=exc)
    if first is not None:
        raise first
