"""Five-field cron schedules in a time zone, for ``@periodic(cron=...)``.

An expression has five fields, separated by spaces::

    minute  hour  day-of-month  month  day-of-week
    0-59    0-23  1-31          1-12   0-7 (0 and 7 are Sunday)

Each field is ``*``, a value, a range ``a-b``, a step (``*/n``, ``a-b/n``,
or ``a/n``, which runs from ``a`` to the field's end), or a comma-separated
list of these. Months and days of the week also take English names
(``JAN``-``DEC``, ``SUN``-``SAT``, in any case). ``@yearly``
(``@annually``), ``@monthly``, ``@weekly``, ``@daily`` (``@midnight``),
and ``@hourly`` stand for the usual expressions.

As in cron, when both the day of the month and the day of the week are
restricted (neither field starts with ``*``), a day matches when either
does: ``0 0 1 * MON`` runs on the first of the month and on Mondays.

Times are wall-clock times in the schedule's zone. Daylight saving time
never runs a time twice and never skips one:

- A time that a spring-forward gap skips (02:30 when clocks jump from
  02:00 to 03:00) runs once, at the end of the gap (03:00).
- A time that a fall-back repeats (01:30 when clocks go from 02:00 back to
  01:00) runs once, at its first occurrence. If the hour field starts with
  ``*`` (``*/15 * * * *``), times in the repeated hour run in both passes
  instead, so the schedule keeps its pace in elapsed time, as cron does.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MACROS: Final = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}
MAX_LENGTH: Final = 256
"""Longest expression accepted."""
MONTHS: Final = {
    name: number
    for number, name in enumerate(
        (
            *("JAN", "FEB", "MAR", "APR", "MAY", "JUN"),
            *("JUL", "AUG", "SEP", "OCT", "NOV", "DEC"),
        ),
        start=1,
    )
}
WEEKDAYS: Final = {
    name: number
    for number, name in enumerate(("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"))
}
SEARCH_DAYS: Final = 366 * 50
"""Days searched for the next run; an expression that never runs is refused."""
MAX_JUMP: Final = timedelta(hours=3)
"""The largest change of a zone's offset taken into account (DST is 1 hour)."""
_ITEM: Final = re.compile(
    r"(?P<start>\*|[0-9A-Z]{1,3})(?:-(?P<end>[0-9A-Z]{1,3}))?(?:/(?P<step>[0-9]{1,2}))?"
)


class CronError(ValueError):
    """An expression or time zone that cannot be used."""


@dataclass(frozen=True, slots=True)
class _Field:
    """One field's bounds and names."""

    name: str
    low: int
    high: int
    names: dict[str, int] = field(default_factory=dict)

    def parse(self, text: str) -> frozenset[int]:
        """Return the values ``text`` selects."""
        values: set[int] = set()
        for item in text.split(","):
            match = _ITEM.fullmatch(item.upper())
            if match is None:
                raise CronError(f"{self.name}: {item!r} is not a value, range, or step")
            values.update(self._values(match))
        return frozenset(values)

    def _values(self, match: re.Match[str]) -> range:
        """The values one item selects."""
        start, end, step = match["start"], match["end"], match["step"]
        if start == "*":
            if end is not None:
                raise CronError(f"{self.name}: '*' cannot start a range")
            low, high = self.low, self.high
        else:
            low = self._number(start)
            high = self._number(end) if end is not None else low
            if step is not None and end is None:
                high = self.high  # a/n runs to the end of the field.
        if low > high:
            raise CronError(f"{self.name}: the range {low}-{high} runs backwards")
        every = 1 if step is None else int(step)
        if not 1 <= every <= self.high - self.low + 1:
            raise CronError(f"{self.name}: the step {every} is out of range")
        return range(low, high + 1, every)

    def _number(self, text: str) -> int:
        """A value given as a number or a name, checked against the bounds."""
        if text in self.names:
            return self.names[text]
        if not text.isdigit():
            raise CronError(f"{self.name}: {text!r} is not a number or a name")
        number = int(text)
        if not self.low <= number <= self.high:
            raise CronError(f"{self.name}: {number} is outside {self.low}-{self.high}")
        return number


_MINUTE: Final = _Field("minute", 0, 59)
_HOUR: Final = _Field("hour", 0, 23)
_DAY: Final = _Field("day of month", 1, 31)
_MONTH: Final = _Field("month", 1, 12, MONTHS)
_WEEKDAY: Final = _Field("day of week", 0, 7, WEEKDAYS)


def zone(name: str) -> ZoneInfo:
    """Return the IANA time zone ``name`` (``Africa/Cairo``).

    Raises:
        CronError: the zone is unknown. On Windows, which has no zone
            database of its own, install ``domainkit[tzdata]``.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise CronError(
            f"Unknown time zone {name!r}; use an IANA name such as 'Africa/Cairo' "
            "(on Windows, install domainkit[tzdata])."
        ) from None


@dataclass(frozen=True, slots=True)
class Cron:
    """A parsed cron expression and the zone its times are in."""

    expression: str
    """The expression as given (macros included)."""
    tz: ZoneInfo
    minutes: tuple[int, ...]
    hours: tuple[int, ...]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    """Days of the week, 0 (Sunday) to 6."""
    any_day: bool
    """The day of the month starts with ``*``."""
    any_weekday: bool
    """The day of the week starts with ``*``."""
    any_hour: bool
    """The hour starts with ``*``: repeated hours run in both passes."""

    @classmethod
    def parse(cls, expression: str, tz: str = "UTC") -> "Cron":
        """Parse ``expression`` for zone ``tz``.

        Raises:
            CronError: the expression is malformed, never runs, or the zone
                is unknown.
        """
        if len(expression) > MAX_LENGTH:
            raise CronError(f"Cron expressions have at most {MAX_LENGTH} characters.")
        text = MACROS.get(expression.strip().lower(), expression)
        fields = text.split()
        if len(fields) != 5:
            raise CronError(
                f"{expression!r} needs five fields: minute, hour, day of month, "
                "month, and day of week"
            )
        minute, hour, day, month, weekday = fields
        cron = cls(
            expression=expression,
            tz=zone(tz),
            minutes=tuple(sorted(_MINUTE.parse(minute))),
            hours=tuple(sorted(_HOUR.parse(hour))),
            days=_DAY.parse(day),
            months=_MONTH.parse(month),
            weekdays=frozenset(value % 7 for value in _WEEKDAY.parse(weekday)),
            any_day=day.startswith("*"),
            any_weekday=weekday.startswith("*"),
            any_hour=hour.startswith("*"),
        )
        if cron.next_after(datetime(2000, 1, 1, tzinfo=UTC)) is None:
            raise CronError(f"{expression!r} never runs")
        return cron

    def __str__(self) -> str:
        """The expression and its zone."""
        return f"{self.expression} ({self.tz.key})"

    def matches_day(self, day: date) -> bool:
        """Whether runs are due on ``day`` (by month, day, and weekday)."""
        if day.month not in self.months:
            return False
        by_day = day.day in self.days
        by_weekday = (day.weekday() + 1) % 7 in self.weekdays
        if self.any_day or self.any_weekday:
            return by_day and by_weekday
        return by_day or by_weekday

    def next_after(self, instant: datetime) -> datetime | None:
        """The first run strictly after ``instant``, in UTC.

        Returns None if there is none in the next fifty years.
        """
        local = instant.astimezone(self.tz).replace(tzinfo=None)
        best: datetime | None = None
        best_wall = local
        for wall in self._walls(local - MAX_JUMP):
            if best is not None and wall > best_wall + 2 * MAX_JUMP:
                break  # No later wall time can run sooner.
            for run in self.instants(wall):
                if run > instant and (best is None or run < best):
                    best, best_wall = run, wall
        return best

    def _walls(self, start: datetime) -> Iterator[datetime]:
        """Every matching wall-clock time from ``start`` on, in order."""
        start = start.replace(second=0, microsecond=0)
        day = start.date()
        for _ in range(SEARCH_DAYS):
            if self.matches_day(day):
                for hour in self.hours:
                    for minute in self.minutes:
                        wall = datetime(day.year, day.month, day.day, hour, minute)  # noqa: DTZ001 - a wall-clock time.
                        if wall >= start:
                            yield wall
            day += timedelta(days=1)

    def instants(self, wall: datetime) -> list[datetime]:
        """The moments, in UTC, at which the wall-clock time ``wall`` runs."""
        first = wall.replace(tzinfo=self.tz, fold=0).astimezone(UTC)
        second = wall.replace(tzinfo=self.tz, fold=1).astimezone(UTC)
        if first == second:
            return [first]
        if self._shows(first, wall) and self._shows(second, wall):
            # Repeated by a fall-back: once, or in both passes for '*' hours.
            earlier, later = sorted((first, second))
            return [earlier, later] if self.any_hour else [earlier]
        return [self._gap_end(*sorted((first, second)))]

    def _shows(self, moment: datetime, wall: datetime) -> bool:
        """Whether the zone's clocks read ``wall`` at ``moment``."""
        return moment.astimezone(self.tz).replace(tzinfo=None) == wall

    def _gap_end(self, before: datetime, after: datetime) -> datetime:
        """The moment clocks jump forward, between ``before`` and ``after``."""
        offset = after.astimezone(self.tz).utcoffset()
        low, high = 0, int((after - before).total_seconds())
        while low < high:  # The first second on the new offset.
            middle = (low + high) // 2
            moment = before + timedelta(seconds=middle)
            if moment.astimezone(self.tz).utcoffset() == offset:
                high = middle
            else:
                low = middle + 1
        return before + timedelta(seconds=low)
