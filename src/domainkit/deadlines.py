"""Deadlines: how long the work of a request, or of a block, may take.

Every HTTP request gets a deadline when it arrives (``REQUEST_TIMEOUT``; see
``domainkit.http.deadlines``). It lives in a context variable, as the request
context does, so whatever the request waits on can respect it::

    from domainkit import deadlines

    deadlines.remaining()  # Seconds left; None without a deadline.
    deadlines.remaining(5.0)  # The smaller of 5 and the seconds left.
    await deadlines.within(fetch_rates(), 5.0)  # Bound one await.
    async with deadlines.timeout(5.0):  # Bound a block, as asyncio.timeout.
        ...

``timeout`` and ``within`` bound their work by the smaller of their own
seconds and the time remaining, and make that the deadline inside, so what
the block calls sees the tighter one. When their own seconds run out first,
they raise ``TimeoutError``. When the deadline does, the block that set it
cancels the work and raises: the request's answers 503, and a ``timeout``
block raises ``DeadlineExceeded`` (a ``TimeoutError`` too). A block entered
with no time left raises ``DeadlineExceeded`` at once, without running.
A deadline governs the work inside its block: tasks that outlive the
block are not bound by it.

Outside a request there is no deadline until a block sets one with
``timeout``: a worker bounds a task the same way. Transactions on
PostgreSQL lower ``statement_timeout`` to the time remaining, and
``domainkit.http.client`` bounds every call by it.

The time remaining is read with ``domainkit.clock.monotonic``, so a frozen
clock stops it; the timers that cancel the work run on the event loop's
clock.
"""

import asyncio
import math
from collections.abc import Awaitable, Coroutine
from contextvars import ContextVar, Token
from types import TracebackType
from typing import Any, Self, overload

from domainkit import clock


class DeadlineExceeded(TimeoutError):
    """The deadline ran out: the request's, or that of a ``timeout`` block."""


class Deadline:
    """When the work of a request, or of a ``timeout`` block, must be done.

    ``at`` is a reading of ``clock.monotonic()``; None means there is no
    deadline (any more). A deadline can be moved (``reset``) or cleared
    while its work runs, and the timer that enforces it follows.
    """

    def __init__(self, seconds: float | None) -> None:
        """End ``seconds`` from now; None for no deadline.

        Raises:
            ValueError: ``seconds`` is not a finite number.
        """
        _check(seconds)
        self.started = clock.monotonic()
        """When the work started, the reading ``reset`` counts from."""
        self.seconds = seconds
        """The seconds the work was given, from when it started; kept when
        cleared, so logs can say what the work had."""
        self.at = None if seconds is None else self.started + seconds
        self._timer: asyncio.Timeout | None = None
        self._task: asyncio.Task[Any] | None = None
        self._outer: Deadline | None = None

    def remaining(self) -> float | None:
        """Seconds left, never below 0; None without a deadline."""
        if self.at is None:
            return None
        return max(self.at - clock.monotonic(), 0.0)

    def expired(self) -> bool:
        """Whether the deadline has passed."""
        return self.remaining() == 0.0

    def reset(self, seconds: float | None) -> None:
        """End ``seconds`` after the work started instead; None clears it.

        Raises:
            ValueError: ``seconds`` is not a finite number.
        """
        _check(seconds)
        self.seconds = seconds
        self.at = None if seconds is None else self.started + seconds
        self._reschedule()

    def clear(self) -> None:
        """Remove the deadline: nothing is cut off, and ``remaining()`` is None."""
        self.at = None
        self._reschedule()

    def _enforced(self) -> bool:
        """Whether a timer cancels the running task when the deadline passes.

        Its own, or that of the outer deadline it shares: a task started in
        the block shares the deadline, but not the timer, which cancels
        only the task that entered the block.
        """
        if self._timer is not None:
            return self._task is asyncio.current_task()
        return self._outer is not None and self._outer._enforced()

    def _enforce(
        self, timer: asyncio.Timeout | None, outer: "Deadline | None" = None
    ) -> None:
        """Enforce with ``timer``, or with ``outer``'s; neither stops it."""
        self._timer = timer
        self._task = None if timer is None else asyncio.current_task()
        self._outer = outer

    def _reschedule(self) -> None:
        """Move the enforcing timer to the deadline, unless it already fired."""
        timer = self._timer
        if timer is None or timer.expired():
            return
        left = self.remaining()
        when = None if left is None else asyncio.get_running_loop().time() + left
        timer.reschedule(when)

    def __repr__(self) -> str:
        """Show the seconds left."""
        left = self.remaining()
        return "<Deadline none>" if left is None else f"<Deadline {left:.3f}s left>"


_current: ContextVar[Deadline | None] = ContextVar("domainkit_deadline", default=None)


def current_deadline() -> Deadline | None:
    """Return the deadline of the work being done, or None outside one."""
    return _current.get()


@overload
def remaining(at_most: None = None) -> float | None: ...


@overload
def remaining(at_most: float) -> float: ...


def remaining(at_most: float | None = None) -> float | None:
    """Return the seconds left before the deadline; None without one.

    With ``at_most``, return the smaller of it and the seconds left: what to
    pass to a library as its own timeout, so it never waits past the
    deadline. It is 0 once the deadline has passed.
    """
    deadline = _current.get()
    left = None if deadline is None else deadline.remaining()
    if left is None:
        return at_most
    if at_most is None:
        return left
    return min(at_most, left)


class Timeout:
    """A block bounded by its seconds and the deadline (see ``timeout``)."""

    def __init__(self, seconds: float | None) -> None:
        """Bound the block by ``seconds`` too, unless None.

        Raises:
            ValueError: ``seconds`` is not a finite number.
        """
        _check(seconds)
        self.seconds = seconds
        self.deadline: Deadline | None = None
        """The block's deadline, once entered."""
        self._by_deadline = False
        self._timer: asyncio.Timeout | None = None
        self._token: Token[Deadline | None] | None = None

    def expired(self) -> bool:
        """Whether this block's own timer ran out (and cancelled the block)."""
        return self._timer is not None and self._timer.expired()

    async def __aenter__(self) -> Self:
        """Start the timer; raise at once if no time is left.

        A block bounded by a deadline that an outer block enforces starts no
        timer of its own: the outer one cancels the work, and its block
        raises, so it is always clear which deadline ran out.

        Raises:
            DeadlineExceeded: The deadline has already passed.
            TimeoutError: ``seconds`` is not above 0.
        """
        outer = _current.get()
        left = None if outer is None else outer.remaining()
        limit, self._by_deadline = _limit(self.seconds, left)
        if limit is not None and limit <= 0:
            raise _expired(self._by_deadline)
        self.deadline = Deadline(limit)
        if self._by_deadline and outer is not None and outer._enforced():
            self.deadline._enforce(None, outer)
        elif limit is not None:
            self._timer = asyncio.timeout(limit)
            await self._timer.__aenter__()
            self.deadline._enforce(self._timer)
        self._token = _current.set(self.deadline)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Stop the timer; turn its cancellation into a timeout error.

        Raises:
            DeadlineExceeded: The deadline ran out before the block ended.
            TimeoutError: The block's own seconds ran out first.
        """
        if self.deadline is None or self._token is None:
            return  # Never entered.
        _current.reset(self._token)
        self.deadline._enforce(None)
        self.deadline.clear()  # Tasks that outlive the block are not bound.
        if self._timer is None:
            return
        try:
            await self._timer.__aexit__(exc_type, exc, traceback)
        except TimeoutError as error:
            if self._by_deadline:
                raise DeadlineExceeded("The deadline passed.") from error
            raise


def timeout(seconds: float | None = None) -> Timeout:
    """Bound an ``async with`` block by ``seconds`` and by the deadline.

    The block gets the smaller of the two, as its deadline too: the code
    it runs sees it in ``remaining()``. Like ``asyncio.timeout``, the block
    is cancelled when its time runs out. Its own ``seconds`` running out
    raises ``TimeoutError`` here; the deadline running out cancels the work
    up to the block that set the deadline, which raises (a ``timeout``
    block: ``DeadlineExceeded``). Without ``seconds`` and outside any
    deadline, nothing is bounded. If no time is left on entry,
    ``DeadlineExceeded`` (or, for ``seconds`` of 0 or less, ``TimeoutError``)
    is raised there, and the block does not run. ``seconds`` that are not a
    finite number (NaN, infinity) are refused with ``ValueError``: None is
    how a block says it has no bound of its own.
    """
    return Timeout(seconds)


async def within[T](awaitable: Awaitable[T], seconds: float | None = None) -> T:
    """Await ``awaitable``, bounded by ``seconds`` and by the deadline.

    As ``timeout`` for one await (``asyncio.wait_for``'s replacement). If no
    time is left, a coroutine is closed and a future cancelled, unawaited.

    Raises:
        DeadlineExceeded: No time was left, or a deadline that no outer
            block enforces ran out.
        TimeoutError: ``seconds`` ran out first.
        ValueError: ``seconds`` is not a finite number.
    """
    try:
        bound = Timeout(seconds)
    except ValueError:
        _discard(awaitable)
        raise
    try:
        async with bound:
            return await awaitable
    finally:
        if bound.deadline is None:  # Refused on entry: never awaited.
            _discard(awaitable)


def _check(seconds: float | None) -> None:
    """Refuse ``seconds`` that are not a finite number: NaN or infinity.

    NaN compares false with everything, so it would bound nothing, and an
    infinite deadline overflows what is derived from it (PostgreSQL's
    ``statement_timeout``); None is how to ask for no bound.
    """
    if seconds is not None and not math.isfinite(seconds):
        raise ValueError(
            f"A deadline is a finite number of seconds, or None; not {seconds!r}."
        )


def _limit(seconds: float | None, left: float | None) -> tuple[float | None, bool]:
    """The bound of a block, and whether the deadline (``left``) sets it."""
    if left is None:
        return seconds, False
    if seconds is None or left <= seconds:
        return left, True
    return seconds, False


def _expired(by_deadline: bool) -> TimeoutError:
    """The error for a block that has no time left."""
    if by_deadline:
        return DeadlineExceeded("The deadline has passed.")
    return TimeoutError("No time was given.")


def _discard(awaitable: Awaitable[Any]) -> None:
    """Close a coroutine, or cancel a future, that will never be awaited."""
    if isinstance(awaitable, Coroutine):
        awaitable.close()
    elif isinstance(awaitable, asyncio.Future):
        awaitable.cancel()
