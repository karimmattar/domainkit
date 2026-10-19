"""The framework's clock: the one place domainkit reads the time.

``now()`` is the current time, aware and in UTC, and ``today()`` its date;
``monotonic()`` counts seconds for measuring intervals. domainkit reads the
time only through these, so freezing the clock moves everything it times
together: token expiry, idempotency leases, rate windows counted in this
process, cache lifetimes, and the timestamps of audit entries, events, and
messages. Domain code may read them too (the layer rules allow
``domainkit.clock``), and then follows the same frozen clock.

``freeze_time`` stops the clock, or sets it ticking from a chosen moment,
for the code its block runs::

    with freeze_time("2026-01-01T09:00:00Z") as clock:
        token = await issue_token()
        clock.tick(timedelta(hours=2))
        assert token.is_expired()

The frozen clock lives in a ``ContextVar``, like the request context: tasks
started inside the block see it, and concurrent requests never see one
another's. It never reaches the database or Redis, which keep their own
clocks: ``now()`` in SQL, a column's ``server_default``, and shared rate
limits counted on the Redis server.
"""

import time
from contextlib import suppress
from contextvars import ContextVar, Token
from datetime import UTC, date, datetime, timedelta
from types import TracebackType
from typing import Self

type Moment = datetime | date | str
"""A time to freeze at: an ISO 8601 string, a date (its midnight), or a datetime."""


def now() -> datetime:
    """Return the current time, aware and in UTC (the frozen clock's, if any)."""
    clock = frozen()
    return datetime.now(UTC) if clock is None else clock.now()


def today() -> date:
    """Return today's date in UTC (the frozen clock's, if any)."""
    return now().date()


def monotonic() -> float:
    """Return seconds from an arbitrary start, for intervals (``time.monotonic``).

    A frozen clock's count stands still with it and moves forward when it
    does, never back. Intervals measured across the end of a freeze are
    meaningless: the real count takes over again.
    """
    clock = frozen()
    return time.monotonic() if clock is None else clock.monotonic()


def moment(when: Moment | None = None) -> datetime:
    """Return ``when`` as an aware datetime in UTC; None means ``now()``.

    A string is read as ISO 8601. A date means its midnight. Values without
    a time zone are taken as UTC.
    """
    if when is None:
        return now()
    if isinstance(when, str):
        when = datetime.fromisoformat(when)
    if not isinstance(when, datetime):
        return datetime(when.year, when.month, when.day, tzinfo=UTC)
    if when.tzinfo is None:
        return when.replace(tzinfo=UTC)
    return when.astimezone(UTC)


def freeze_time(when: Moment | None = None, *, tick: bool = False) -> "FrozenClock":
    """Return a clock frozen at ``when`` (default: now), for a ``with`` block.

    With ``tick=True`` it starts at ``when`` and runs on in real time.
    Inside the block, ``now()``, ``today()``, and ``monotonic()`` read it.
    """
    return FrozenClock(moment(when), tick=tick)


class FrozenClock:
    """A clock that stands at a moment, or ticks on from it, until moved.

    ``freeze_time`` makes one; ``with`` (or ``start`` and ``stop``) puts it
    in use for the current context and the tasks started from it. ``tick``
    and ``move_to`` move it for everyone using it.
    """

    def __init__(self, when: datetime, *, tick: bool = False) -> None:
        """Stand at ``when`` (aware), or tick on from it with ``tick=True``."""
        self.ticking = tick
        self.active = False
        """Whether the clock is in use: between ``start`` and ``stop``."""
        self.outer: FrozenClock | None = None
        """The clock this one replaced, in use again once this one stops."""
        self._token: Token[FrozenClock | None] | None = None
        self._at = when
        self._since = time.monotonic()
        self._monotonic = self._since

    def now(self) -> datetime:
        """Return this clock's time."""
        return self._at + timedelta(seconds=self._passed())

    def monotonic(self) -> float:
        """Return this clock's interval count."""
        return self._monotonic + self._passed()

    def tick(self, delta: timedelta | float = 1.0) -> datetime:
        """Move forward by ``delta`` (a timedelta, or seconds); return the new time."""
        seconds = delta.total_seconds() if isinstance(delta, timedelta) else delta
        if seconds < 0:
            raise ValueError("tick() moves forward; use move_to() to go back.")
        self._settle()
        self._at += timedelta(seconds=seconds)
        self._monotonic += seconds
        return self._at

    def move_to(self, when: Moment) -> datetime:
        """Jump to ``when``, forward or back; return it as an aware UTC datetime.

        The interval count moves forward with the clock, but never back.
        """
        target = moment(when)
        self._settle()
        self._monotonic += max((target - self._at).total_seconds(), 0.0)
        self._at = target
        return target

    def start(self) -> Self:
        """Use this clock in the current context; starting twice changes nothing."""
        if not self.active:
            self.outer = frozen()
            self._token = _frozen.set(self)
            self.active = True
        return self

    def stop(self) -> None:
        """Stop using this clock: the one it replaced, if any, is read again.

        Contexts copied while it was in use (tasks it started) move on too.
        """
        self.active = False
        token, self._token = self._token, None
        if token is not None and _frozen.get() is self:
            with suppress(ValueError):  # Started in another context: it lapses.
                _frozen.reset(token)

    def __enter__(self) -> Self:
        """Start the clock for the block."""
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Stop the clock."""
        self.stop()

    def __repr__(self) -> str:
        """Show the clock's time and whether it ticks."""
        ticking = ", ticking" if self.ticking else ""
        return f"<FrozenClock {self.now().isoformat()}{ticking}>"

    def _passed(self) -> float:
        """Real seconds since the clock was last set, if it ticks."""
        return time.monotonic() - self._since if self.ticking else 0.0

    def _settle(self) -> None:
        """Fold the seconds ticked so far into the clock's readings."""
        real = time.monotonic()
        if self.ticking:
            passed = real - self._since
            self._at += timedelta(seconds=passed)
            self._monotonic += passed
        self._since = real


def frozen() -> FrozenClock | None:
    """Return the frozen clock in use in this context, or None for real time."""
    clock = _frozen.get()
    while clock is not None and not clock.active:
        clock = clock.outer
    return clock


_frozen: ContextVar[FrozenClock | None] = ContextVar("domainkit_clock", default=None)
