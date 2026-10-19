"""Time zones for display, and numbers and dates as each language writes them."""

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta, timezone, tzinfo
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from domainkit.clock import freeze_time
from domainkit.conf import override_settings
from domainkit.i18n import (
    date_format,
    datetime_format,
    localdate,
    localtime,
    number_format,
    override,
    time_format,
)
from domainkit.i18n import timezone as tz
from domainkit.i18n.formats import ARABIC, ENGLISH, get_formats, register_formats
from domainkit.i18n.formats import _formats as registered

AR_DIGITS = "\u0660\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669"
RLM = "\u200f"
MOMENT = datetime(2026, 10, 19, 22, 4, 5, tzinfo=UTC)
"""A Monday evening in UTC: already Tuesday in Cairo."""


def arabic(text: str) -> str:
    """``text`` with ASCII digits written as Arabic-Indic ones."""
    return text.translate(str.maketrans("0123456789", AR_DIGITS))


# Time zones --------------------------------------------------------------------------


def test_times_are_shown_in_the_settings_zone() -> None:
    """``localtime`` and ``localdate`` use ``TIME_ZONE``; UTC by default."""
    assert localtime(MOMENT) == MOMENT
    assert localtime(MOMENT).tzinfo == ZoneInfo("UTC")
    with override_settings(TIME_ZONE="Africa/Cairo"):
        cairo = localtime(MOMENT)
        assert (cairo.hour, cairo.utcoffset()) == (1, timedelta(hours=3))
        assert cairo == MOMENT  # The same moment.
        assert localdate(MOMENT) == date(2026, 10, 20)
        assert tz.get_default_timezone() == ZoneInfo("Africa/Cairo")


def test_a_block_can_show_another_zone() -> None:
    """``override`` takes a name or a ``tzinfo``, and restores the zone after."""
    fixed = timezone(timedelta(hours=-5))
    with tz.override("Asia/Tokyo"):
        assert localtime(MOMENT).hour == 7
        with tz.override(fixed):
            assert localtime(MOMENT).hour == 17
            assert tz.get_current_timezone() is fixed
        assert tz.get_current_timezone() == ZoneInfo("Asia/Tokyo")
    assert tz.get_current_timezone() == ZoneInfo("UTC")
    assert localtime(MOMENT, ZoneInfo("Asia/Tokyo")).hour == 7
    assert localdate(MOMENT, ZoneInfo("Asia/Tokyo")) == date(2026, 10, 20)


def test_now_is_aware_and_utc() -> None:
    """What to store; ``localtime()`` and ``localdate()`` default to it."""
    now = tz.now()
    assert now.tzinfo is UTC
    assert abs(localtime() - now) < timedelta(seconds=5)
    with override_settings(TIME_ZONE="Pacific/Kiritimati"):  # UTC+14
        assert localtime().utcoffset() == timedelta(hours=14)
        assert localdate() == localtime().date()


def test_now_follows_the_frozen_clock() -> None:
    """``now()`` is the framework's clock, so tests can freeze "today" too."""
    with (
        freeze_time("2026-10-19T22:30:00Z"),
        override_settings(TIME_ZONE="Pacific/Kiritimati"),
    ):
        assert tz.now() == datetime(2026, 10, 19, 22, 30, tzinfo=UTC)
        assert localdate() == date(2026, 10, 20)


def test_naive_datetimes_have_no_local_time() -> None:
    """Their zone is unknown, so they are refused."""
    naive = datetime(2026, 10, 19, 12)  # noqa: DTZ001 - naive on purpose.
    with pytest.raises(ValueError, match="aware datetime"):
        localtime(naive)
    unknown_offset = datetime(2026, 10, 19, tzinfo=NoOffset())
    with pytest.raises(ValueError, match="aware datetime"):
        localtime(unknown_offset)


class NoOffset(tzinfo):
    """A ``tzinfo`` that does not know its offset (Python calls such times naive)."""

    def utcoffset(self, dt: datetime | None) -> None:
        """Unknown."""
        return

    def dst(self, dt: datetime | None) -> None:
        """Unknown."""
        return

    def tzname(self, dt: datetime | None) -> None:
        """None."""
        return


# Numbers -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "places", "english"),
    [
        (0, None, "0"),
        (7, None, "7"),
        (999, None, "999"),
        (1000, None, "1,000"),
        (1234567, None, "1,234,567"),
        (-1234567, None, "-1,234,567"),
        (1234.5, None, "1,234.5"),
        (0.1 + 0.2, None, "0.30000000000000004"),
        (1e16, None, "10,000,000,000,000,000"),
        (Decimal("1E+3"), None, "1,000"),
        (Decimal("1234.500"), None, "1,234.500"),
        (Decimal("2.675"), 2, "2.68"),
        (Decimal("2.665"), 2, "2.66"),
        (Decimal("-0.001"), 2, "0.00"),
        (Decimal("-0.005"), 2, "0.00"),
        (Decimal("-0.015"), 2, "-0.02"),
        (12, 2, "12.00"),
        (Decimal("1234.5"), 0, "1,234"),
        (10**30, 2, "1,000,000,000,000,000,000,000,000,000,000.00"),
    ],
)
def test_numbers_in_english(
    value: int | float | Decimal, places: int | None, english: str
) -> None:
    """Groups of three, a point, half to even, and a minus only below zero."""
    assert number_format(value, places) == english


def test_numbers_in_arabic() -> None:
    """Arabic-Indic digits, the Arabic decimal and group marks, a marked minus."""
    with override("ar"):
        assert number_format(-1234567.5) == (
            "\u061c-"
            + arabic("1")
            + "\u066c"
            + arabic("234")
            + "\u066c"
            + arabic("567")
            + "\u066b"
            + arabic("5")
        )
    assert number_format(1234, language="ar") == arabic("1") + "\u066c" + arabic("234")
    assert number_format(1234, grouping=False, language="ar") == arabic("1234")
    assert number_format(1234, grouping=False) == "1234"


def test_numbers_are_checked() -> None:
    """Booleans, infinities, and negative places are refused."""
    with pytest.raises(TypeError, match="not booleans"):
        number_format(True)
    for value in (float("inf"), float("nan"), Decimal("-Infinity")):
        with pytest.raises(ValueError, match="finite numbers"):
            number_format(value)
    with pytest.raises(ValueError, match="cannot be negative"):
        number_format(1, -1)


def test_languages_without_formats_fall_back() -> None:
    """To their primary subtag, then to English; registered ones are used."""
    assert get_formats("ar-eg") is ARABIC
    assert get_formats("fr") is ENGLISH
    with override("ar"):
        assert get_formats() is ARABIC
    latin = replace(ARABIC, digits="0123456789", decimal=",")
    register_formats("ar-ma", latin)
    try:
        assert number_format(1234.5, language="ar-ma") == "1\u066c234,5"
        assert get_formats("ar-ma") is latin
        with pytest.raises(ValueError, match="language code"):
            register_formats("Arabic", latin)
    finally:
        del registered["ar-ma"]


# Dates -------------------------------------------------------------------------------

DAY = date(2026, 10, 5)  # A Monday.


@pytest.mark.parametrize(
    ("style", "english", "arabic_text"),
    [
        ("short", "10/5/26", f"5{RLM}/10{RLM}/2026"),
        ("medium", "Oct 5, 2026", f"05{RLM}/10{RLM}/2026"),
        ("long", "October 5, 2026", "5 أكتوبر 2026"),
        ("full", "Monday, October 5, 2026", "الاثنين، 5 أكتوبر 2026"),
    ],
)
def test_dates_in_each_style(style: str, english: str, arabic_text: str) -> None:
    """CLDR's patterns for ``en`` and ``ar``."""
    assert date_format(DAY, style) == english  # type: ignore[arg-type]
    assert date_format(DAY, style, language="ar") == arabic(arabic_text)  # type: ignore[arg-type]


def test_every_month_and_weekday_has_its_name() -> None:
    """Twelve months and seven weekdays, in order, short and long."""
    assert [date_format(date(2026, month, 1), "long") for month in (1, 5, 12)] == [
        "January 1, 2026",
        "May 1, 2026",
        "December 1, 2026",
    ]
    week = [date(2026, 10, day) for day in range(5, 12)]
    assert [date_format(day, "full").split(",")[0] for day in week] == list(
        ENGLISH.weekdays
    )
    assert [
        date_format(day, "full", language="ar").split("،")[0] for day in week
    ] == list(ARABIC.weekdays)
    assert len(ARABIC.months) == len(ENGLISH.months_short) == 12


def test_times_in_each_style() -> None:
    """Twelve-hour clocks with day periods; noon and midnight are 12."""
    assert time_format(time(0, 5)) == "12:05 AM"
    assert time_format(time(12, 0)) == "12:00 PM"
    assert time_format(time(15, 4, 5), "medium") == "3:04:05 PM"
    assert time_format(time(9, 30), language="ar") == arabic("9:30") + " ص"
    assert time_format(time(21, 30), language="ar") == arabic("9:30") + " م"


def test_aware_datetimes_are_shown_where_the_business_is() -> None:
    """Dates and times of datetimes are local; naive ones are refused."""
    with override_settings(TIME_ZONE="Africa/Cairo"):
        assert date_format(MOMENT) == "Oct 20, 2026"
        assert time_format(MOMENT) == "1:04 AM"
        assert datetime_format(MOMENT) == "Oct 20, 2026, 1:04 AM"
        assert datetime_format(MOMENT, "long", "medium", language="ar") == (
            arabic("20") + " أكتوبر " + arabic("2026") + "، " + arabic("1:04:05") + " ص"
        )
    naive = datetime(2026, 10, 19)  # noqa: DTZ001 - naive on purpose.
    for function in (date_format, time_format, datetime_format):
        with pytest.raises(ValueError, match="aware datetime"):
            function(naive)


def test_patterns_read_cldr_letters() -> None:
    """Quoted text, two-digit years, 24-hour clocks, and short names."""
    custom = replace(
        ENGLISH,
        date_patterns={**ENGLISH.date_patterns, "medium": "EEE d MMM ''yy 'at home'"},
        time_patterns={**ENGLISH.time_patterns, "short": "HH:mm:ss 'o''clock' h a"},
    )
    register_formats("en-xx", custom)
    try:
        assert date_format(DAY, language="en-xx") == "Mon 5 Oct '26 at home"
        assert time_format(time(7, 8, 9), language="en-xx") == "07:08:09 o'clock 7 AM"
    finally:
        del registered["en-xx"]
    broken = replace(
        ENGLISH, date_patterns={"medium": "h"}, time_patterns={"short": "y"}
    )
    register_formats("en-yy", broken)
    try:
        with pytest.raises(ValueError, match="needs a time"):
            date_format(DAY, language="en-yy")
        with pytest.raises(ValueError, match="needs a date"):
            time_format(time(1), language="en-yy")
    finally:
        del registered["en-yy"]
