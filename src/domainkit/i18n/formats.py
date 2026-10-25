"""Numbers and dates written as each language writes them.

English and Arabic follow the Unicode CLDR's data for ``en`` and ``ar``:
Arabic writes Arabic-Indic digits, with its own decimal and group marks,
and its own month names.
Other languages fall back to their primary subtag (``ar-eg`` to ``ar``),
then to English, until a project registers formats for them::

    from dataclasses import replace

    from domainkit.i18n.formats import ARABIC, register_formats

    register_formats("ar-ma", replace(ARABIC, digits="0123456789", decimal=","))

Aware datetimes are shown in the current time zone (``TIME_ZONE``, see
``timezone``); naive ones are refused, as their zone is unknown.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Final, Literal

from domainkit.i18n.timezone import localtime
from domainkit.i18n.translation import check_language, get_language

__all__ = [
    "ARABIC",
    "date_format",
    "datetime_format",
    "number_format",
    "register_formats",
    "time_format",
]

type DateStyle = Literal["short", "medium", "long", "full"]
type TimeStyle = Literal["short", "medium"]
type Number = int | float | Decimal

_TOKEN: Final = re.compile(r"'(?:[^']|'')*'|([yMdEhHmsa])\1*|.", re.DOTALL)
"""A CLDR pattern's parts: quoted text, a run of one field letter, a character."""


@dataclass(frozen=True, slots=True)
class LocaleFormats:
    """How one language writes numbers and dates (an abridged CLDR locale).

    Date and time patterns use CLDR's letters: ``y`` year (``yy``: two
    digits), ``M`` month (``MMM``, ``MMMM``: its name), ``d`` day, ``EEEE``
    weekday, ``h``/``H`` hour (12 or 24), ``m`` minute, ``s`` second, ``a``
    AM or PM; text in single quotes is kept as it is.
    """

    decimal: str
    group: str
    minus: str
    digits: str
    """The ten digits, zero first."""
    months: tuple[str, ...]
    """January first."""
    months_short: tuple[str, ...]
    weekdays: tuple[str, ...]
    """Monday first, as ``date.weekday()`` counts."""
    weekdays_short: tuple[str, ...]
    day_periods: tuple[str, str]
    """Before and after noon."""
    date_patterns: Mapping[DateStyle, str]
    time_patterns: Mapping[TimeStyle, str]
    datetime_pattern: str
    """How a date and a time join: ``{date}`` and ``{time}``."""


ENGLISH: Final = LocaleFormats(
    decimal=".",
    group=",",
    minus="-",
    digits="0123456789",
    months=(
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    ),
    months_short=(
        "Jan",
        "Feb",
        "Mar",
        "Apr",
        "May",
        "Jun",
        "Jul",
        "Aug",
        "Sep",
        "Oct",
        "Nov",
        "Dec",
    ),
    weekdays=(
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
    ),
    weekdays_short=("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
    day_periods=("AM", "PM"),
    date_patterns={
        "short": "M/d/yy",
        "medium": "MMM d, y",
        "long": "MMMM d, y",
        "full": "EEEE, MMMM d, y",
    },
    time_patterns={"short": "h:mm a", "medium": "h:mm:ss a"},
    datetime_pattern="{date}, {time}",
)
"""CLDR's ``en``: ``Oct 19, 2026, 3:04 PM``."""

_ARABIC_MONTHS: Final = (
    "يناير",
    "فبراير",
    "مارس",
    "أبريل",
    "مايو",
    "يونيو",
    "يوليو",
    "أغسطس",
    "سبتمبر",
    "أكتوبر",
    "نوفمبر",
    "ديسمبر",
)
_ARABIC_WEEKDAYS: Final = (
    "الاثنين",
    "الثلاثاء",
    "الأربعاء",
    "الخميس",
    "الجمعة",
    "السبت",
    "الأحد",
)
ARABIC: Final = LocaleFormats(
    decimal="\u066b",
    group="\u066c",
    minus="\u061c-",
    digits="\u0660\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669",
    months=_ARABIC_MONTHS,
    months_short=_ARABIC_MONTHS,
    weekdays=_ARABIC_WEEKDAYS,
    weekdays_short=_ARABIC_WEEKDAYS,
    day_periods=("ص", "م"),
    date_patterns={
        "short": "d\u200f/M\u200f/y",
        "medium": "dd\u200f/MM\u200f/y",
        "long": "d MMMM y",
        "full": "EEEE، d MMMM y",
    },
    time_patterns={"short": "h:mm a", "medium": "h:mm:ss a"},
    datetime_pattern="{date}، {time}",
)
"""CLDR's ``ar``: Arabic-Indic digits, Arabic month names, and RLM marks."""

_formats: dict[str, LocaleFormats] = {"en": ENGLISH, "ar": ARABIC}


def register_formats(language: str, formats: LocaleFormats) -> None:
    """Write numbers and dates of ``language`` with ``formats`` from now on."""
    _formats[check_language(language)] = formats


def get_formats(language: str | None = None) -> LocaleFormats:
    """The formats of ``language`` (default: the active language).

    A language without its own falls back to its primary subtag, then to
    English.
    """
    language = language or get_language()
    found = _formats.get(language) or _formats.get(language.partition("-")[0])
    return found or ENGLISH


def number_format(
    value: Number,
    decimal_places: int | None = None,
    *,
    grouping: bool = True,
    language: str | None = None,
) -> str:
    """``value`` with the language's digits and separators (``1,234.5`` in English).

    ``decimal_places`` rounds half to even (as CLDR does); without it, the
    value keeps the places it has. Thousands are grouped unless
    ``grouping`` is False.

    Raises:
        TypeError: ``value`` is a bool.
        ValueError: ``value`` is not finite, or ``decimal_places`` is negative.
    """
    if isinstance(value, bool):
        raise TypeError("number_format() takes numbers, not booleans.")
    amount = Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
    if not amount.is_finite():
        raise ValueError("number_format() takes finite numbers.")
    if decimal_places is not None:
        if decimal_places < 0:
            raise ValueError("decimal_places cannot be negative.")
        with localcontext() as context:  # Room for every digit of large amounts.
            context.prec = max(context.prec, amount.adjusted() + decimal_places + 2)
            amount = amount.quantize(
                Decimal(1).scaleb(-decimal_places), ROUND_HALF_EVEN
            )
    formats = get_formats(language)
    whole, _, fraction = f"{amount.copy_abs():f}".partition(".")
    if grouping:
        whole = formats.group.join(_groups(whole))
    text = f"{whole}{formats.decimal}{fraction}" if fraction else whole
    sign = formats.minus if amount < 0 else ""
    return sign + _digits(text, formats)


def _groups(whole: str) -> list[str]:
    """The integer digits in threes, from the right."""
    head = len(whole) % 3 or 3
    return [whole[:head], *(whole[i : i + 3] for i in range(head, len(whole), 3))]


def _digits(text: str, formats: LocaleFormats) -> str:
    """``text`` with ASCII digits replaced by the language's."""
    return text.translate(str.maketrans("0123456789", formats.digits))


def date_format(
    value: date, style: DateStyle = "medium", *, language: str | None = None
) -> str:
    """A date, or the local date of an aware datetime: ``Oct 19, 2026``."""
    formats = get_formats(language)
    if isinstance(value, datetime):
        value = localtime(value)
    return _render(formats.date_patterns[style], value, formats)


def time_format(
    value: time | datetime, style: TimeStyle = "short", *, language: str | None = None
) -> str:
    """A time, or the local time of an aware datetime: ``3:04 PM``."""
    formats = get_formats(language)
    if isinstance(value, datetime):
        value = localtime(value)
    return _render(formats.time_patterns[style], value, formats)


def datetime_format(
    value: datetime,
    date_style: DateStyle = "medium",
    time_style: TimeStyle = "short",
    *,
    language: str | None = None,
) -> str:
    """An aware datetime in the current zone: ``Oct 19, 2026, 3:04 PM``."""
    formats = get_formats(language)
    local = localtime(value)
    return formats.datetime_pattern.format(
        date=_render(formats.date_patterns[date_style], local, formats),
        time=_render(formats.time_patterns[time_style], local, formats),
    )


def _render(pattern: str, value: date | time, formats: LocaleFormats) -> str:
    """Fill a CLDR pattern with the fields of ``value``."""
    parts = []
    for match in _TOKEN.finditer(pattern):
        token = match[0]
        if match[1] is not None:
            parts.append(_field(token, value, formats))
        elif len(token) > 1:  # Quoted text; '' is a quote.
            parts.append(token[1:-1].replace("''", "'") or "'")
        else:
            parts.append(token)
    return "".join(parts)


def _field(token: str, value: date | time, formats: LocaleFormats) -> str:
    """One field of a pattern (``MMMM``, ``d``, ``a``, ...) for ``value``."""
    letter, width = token[0], len(token)
    if letter in "yMdE":
        if not isinstance(value, date):
            raise ValueError(f"{token!r} needs a date.")
        if letter == "M" and width >= 3:
            names = formats.months if width >= 4 else formats.months_short
            return names[value.month - 1]
        if letter == "E":
            names = formats.weekdays if width >= 4 else formats.weekdays_short
            return names[value.weekday()]
        number = {"y": value.year, "M": value.month, "d": value.day}[letter]
        if letter == "y" and width == 2:
            return _digits(f"{number % 100:02d}", formats)
    else:
        if not isinstance(value, time | datetime):
            raise ValueError(f"{token!r} needs a time.")
        if letter == "a":
            return formats.day_periods[value.hour >= 12]
        hour12 = value.hour % 12 or 12
        number = {"h": hour12, "H": value.hour, "m": value.minute, "s": value.second}[
            letter
        ]
    return _digits(f"{number:0{min(width, 2)}d}", formats)
