"""Local time for display (Django's ``django.utils.timezone``).

Dates are stored, compared, and sent in UTC (``UTCDateTime`` columns,
aware datetimes everywhere). ``TIME_ZONE`` is only for showing them, and
for "today" in the business's own zone::

    from domainkit.i18n import timezone

    timezone.localtime(invoice.issued_at)  # an aware datetime in TIME_ZONE
    timezone.localdate()                   # today, where the business is

    with timezone.override("Africa/Cairo"):
        ...                                # this caller's zone, for a block

Zones come from the system's time zone database; where there is none
(some slim containers), install the ``tzdata`` package.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, tzinfo
from functools import cache
from zoneinfo import ZoneInfo

from domainkit import clock
from domainkit.conf import settings

__all__ = [
    "localdate",
    "localtime",
    "now",
    "override",
]

_active: ContextVar[tzinfo | None] = ContextVar("timezone", default=None)


@cache
def zone(name: str) -> ZoneInfo:
    """The zone called ``name`` (``"Africa/Cairo"``), loaded once.

    Raises:
        zoneinfo.ZoneInfoNotFoundError: No zone has that name.
        ValueError: ``name`` is not a zone name at all.
    """
    return ZoneInfo(name)


def get_default_timezone() -> ZoneInfo:
    """The zone of ``TIME_ZONE``."""
    return zone(settings.TIME_ZONE)


def get_current_timezone() -> tzinfo:
    """The zone of the block being served (``override``), or ``TIME_ZONE``."""
    return _active.get() or get_default_timezone()


@contextmanager
def override(timezone: str | tzinfo) -> Iterator[None]:
    """Show times in ``timezone`` (a name or a ``tzinfo``) inside the block."""
    token = _active.set(zone(timezone) if isinstance(timezone, str) else timezone)
    try:
        yield
    finally:
        _active.reset(token)


def now() -> datetime:
    """The current time, aware, in UTC: what to store and compare.

    It is ``domainkit.clock.now()``, so a frozen clock stops it too.
    """
    return clock.now()


def localtime(
    value: datetime | None = None, timezone: tzinfo | None = None
) -> datetime:
    """``value`` (default: now) in ``timezone`` (default: the current zone).

    Raises:
        ValueError: ``value`` is naive: its zone is unknown.
    """
    if value is None:
        value = now()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(
            "localtime() needs an aware datetime; a naive one's zone is unknown."
        )
    return value.astimezone(timezone or get_current_timezone())


def localdate(value: datetime | None = None, timezone: tzinfo | None = None) -> date:
    """The date ``value`` (default: now) falls on in ``timezone``."""
    return localtime(value, timezone).date()
