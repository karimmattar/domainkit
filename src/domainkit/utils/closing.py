"""Close every item of a collection, even after one fails or hangs."""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Sequence
from typing import Any, Final

logger = logging.getLogger(__name__)

RESERVED_SHARE: Final = 0.25
"""The part of ``close_in_turn``'s time kept back for the items still to close."""


async def close_each[T](
    items: Iterable[T], close: Callable[[T], Awaitable[object]]
) -> None:
    """Close every item at once; when all are done, raise the first failure.

    Items close concurrently, so one that hangs holds up none of the others.
    The first failure in ``items`` order is raised and later ones are
    logged, since only one can be raised. If the wait is cancelled (a
    deadline passed), the items still closing are named in the log and
    cancelled, those that failed are logged, and the cancellation goes on.
    """
    items = list(items)

    async def run(item: T) -> None:
        await close(item)

    closing = [asyncio.ensure_future(run(item)) for item in items]
    if not closing:
        return
    try:
        await asyncio.wait(closing)
    except asyncio.CancelledError:
        for item, task in zip(items, closing, strict=True):
            if not task.done():
                task.cancel()
                task.add_done_callback(forget_outcome)
                logger.error("Gave up closing %r", item)
            elif not task.cancelled() and (exc := task.exception()) is not None:
                logger.error("Could not close %r", item, exc_info=exc)
        raise
    _raise_first(
        items,
        [
            asyncio.CancelledError() if task.cancelled() else task.exception()
            for task in closing
        ],
    )


async def close_in_turn[T](
    items: Sequence[T],
    close: Callable[[T], Awaitable[object]],
    *,
    within: float | None = None,
) -> None:
    """Close the items one after another, in order; then raise the first failure.

    With ``within``, the items share that many seconds. Each may take what
    is left, less a reserve kept for every item after it (a quarter of
    ``within``, split among the items), so one slow close may use most of
    the time while one that hangs is cut off (``TimeoutError``) before the
    others' reserves. Each close runs as a task: one cut off is cancelled
    and left behind, so a close that ignores cancellation holds up neither
    the items after it nor the caller. Every item is tried; later failures
    are logged. If the wait is cancelled, the close running is cancelled
    and left behind too, and the cancellation goes on.
    """
    loop = asyncio.get_running_loop()
    end = None if within is None else loop.time() + within
    reserve = 0.0 if within is None else within * RESERVED_SHARE / max(len(items), 1)
    failures: list[BaseException | None] = []

    async def run(item: T) -> None:
        await close(item)

    for done, item in enumerate(items):
        after = len(items) - done - 1
        share = None if end is None else end - loop.time() - reserve * after
        closing = asyncio.ensure_future(run(item))
        try:
            await asyncio.wait([closing], timeout=share)
        except asyncio.CancelledError:
            _leave_behind(closing)
            logger.error("Gave up closing %r", item)
            raise
        if not closing.done():
            _leave_behind(closing)
            failures.append(TimeoutError(f"{item!r} did not close within {share:.2f}s"))
        elif closing.cancelled():
            failures.append(asyncio.CancelledError())
        else:
            failures.append(closing.exception())
    _raise_first(items, failures)


def _leave_behind(task: "asyncio.Future[Any]") -> None:
    """Cancel ``task`` and stop waiting for it; how it ends is not logged."""
    task.cancel()
    task.add_done_callback(forget_outcome)


def forget_outcome(task: "asyncio.Future[Any]") -> None:
    """Retrieve how a task given up on ended, so asyncio does not log it."""
    if not task.cancelled():
        task.exception()


def _raise_first[T](
    items: Sequence[T], failures: Sequence[BaseException | None]
) -> None:
    """Log every failure but the first, then raise the first, if any."""
    first: BaseException | None = None
    for item, failure in zip(items, failures, strict=True):
        if failure is None:
            continue
        if first is None:
            first = failure
        else:
            logger.error("Could not close %r", item, exc_info=failure)
    if first is not None:
        raise first
