"""Limits as values: what a limit may be, windows, movements, and which limit wins."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from domainkit.clock import freeze_time
from domainkit.conf import override_settings
from domainkit.contrib.limits import (
    Direction,
    InvalidLimit,
    Limit,
    LimitDefault,
    LimitRemoved,
    LimitSet,
    Movement,
    Window,
)
from domainkit.contrib.limits.apps import default_problems
from domainkit.contrib.limits.usage import Rule, applicable, default_rule
from domainkit.ddd import DomainError
from domainkit.i18n import gettext, override
from domainkit.money import Money, UnknownCurrency, register_currency


@contextmanager
def refused(match: str) -> Iterator[None]:
    """``InvalidLimit`` with ``match``, its message one the catalogs translate."""
    with pytest.raises(InvalidLimit, match=match) as raised:
        yield
    message = str(raised.value)
    with override("ar"):
        assert gettext(message) != message, message


def egp(amount: str) -> Money:
    """``amount`` Egyptian pounds."""
    return Money.of(amount, "EGP")


def daily(**fields: Any) -> Limit:  # noqa: ANN401 - Limit.define's options.
    """A daily limit of holders in pounds, with ``fields`` replaced."""
    options: dict[str, Any] = {"window": Window.DAY, "max_amount": egp("1000")}
    options.update(fields)
    return Limit.define("wallet.daily", "holder", "EGP", **options)


def test_a_limit_records_what_it_allows() -> None:
    """``LimitSet`` carries its key and bounds; ``remove`` records ``LimitRemoved``."""
    limit = Limit.define(
        "wallet.daily",
        "holder",
        "EGP",
        direction=Direction.IN,
        subject="h-1",
        per_transaction=egp("100"),
        window=Window.MONTH,
        max_amount=egp("1000"),
        max_count=3,
        time_zone="Africa/Cairo",
        enabled=False,
        note="Raised on request",
    )
    limit.remove()

    (made, removed) = limit.pull_events()
    assert isinstance(made, LimitSet)
    assert (made.code, made.kind, made.direction, made.currency, made.subject) == (
        "wallet.daily",
        "holder",
        Direction.IN,
        "EGP",
        "h-1",
    )
    assert (made.per_transaction, made.window, made.max_amount, made.max_count) == (
        egp("100"),
        Window.MONTH,
        egp("1000"),
        3,
    )
    assert (made.time_zone, made.enabled, made.limit_id) == (
        "Africa/Cairo",
        False,
        limit.id,
    )
    assert (limit.note, limit.subject) == ("Raised on request", "h-1")
    assert isinstance(removed, LimitRemoved)
    assert (removed.limit_id, removed.code, removed.kind, removed.subject) == (
        limit.id,
        "wallet.daily",
        "holder",
        "h-1",
    )


def test_a_limit_changes_its_bounds_and_records_each_change() -> None:
    """The bounds change; the key stays."""
    limit = daily()
    with freeze_time(datetime(2026, 11, 1, tzinfo=UTC)):
        limit.change(
            per_transaction=egp("5"),
            window=None,
            max_amount=None,
            max_count=None,
            time_zone="UTC",
            enabled=True,
            note="",
        )

    assert (limit.per_transaction, limit.window, limit.max_amount) == (
        egp("5"),
        None,
        None,
    )
    assert limit.changed_at == datetime(2026, 11, 1, tzinfo=UTC)
    assert [type(each) for each in limit.pull_events()] == [LimitSet, LimitSet]


@pytest.mark.parametrize(
    ("fields", "match"),
    [
        ({"code": "Wallet"}, "A limit's code has up to 64"),
        ({"code": "a" * 65}, "A limit's code has up to 64"),
        ({"kind": "Holder"}, "A kind of subject has up to 32"),
        ({"kind": "h" * 33}, "A kind of subject has up to 32"),
        ({"subject": "a\x00"}, "A subject has 1 to 255"),
        ({"subject": "a" * 256}, "A subject has 1 to 255"),
        ({"currency": "USDC6", "max_amount": None, "max_count": 1}, "at most four"),
        ({"max_amount": Money.of("1", "USD")}, "A limit's amounts are in its"),
        ({"per_transaction": egp("0")}, "A limit's amounts are more than zero"),
        ({"max_amount": egp("-1")}, "A limit's amounts are more than zero"),
        ({"max_count": 0}, "A limit's count is at least one"),
        ({"window": None}, "A window needs a maximum amount or count"),
        ({"max_amount": None}, "A window needs a maximum amount or count"),
        ({"window": None, "max_amount": None}, "A limit sets an amount per"),
        ({"time_zone": "Mars/Olympus"}, "The time zone is not one"),
        ({"time_zone": "../etc"}, "The time zone is not one"),
        ({"note": "n" * 256}, "A limit's note has at most 255"),
    ],
)
def test_a_limit_that_cannot_be_is_refused(fields: dict[str, Any], match: str) -> None:
    """Every part is checked; a currency of more than four places is refused."""
    register_currency("USDC6", minor_units=6)
    options: dict[str, Any] = {
        "code": "wallet.daily",
        "kind": "holder",
        "currency": "EGP",
        "window": Window.DAY,
        "max_amount": egp("1000"),
        **fields,
    }
    code, kind, currency = (options.pop(key) for key in ("code", "kind", "currency"))

    with refused(match):
        Limit.define(code, kind, currency, **options)


def test_a_currency_of_four_places_is_kept() -> None:
    """CLF and UYW have four places: the columns' scale."""
    limit = Limit.define(
        "x", "holder", "CLF", per_transaction=Money.of("1.2345", "CLF")
    )

    assert (limit.currency, limit.per_transaction) == ("CLF", Money.of("1.2345", "CLF"))


def test_an_unknown_currency_is_money_s_error() -> None:
    """``UnknownCurrency``, a 422 ``unknown_currency``."""
    with pytest.raises(UnknownCurrency):
        Limit.define("x", "holder", "XYZ", per_transaction=None)


@pytest.mark.parametrize(
    ("window", "now", "zone", "start"),
    [
        (Window.DAY, "2026-10-30T12:00:00+00:00", "UTC", "2026-10-30T00:00:00+00:00"),
        (
            Window.DAY,
            "2026-10-30T22:30:00+00:00",
            "Africa/Cairo",  # 01:30 on the 31st in Cairo (UTC+2 after DST).
            "2026-10-30T22:00:00+00:00",
        ),
        (
            Window.MONTH,
            "2026-10-30T12:00:00+00:00",
            "UTC",
            "2026-10-01T00:00:00+00:00",
        ),
        (
            Window.MONTH,
            "2026-10-31T23:30:00+00:00",
            "Asia/Tokyo",  # November in Tokyo.
            "2026-10-31T15:00:00+00:00",
        ),
        (
            Window.ROLLING_DAY,
            "2026-10-30T12:00:00+00:00",
            "Asia/Tokyo",
            "2026-10-29T12:00:00+00:00",
        ),
        (
            Window.ROLLING_MONTH,
            "2026-10-30T12:00:00+00:00",
            "UTC",
            "2026-09-30T12:00:00+00:00",
        ),
    ],
)
def test_windows_start_at_midnight_in_their_zone_or_reach_back(
    window: Window, now: str, zone: str, start: str
) -> None:
    """Calendar windows by the limit's zone; rolling ones by 24 hours or 30 days."""
    began = window.start(datetime.fromisoformat(now), zone)

    assert began == datetime.fromisoformat(start)
    assert began.tzinfo is UTC


@pytest.mark.parametrize(
    ("amount", "subjects", "match"),
    [
        ("0", {"holder": "h"}, "A movement's amount is more than zero"),
        ("-1", {"holder": "h"}, "A movement's amount is more than zero"),
        ("1", {}, "A movement names at least one subject"),
        ("1", {"Holder": "h"}, "A kind of subject has up to 32"),
        ("1", {"holder": ""}, "A subject has 1 to 255"),
    ],
)
def test_a_movement_that_cannot_be_is_refused(
    amount: str, subjects: dict[str, str], match: str
) -> None:
    """More than zero, of one subject at least, each well formed."""
    with refused(match):
        Movement(egp(amount), subjects)


def test_a_movement_moves_out_by_default() -> None:
    """Out, of the subjects given."""
    movement = Movement(egp("1"), {"holder": "h", "project_key": "k"})

    assert movement.direction is Direction.OUT
    assert movement == Movement(egp("1"), {"holder": "h", "project_key": "k"})


def rule(code: str, subject: str = "", *, stored: bool = True, **fields: Any) -> Rule:  # noqa: ANN401
    """A daily rule of holders in pounds."""
    values: dict[str, Any] = {
        "code": code,
        "kind": "holder",
        "direction": Direction.OUT,
        "currency": "EGP",
        "subject": subject,
        "per_transaction": None,
        "window": Window.DAY,
        "max_amount": Decimal(100),
        "max_count": None,
        "time_zone": "UTC",
        "enabled": True,
        "stored": stored,
    }
    values.update(fields)
    return Rule(**values)


KEY = ("holder", Direction.OUT, "EGP", "h-1")


def test_one_limit_of_each_code_bounds_a_subject() -> None:
    """For the subject over for every one, then stored over the settings'."""
    every = rule("a")
    mine = rule("a", "h-1", stored=False)
    default = rule("b", stored=False)
    stored = rule("b")
    others = [
        rule("c", "h-2"),
        rule("d", kind="account"),
        rule("e", direction=Direction.IN),
        rule("f", currency="USD"),
    ]

    chosen = applicable(KEY, [every, mine, default, stored, *others])

    assert chosen == [mine, stored]
    assert applicable(KEY, [stored, default]) == [stored]
    assert applicable(KEY, [mine, every]) == [mine]


def test_a_disabled_limit_exempts_its_subject() -> None:
    """The winner bounds nothing when disabled: no fallback to the general one."""
    every = rule("a")
    exempt = rule("a", "h-1", enabled=False)

    assert applicable(KEY, [every, exempt]) == []
    assert applicable(("holder", Direction.OUT, "EGP", "h-2"), [every, exempt]) == [
        every
    ]


def test_defaults_become_rules_in_the_settings_zone() -> None:
    """Text amounts, the default zone, enabled, not stored."""
    with override_settings(TIME_ZONE="Africa/Cairo"):
        made = default_rule(
            LimitDefault(
                code="wallet.daily",
                kind="holder",
                currency="EGP",
                per_transaction="10.50",
                window="day",
                max_amount="100",
                max_count=4,
            )
        )
        bare = default_rule(
            LimitDefault(
                code="x",
                kind="holder",
                currency="EGP",
                per_transaction="1",
                time_zone="UTC",
            )
        )

    assert made == rule(
        "wallet.daily",
        per_transaction=Decimal("10.50"),
        max_amount=Decimal(100),
        max_count=4,
        time_zone="Africa/Cairo",
        stored=False,
    )
    assert (bare.window, bare.max_amount, bare.time_zone) == (None, None, "UTC")


def test_defaults_that_are_not_limits_are_found() -> None:
    """``limits.E002``: each one named, and a key given twice."""
    good = LimitDefault(
        code="wallet.daily", kind="holder", currency="EGP", per_transaction="1"
    )
    problems = default_problems(
        (
            good,
            good,
            LimitDefault(code="x", kind="holder", currency="XYZ", per_transaction="1"),
            LimitDefault(
                code="y",
                kind="holder",
                currency="EGP",
                direction="sideways",
                per_transaction="1",
            ),
            LimitDefault(
                code="z", kind="holder", currency="EGP", window="week", max_count=1
            ),
            LimitDefault(
                code="w", kind="holder", currency="JPY", per_transaction="1.5"
            ),
            LimitDefault(code="v", kind="holder", currency="EGP"),
        )
    )

    assert [problem.id for problem in problems] == ["limits.E002"] * 6
    assert problems[0] == (
        "LIMITS['DEFAULTS'][1] ('wallet.daily'): another default has the same key."
    )
    assert problems[1].startswith("LIMITS['DEFAULTS'][2] ('x'): ")
    assert problems[5] == (
        "LIMITS['DEFAULTS'][6] ('v'): A limit sets an amount per transaction, or a "
        "maximum over a window."
    )
    assert default_problems((good,)) == []
    assert issubclass(InvalidLimit, DomainError)


def test_a_limit_made_bare_is_for_every_subject_enabled_without_a_note() -> None:
    """The defaults of ``define``; a note of 255 characters is taken."""
    limit = Limit.define("x", "holder", "EGP", per_transaction=egp("1"))
    limit.change(
        per_transaction=egp("1"),
        window=None,
        max_amount=None,
        max_count=None,
        time_zone="UTC",
        enabled=True,
        note="n" * 255,
    )

    assert (limit.subject, limit.direction, limit.time_zone) == (
        "",
        Direction.OUT,
        "UTC",
    )
    first = limit.pull_events()[0]
    assert isinstance(first, LimitSet)
    assert (first.subject, first.enabled) == ("", True)
    assert Limit.define("x", "holder", "EGP", per_transaction=egp("1")).note == ""


def test_a_stored_limit_is_a_rule_of_its_own_bounds() -> None:
    """``Rule.of`` keeps every field, as stored."""
    limit = Limit.define(
        "wallet.daily",
        "holder",
        "EGP",
        subject="h-1",
        direction=Direction.IN,
        per_transaction=egp("5"),
        window=Window.DAY,
        max_amount=egp("100"),
        max_count=3,
        time_zone="Africa/Cairo",
        enabled=False,
    )

    assert Rule.of(limit) == rule(
        "wallet.daily",
        "h-1",
        direction=Direction.IN,
        per_transaction=Decimal("5.00"),
        max_count=3,
        time_zone="Africa/Cairo",
        enabled=False,
    )


def test_a_calendar_window_starts_at_its_midnight_to_the_microsecond() -> None:
    """Seconds and microseconds are dropped, not kept from now."""
    now = datetime(2026, 10, 30, 12, 34, 56, 789, tzinfo=UTC)

    assert Window.DAY.start(now, "UTC") == datetime(2026, 10, 30, tzinfo=UTC)
    assert Window.MONTH.start(now, "UTC") == datetime(2026, 10, 1, tzinfo=UTC)


def test_a_breach_names_no_limit_unless_told() -> None:
    """``limit`` and ``window`` are empty by default."""
    from domainkit.contrib.limits import LimitExceeded

    bare = LimitExceeded("The amount is more than a limit allows in its period.")

    assert (bare.limit, bare.window, bare.code) == ("", "", "limit_exceeded")


def test_usage_over_http_says_where_its_limit_comes_from() -> None:
    """Every field of a usage, and ``settings`` for a default."""
    from domainkit.contrib.limits import Usage
    from domainkit.contrib.limits.api import UsageOut

    usage = Usage(
        code="daily",
        kind="holder",
        direction=Direction.OUT,
        subject="",
        window=Window.DAY,
        since=datetime(2026, 10, 30, tzinfo=UTC),
        used=egp("30"),
        count=1,
        per_transaction=egp("80"),
        max_amount=egp("100"),
        max_count=5,
        stored=False,
    )

    shown = UsageOut.of(usage).model_dump(mode="json")

    assert shown == {
        "code": "daily",
        "kind": "holder",
        "direction": "out",
        "subject": "",
        "window": "day",
        "since": "2026-10-30T00:00:00Z",
        "used": {"amount": "30.00", "currency": "EGP"},
        "count": 1,
        "remaining": {"amount": "70.00", "currency": "EGP"},
        "per_transaction": {"amount": "80.00", "currency": "EGP"},
        "max_amount": {"amount": "100.00", "currency": "EGP"},
        "max_count": 5,
        "origin": "settings",
    }


def test_valid_defaults_pass_and_each_part_of_a_bad_one_is_checked() -> None:
    """Windowed defaults pass; a bad subject, zone, or note does not."""
    windowed = LimitDefault(
        code="daily", kind="holder", currency="EGP", window="day", max_amount="100"
    )
    counted = LimitDefault(
        code="count",
        kind="holder",
        currency="EGP",
        window="month",
        max_count=3,
        direction="in",
        subject="h-1",
    )

    assert default_problems((windowed, counted)) == []
    bad = default_problems(
        (
            LimitDefault(
                code="a",
                kind="holder",
                currency="EGP",
                subject="\x00",
                per_transaction="1",
            ),
            LimitDefault(
                code="b",
                kind="holder",
                currency="EGP",
                time_zone="Mars/Olympus",
                per_transaction="1",
            ),
            LimitDefault(
                code="c",
                kind="holder",
                currency="EGP",
                note="n" * 256,
                per_transaction="1",
            ),
        )
    )
    assert [problem.split(": ", 1)[1] for problem in bad] == [
        "A subject has 1 to 255 printable characters.",
        "The time zone is not one, such as 'Africa/Cairo'.",
        "A limit's note has at most 255 characters.",
    ]
