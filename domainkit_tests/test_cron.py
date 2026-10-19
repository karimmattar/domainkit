"""Cron schedules: the five-field syntax, time zones, and daylight saving time."""

from datetime import UTC, date, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from domainkit.contrib.messaging import cron as cron_module
from domainkit.contrib.messaging.cron import Cron, CronError, zone

NEW_YORK = "America/New_York"
CAIRO = "Africa/Cairo"


def runs(expression: str, tz: str, start: datetime, count: int) -> list[str]:
    """The next ``count`` runs after ``start``, as local times with offsets."""
    schedule = Cron.parse(expression, tz)
    found = []
    moment = start
    for _ in range(count):
        following = schedule.next_after(moment)
        assert following is not None
        assert following > moment
        assert following.tzinfo is UTC
        found.append(following.astimezone(schedule.tz).strftime("%m-%d %H:%M%z"))
        moment = following
    return found


def at(*fields: int) -> datetime:
    """A moment in UTC."""
    return datetime(*fields, tzinfo=UTC)  # type: ignore[arg-type,misc]


@pytest.mark.parametrize(
    ("expression", "minutes", "hours", "days", "months", "weekdays"),
    [
        ("* * * * *", range(60), range(24), range(1, 32), range(1, 13), range(7)),
        ("0 3 * * 1-5", [0], [3], range(1, 32), range(1, 13), range(1, 6)),
        (
            "*/15 9-17/4 1,15 JAN,jul *",
            [0, 15, 30, 45],
            [9, 13, 17],
            [1, 15],
            [1, 7],
            range(7),
        ),
        (
            "5/20 * 10-12 * *",
            [5, 25, 45],
            range(24),
            [10, 11, 12],
            range(1, 13),
            range(7),
        ),
        ("0 0 * * MON-FRI", [0], [0], range(1, 32), range(1, 13), range(1, 6)),
        ("0 0 * * 5-7", [0], [0], range(1, 32), range(1, 13), [0, 5, 6]),
        ("0 0 * * sun,7", [0], [0], range(1, 32), range(1, 13), [0]),
        ("0 0 * FEB-apr/2 *", [0], [0], range(1, 32), [2, 4], range(7)),
        ("59 23 31 12 6", [59], [23], [31], [12], [6]),
        ("00 05 * * *", [0], [5], range(1, 32), range(1, 13), range(7)),
        ("*/60 */24 */31 */12 */7", [0], [0], [1], [1], [0]),
    ],
)
def test_fields_select_their_values(
    expression: str,
    minutes: list[int],
    hours: list[int],
    days: list[int],
    months: list[int],
    weekdays: list[int],
) -> None:
    """Values, ranges, steps, lists, and names; 7 is Sunday, as 0 is."""
    schedule = Cron.parse(expression)

    assert schedule.minutes == tuple(minutes)
    assert schedule.hours == tuple(hours)
    assert schedule.days == frozenset(days)
    assert schedule.months == frozenset(months)
    assert schedule.weekdays == frozenset(weekdays)
    assert schedule.tz.key == "UTC"
    assert str(schedule) == f"{expression} (UTC)"


@pytest.mark.parametrize(
    ("macro", "expression"),
    [
        ("@yearly", "0 0 1 1 *"),
        ("@annually", "0 0 1 1 *"),
        ("@monthly", "0 0 1 * *"),
        ("@weekly", "0 0 * * 0"),
        ("@daily", "0 0 * * *"),
        ("@midnight", "0 0 * * *"),
        (" @HOURLY ", "0 * * * *"),
    ],
)
def test_macros_stand_for_expressions(macro: str, expression: str) -> None:
    """The usual macros, in any case; the expression keeps what was given."""
    schedule = Cron.parse(macro)
    plain = Cron.parse(expression)

    assert (schedule.minutes, schedule.hours, schedule.weekdays) == (
        plain.minutes,
        plain.hours,
        plain.weekdays,
    )
    assert (schedule.days, schedule.months) == (plain.days, plain.months)
    assert schedule.expression == macro


@pytest.mark.parametrize(
    ("expression", "error"),
    [
        (
            "* * * *",
            (
                "'* * * *' needs five fields: minute, hour, day of month, month, "
                "and day of week"
            ),
        ),
        ("* * * * * *", "needs five fields"),
        ("@reboot", "needs five fields"),
        ("1,,2 * * * *", "minute: '' is not a value, range, or step"),
        ("1- * * * *", "minute: '1-' is not a value, range, or step"),
        ("1/ * * * *", "minute: '1/' is not a value, range, or step"),
        ("+1 * * * *", "minute: '+1' is not a value, range, or step"),
        ("1000 * * * *", "minute: '1000' is not a value, range, or step"),
        ("*-5 * * * *", "minute: '*' cannot start a range"),
        ("60 * * * *", "minute: 60 is outside 0-59"),
        ("* 24 * * *", "hour: 24 is outside 0-23"),
        ("* * 0 * *", "day of month: 0 is outside 1-31"),
        ("* * 32 * *", "day of month: 32 is outside 1-31"),
        ("* * * 0 *", "month: 0 is outside 1-12"),
        ("* * * 13 *", "month: 13 is outside 1-12"),
        ("* * * * 8", "day of week: 8 is outside 0-7"),
        ("MON * * * *", "minute: 'MON' is not a number or a name"),
        ("* * * JAN-MON *", "month: 'MON' is not a number or a name"),
        ("* * * * 1A", "day of week: '1A' is not a number or a name"),
        ("5-1 * * * *", "minute: the range 5-1 runs backwards"),
        ("* * * * SAT-SUN", "day of week: the range 6-0 runs backwards"),
        ("*/0 * * * *", "minute: the step 0 is out of range"),
        ("*/61 * * * *", "minute: the step 61 is out of range"),
        ("* */25 * * *", "hour: the step 25 is out of range"),
        ("* * */32 * *", "day of month: the step 32 is out of range"),
        ("* * * */13 *", "month: the step 13 is out of range"),
        ("0 0 30 2 *", "'0 0 30 2 *' never runs"),
        ("0 0 31 4,6,9,11 *", "never runs"),
        ("*" * 257, "at most 256 characters"),
    ],
)
def test_malformed_expressions_are_refused(expression: str, error: str) -> None:
    """Each mistake is named, with its field."""
    with pytest.raises(CronError) as refused:
        Cron.parse(expression)
    assert error in str(refused.value)
    assert str(refused.value) == str(refused.value).strip()


def test_the_longest_expression_is_accepted() -> None:
    """256 characters are allowed."""
    expression = "00" + ",0" * 123 + " 0 * * *"

    assert len(expression) == 256
    assert Cron.parse(expression).minutes == (0,)


def test_time_zones_are_checked() -> None:
    """Zones are IANA names; unknown or malformed ones are refused."""
    assert zone(CAIRO).key == CAIRO
    for name in ("Mars/Olympus", "../etc/passwd", "/etc/localtime", ""):
        with pytest.raises(CronError) as refused:
            zone(name)
        assert str(refused.value) == (
            f"Unknown time zone {name!r}; use an IANA name such as 'Africa/Cairo' "
            "(on Windows, install domainkit[tzdata])."
        )
    with pytest.raises(CronError, match="Unknown time zone 'Nowhere'"):
        Cron.parse("0 0 * * *", "Nowhere")


@pytest.mark.parametrize(
    ("expression", "day", "matches"),
    [
        ("0 0 1 * MON", date(2026, 10, 1), True),  # The first, a Thursday.
        ("0 0 1 * MON", date(2026, 10, 19), True),  # A Monday.
        ("0 0 1 * MON", date(2026, 10, 20), False),
        ("0 0 */2 * MON", date(2026, 10, 26), False),  # '*' day: both must.
        ("0 0 */2 * MON", date(2026, 10, 5), True),
        ("0 0 13 * */7", date(2026, 9, 13), True),  # A Sunday the 13th.
        ("0 0 13 * */7", date(2026, 10, 13), False),
        ("0 0 * * *", date(2026, 2, 28), True),
        ("0 0 * 3 *", date(2026, 2, 28), False),
        ("0 0 * 3 MON", date(2026, 3, 2), True),
        ("0 0 1 3 MON", date(2026, 4, 6), False),  # Right weekday, wrong month.
    ],
)
def test_days_match_by_either_field_when_both_are_restricted(
    expression: str, day: date, matches: bool
) -> None:
    """As in cron: either restricted day field; '*' in one makes both count."""
    assert Cron.parse(expression).matches_day(day) is matches


def test_next_runs_follow_the_schedule() -> None:
    """Runs come strictly after the given moment, across months and years."""
    assert runs("0 3 * * 1-5", "UTC", at(2026, 10, 16, 3), 3) == [
        "10-19 03:00+0000",
        "10-20 03:00+0000",
        "10-21 03:00+0000",
    ]
    assert runs("30 12 31 * *", "UTC", at(2026, 10, 31, 12, 30), 2) == [
        "12-31 12:30+0000",
        "01-31 12:30+0000",
    ]
    assert runs("0 0 29 2 *", "UTC", at(2026, 1, 1), 2) == [
        "02-29 00:00+0000",
        "02-29 00:00+0000",
    ]
    schedule = Cron.parse("*/15 * * * *")
    assert schedule.next_after(at(2026, 1, 1, 0, 14, 59, 999999)) == at(
        2026, 1, 1, 0, 15
    )
    assert schedule.next_after(at(2026, 1, 1, 0, 15)) == at(2026, 1, 1, 0, 30)


def test_times_are_wall_clock_times_in_the_zone() -> None:
    """3:00 in Cairo is 1:00 UTC in winter, midnight in summer."""
    assert runs("0 3 * * 1-5", CAIRO, at(2026, 10, 19), 2) == [
        "10-20 03:00+0300",
        "10-21 03:00+0300",
    ]
    assert Cron.parse("0 3 * * *", CAIRO).next_after(at(2026, 12, 1)) == at(
        2026, 12, 1, 1
    )


def test_a_time_skipped_by_spring_forward_runs_at_the_end_of_the_gap() -> None:
    """02:30 does not exist on 8 March in New York: it runs once, at 03:00."""
    assert runs("30 2 * * *", NEW_YORK, at(2026, 3, 7, 12), 3) == [
        "03-08 03:00-0400",
        "03-09 02:30-0400",
        "03-10 02:30-0400",
    ]
    # Cairo jumps from midnight to 01:00 on the last Friday of April.
    assert runs("30 0 * * *", CAIRO, at(2026, 4, 23, 12), 2) == [
        "04-24 01:00+0300",
        "04-25 00:30+0300",
    ]


def test_a_gap_keeps_frequent_schedules_on_pace() -> None:
    """Every half hour: the times in the gap run once, at its end."""
    assert runs("*/30 * * * *", NEW_YORK, at(2026, 3, 8, 5), 5) == [
        "03-08 00:30-0500",
        "03-08 01:00-0500",
        "03-08 01:30-0500",
        "03-08 03:00-0400",
        "03-08 03:30-0400",
    ]


def test_a_time_repeated_by_fall_back_runs_once() -> None:
    """01:30 happens twice on 1 November in New York: it runs the first time."""
    assert runs("30 1 * * *", NEW_YORK, at(2026, 10, 31, 12), 2) == [
        "11-01 01:30-0400",
        "11-02 01:30-0500",
    ]
    # Cairo goes from midnight back to 23:00 on the last Thursday of October.
    assert runs("30 23 * * *", CAIRO, at(2026, 10, 29, 12), 2) == [
        "10-29 23:30+0300",
        "10-30 23:30+0200",
    ]


def test_wildcard_hours_run_in_both_passes_of_a_repeated_hour() -> None:
    """Every half hour keeps its pace in elapsed time through a fall-back."""
    assert runs("*/30 * * * *", NEW_YORK, at(2026, 11, 1, 4, 15), 6) == [
        "11-01 00:30-0400",
        "11-01 01:00-0400",
        "11-01 01:30-0400",
        "11-01 01:00-0500",
        "11-01 01:30-0500",
        "11-01 02:00-0500",
    ]
    assert runs("0 */1 * * *", NEW_YORK, at(2026, 11, 1, 4, 30), 3) == [
        "11-01 01:00-0400",
        "11-01 01:00-0500",
        "11-01 02:00-0500",
    ]


def test_a_skipped_day_runs_once_after_it() -> None:
    """Samoa skipped 30 December 2011: noon that day ran at its end."""
    schedule = Cron.parse("0 12 * * *", "Pacific/Apia")

    assert schedule.next_after(at(2011, 12, 29)) == at(2011, 12, 29, 22)
    assert schedule.next_after(at(2011, 12, 29, 22)) == at(2011, 12, 30, 10)
    assert schedule.next_after(at(2011, 12, 30, 10)) == at(2011, 12, 30, 22)


def test_instants_of_ordinary_repeated_and_skipped_times() -> None:
    """One moment, the first of two, or the end of the gap."""
    fixed = Cron.parse("30 1 * * *", NEW_YORK)
    wildcard = Cron.parse("30 * * * *", NEW_YORK)
    repeated = datetime(2026, 11, 1, 1, 30)  # noqa: DTZ001 - a wall-clock time.
    skipped = datetime(2026, 3, 8, 2, 30)  # noqa: DTZ001

    assert fixed.instants(datetime(2026, 6, 1, 1, 30)) == [at(2026, 6, 1, 5, 30)]  # noqa: DTZ001
    assert fixed.instants(repeated) == [at(2026, 11, 1, 5, 30)]
    assert wildcard.instants(repeated) == [
        at(2026, 11, 1, 5, 30),
        at(2026, 11, 1, 6, 30),
    ]
    assert fixed.instants(skipped) == [at(2026, 3, 8, 7)]
    assert wildcard.instants(skipped) == [at(2026, 3, 8, 7)]


def test_runs_beyond_the_search_window_are_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A schedule with no run in the window has no next run."""
    schedule = Cron.parse("0 0 29 2 *")
    monkeypatch.setattr(cron_module, "SEARCH_DAYS", 30)

    assert schedule.next_after(at(2026, 1, 1)) is None
    assert schedule.next_after(at(2028, 2, 1)) == at(2028, 2, 29)


EXPRESSIONS = st.sampled_from(
    [
        "* * * * *",
        "*/7 * * * *",
        "30 2 * * *",
        "30 1 * * *",
        "0,30 0-3 * * *",
        "15 */2 * * MON-FRI",
        "0 0 1 * MON",
        "45 23 * * *",
    ]
)
ZONES = st.sampled_from(
    ["UTC", NEW_YORK, CAIRO, "Europe/London", "Australia/Lord_Howe"]
)
MOMENTS = st.datetimes(
    min_value=datetime(2024, 1, 1),  # noqa: DTZ001 - hypothesis wants naive bounds.
    max_value=datetime(2030, 12, 31),  # noqa: DTZ001
    timezones=st.just(UTC),
)


@settings(max_examples=100, deadline=None)
@given(EXPRESSIONS, ZONES, MOMENTS)
def test_runs_never_repeat_and_never_skip_a_time(
    expression: str, tz: str, start: datetime
) -> None:
    """Each run is later than the last, at a matching time or a gap's end.

    Checked minute by minute against the zone's clocks: no matching time
    between two runs is left out, and a repeated time runs twice only for
    '*' hours.
    """
    schedule = Cron.parse(expression, tz)

    def matches(wall: datetime) -> bool:
        return (
            wall.minute in schedule.minutes
            and wall.hour in schedule.hours
            and schedule.matches_day(wall.date())
        )

    previous = start
    for _ in range(6):
        following = schedule.next_after(previous)
        assert following is not None
        assert following > previous
        wall = following.astimezone(schedule.tz)
        if not matches(wall):  # Moved by a gap: clocks just jumped forward.
            before = (following - timedelta(seconds=1)).astimezone(schedule.tz)
            assert before.utcoffset() != wall.utcoffset()
        assert wall.fold == 0 or schedule.any_hour  # Repeats only for '*' hours.
        if following - previous <= timedelta(hours=6):
            minute = previous.replace(second=0, microsecond=0) + timedelta(minutes=1)
            while minute < following:
                local = minute.astimezone(schedule.tz)
                assert not (matches(local) and (local.fold == 0 or schedule.any_hour))
                minute += timedelta(minutes=1)
        previous = following
