"""Running limits: setup's checks, the command, the schedule, and privacy."""

import io
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, Final, cast

import pytest
from sqlalchemy import select

from domainkit import setup
from domainkit.apps import registry
from domainkit.apps.hooks import on_erase_subject, on_export_subject
from domainkit.checks import CheckFailed
from domainkit.clock import freeze_time
from domainkit.conf import override_settings
from domainkit.contrib.accounting import on_booking
from domainkit.contrib.limits import (
    LimitsUnitOfWork,
    Movement,
    SetLimit,
    Window,
    consume,
    set_limit,
)
from domainkit.contrib.limits.tables import subjects
from domainkit.contrib.limits.tasks import PURGE_USAGE
from domainkit.contrib.messaging.registry import periodic_tasks
from domainkit.db import connections
from domainkit.management import CommandError, call_command, operators
from domainkit.management.base import run_async
from domainkit.money import Money
from domainkit_tests.conftest import ALPHA, Migrated, domain_warnings
from domainkit_tests.test_limits import ACCOUNTING, LIMITED, LIMITS, egp

MESSAGING: Final = "domainkit.contrib.messaging"
NOON: Final = datetime(2026, 10, 30, 12, tzinfo=UTC)
SQLITE: Final = {"default": {"URL": "sqlite+aiosqlite://"}}


@pytest.fixture
def project(migrated: Migrated) -> Iterator[str]:
    """Limits in a migrated database, for commands (which run their own loop)."""
    with migrated(LIMITED) as url:
        yield url


type Action = tuple[str, str, dict[str, object]]


@pytest.fixture
def actions(monkeypatch: pytest.MonkeyPatch) -> list[Action]:
    """The operator actions the command records: ``(action, outcome, details)``."""
    found: list[Action] = []

    async def record(
        who: object, action: str, outcome: str, details: dict[str, object]
    ) -> None:
        found.append((action, outcome, details))

    monkeypatch.setattr(operators, "_record", record)
    return found


def run(*args: str) -> str:
    """``limits`` with ``args``; what it printed."""
    output = io.StringIO()
    call_command("limits", "--operator", "tester", *args, stdout=output)
    return output.getvalue()


def spend(amount: str, holder: str = "h-1") -> None:
    """Count ``amount`` pounds out of ``holder``, at noon."""

    async def count() -> None:
        with freeze_time(NOON):
            async with LimitsUnitOfWork() as uow:
                await consume(uow, Movement(egp(amount), {"holder": holder}))
                await uow.commit()

    run_async(count())


def subjects_named(subject: str) -> list[str]:
    """The kinds of the subject rows of ``subject``."""

    async def read() -> list[str]:
        async with connections.engine().connect() as connection:
            found = await connection.scalars(
                select(subjects.c.kind).where(subjects.c.subject == subject)
            )
            return list(found)

    return run_async(read())


@pytest.mark.usefixtures("project")
def test_the_command_lists_in_order_and_keeps_what_set_leaves_out() -> None:
    """By code, kind, currency, subject; ``set`` changes only what it is given."""
    for code, kind, currency, subject in (
        ("b", "holder", "EGP", ""),
        ("a", "holder", "USD", ""),
        ("a", "holder", "EGP", "h-2"),
        ("a", "holder", "EGP", ""),
        ("a", "account", "EGP", ""),
    ):
        run(
            "set",
            code,
            "--kind",
            kind,
            "--currency",
            currency,
            "--subject",
            subject,
            "--per-transaction",
            "5",
        )
    run(
        "set",
        "b",
        "--kind",
        "holder",
        "--currency",
        "EGP",
        "--direction",
        "in",
        "--window",
        "day",
        "--max-count",
        "2",
        "--time-zone",
        "Africa/Cairo",
        "--note",
        "kept",
        "--disable",
    )
    run(
        "set",
        "b",
        "--kind",
        "holder",
        "--currency",
        "EGP",
        "--direction",
        "in",
        "--max-count",
        "3",
    )
    with override_settings(
        LIMITS={
            "DEFAULTS": [
                {
                    "code": "z",
                    "kind": "account",
                    "currency": "EGP",
                    "per_transaction": "1",
                },
                {
                    "code": "y",
                    "kind": "holder",
                    "currency": "EGP",
                    "per_transaction": "1",
                },
            ]
        }
    ):
        listed = run("list").splitlines()
        holders = run("list", "--kind", "holder").splitlines()

    assert listed == [
        "a: every account, out EGP: at most 5.00 EGP each [stored]",
        "a: every holder, out EGP: at most 5.00 EGP each [stored]",
        "a: holder 'h-2', out EGP: at most 5.00 EGP each [stored]",
        "a: every holder, out USD: at most 5.00 USD each [stored]",
        "b: every holder, in EGP: 3 a day [stored] (disabled)",
        "b: every holder, out EGP: at most 5.00 EGP each [stored]",
        "z: every account, out EGP: at most 1.00 EGP each [settings]",
        "y: every holder, out EGP: at most 1.00 EGP each [settings]",
    ]
    assert holders == [line for line in listed if "account" not in line]

    async def stored() -> tuple[object, ...]:
        async with LimitsUnitOfWork(readonly=True) as uow:
            (found,) = [
                each
                for each in await uow.limits.filter()
                if each.code == "b" and each.direction == "in"
            ]
        return found.time_zone, found.note, found.enabled, found.window

    assert run_async(stored()) == ("Africa/Cairo", "kept", False, Window.DAY)
    run(
        "set",
        "b",
        "--kind",
        "holder",
        "--currency",
        "EGP",
        "--direction",
        "in",
        "--window",
        "none",
        "--max-count",
        "none",
        "--per-transaction",
        "2",
        "--enable",
    )
    assert run_async(stored()) == ("Africa/Cairo", "kept", True, None)


@pytest.mark.usefixtures("project")
def test_the_command_refuses_options_it_does_not_take() -> None:
    """A set without its kind or currency, a direction or window it has not."""
    for args in (
        ("set", "x", "--currency", "EGP"),
        ("set", "x", "--kind", "holder"),
        ("set", "x", "--kind", "holder", "--currency", "EGP", "--direction", "up"),
        ("set", "x", "--kind", "holder", "--currency", "EGP", "--window", "week"),
    ):
        with pytest.raises((CommandError, SystemExit)):
            run(*args)
    from domainkit.contrib.limits.management.commands.limits import Command

    shown = Command().create_parser("manage.py", "limits").format_help()
    for text in (
        "every limit, stored or default",
        "what a subject used of its limits",
        "make a limit or change its bounds",
        "delete usage older than LIMITS.KEEP_USAGE",
    ):
        assert text in shown
    (actions,) = [
        each
        for each in Command().create_parser("manage.py", "limits")._actions
        if each.dest == "action"
    ]
    setting = actions.choices["set"].format_help()  # type: ignore[index]
    for text in (
        "--subject SUBJECT",
        "one subject; none: every one",
        "{out,in}",
        "{day,month,rolling_day,rolling_month,none}",
        "an amount, or none",
        "a count, or none",
    ):
        assert text in " ".join(setting.split()), text


@pytest.mark.usefixtures("project")
def test_the_command_sets_lists_and_shows_usage() -> None:
    """``set`` makes or changes; ``list`` shows all; ``usage`` shows the windows."""
    assert run("list") == "No limits.\n"

    made = run(
        "set",
        "wallet.daily",
        "--kind",
        "holder",
        "--currency",
        "egp",
        "--window",
        "day",
        "--max-amount",
        "100",
        "--per-transaction",
        "80",
        "--note",
        "agreed",
    )
    assert made == (
        "wallet.daily: every holder, out EGP: at most 80.00 EGP each, "
        "100.00 EGP a day [stored]\n"
    )
    changed = run(
        "set",
        "wallet.daily",
        "--kind",
        "holder",
        "--currency",
        "EGP",
        "--max-count",
        "3",
        "--per-transaction",
        "none",
        "--disable",
    )
    assert changed == (
        "wallet.daily: every holder, out EGP: 100.00 EGP a day, 3 a day [stored] "
        "(disabled)\n"
    )
    run(
        "set",
        "wallet.daily",
        "--kind",
        "holder",
        "--currency",
        "EGP",
        "--enable",
        "--subject",
        "h-9",
        "--window",
        "rolling_day",
        "--max-count",
        "1",
    )
    run(
        "set",
        "wallet.daily",
        "--kind",
        "holder",
        "--currency",
        "EGP",
        "--enable",
        "--max-count",
        "none",
        "--window",
        "month",
    )
    spend("30")

    with override_settings(
        LIMITS={
            "DEFAULTS": [
                {
                    "code": "each",
                    "kind": "account",
                    "currency": "USD",
                    "per_transaction": "5",
                }
            ]
        }
    ):
        assert run("list").splitlines() == [
            "wallet.daily: every holder, out EGP: 100.00 EGP a month [stored]",
            "wallet.daily: holder 'h-9', out EGP: 1 a rolling_day [stored]",
            "each: every account, out USD: at most 5.00 USD each [settings]",
        ]
        assert run("list", "--kind", "account").splitlines() == [
            "each: every account, out USD: at most 5.00 USD each [settings]",
        ]
        assert run("usage", "account", "a-1").splitlines() == [
            "each: at most 5.00 USD each (out)"
        ]
    with freeze_time(NOON):
        assert run("usage", "holder", "h-1") == (
            "wallet.daily: 30.00 EGP of 100.00 EGP in 1 movement(s) since "
            "2026-10-01T00:00:00+00:00 (out)\n"
        )
        assert run("usage", "holder", "h-9", "--currency", "EGP") == (
            "wallet.daily: 0.00 EGP in 0 movement(s) of 1 since "
            "2026-10-29T12:00:00+00:00 (out)\n"
        )
    assert run("usage", "merchant", "m-1") == "No limit bounds merchant 'm-1'.\n"
    assert run("usage", "holder", "h-1", "--currency", "USD") == (
        "No limit bounds holder 'h-1'.\n"
    )


@pytest.mark.usefixtures("project")
@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("--currency", "EGP", "--max-count", "many"), "--max-count is a count"),
        (("--currency", "XYZ", "--per-transaction", "1"), "XYZ"),
        (("--currency", "EGP", "--per-transaction", "1.001"), "decimal places"),
        (("--currency", "EGP"), "A limit sets an amount per transaction"),
    ],
)
def test_the_command_refuses_what_is_not_a_limit(
    args: tuple[str, ...], message: str
) -> None:
    """A ``CommandError`` with the reason; nothing is stored."""
    with pytest.raises(CommandError, match=message):
        run("set", "x", "--kind", "holder", *args)

    assert run("list") == "No limits.\n"


@pytest.mark.usefixtures("project")
def test_purge_is_recorded_as_the_operators(actions: list[Action]) -> None:
    """``purge`` deletes old usage and says how much."""

    async def define() -> None:
        await set_limit(
            SetLimit(
                code="monthly",
                kind="holder",
                currency="EGP",
                window=Window.MONTH,
                max_amount=egp("100"),
            ),
            LimitsUnitOfWork(),
        )

    run_async(define())
    spend("1")
    with freeze_time(NOON + timedelta(days=40)):
        assert run("purge") == "Deleted the usage of 1 movement(s).\n"
    assert actions == [("limits.purge", "success", {"usage": 1})]


@pytest.mark.usefixtures("project")
def test_a_persons_limits_and_usage_are_exported_and_erased() -> None:
    """Export lists them; erasure removes both, and leaves others alone."""

    async def define(subject: str) -> None:
        await set_limit(
            SetLimit(
                code="daily",
                kind="holder",
                currency="EGP",
                subject=subject,
                window=Window.DAY,
                max_amount=egp("100"),
            ),
            LimitsUnitOfWork(),
        )

    run_async(define("subject-1"))
    run_async(define(""))
    spend("10", holder="subject-1")
    spend("20", holder="subject-1")
    spend("30", holder="subject-2")
    (export,) = on_export_subject.of("limits")
    (erase,) = on_erase_subject.of("limits")

    exported = run_async(export.call("subject-1"))
    nothing = run_async(export.call("nobody"))
    erased = run_async(erase.call("subject-1", "asked"))
    again = run_async(erase.call("subject-1", "asked"))

    assert exported == {
        "limits": [
            {
                "code": "daily",
                "kind": "holder",
                "direction": "out",
                "currency": "EGP",
                "window": "day",
            }
        ],
        "usage": [
            {
                "kind": "holder",
                "direction": "out",
                "currency": "EGP",
                "movements": 2,
                "first": NOON.isoformat(),
                "last": NOON.isoformat(),
            }
        ],
    }
    assert nothing == {}
    assert (dict(erased), dict(again)) == (
        {"limits": 1, "usage": 2},
        {"limits": 0, "usage": 0},
    )
    assert run_async(export.call("subject-1")) == {}
    assert subjects_named("subject-1") == []
    assert (
        cast(dict[str, Any], run_async(export.call("subject-2")))["usage"][0][
            "movements"
        ]
        == 1
    )


def test_limits_live_where_the_ledger_does() -> None:
    """``limits.E001``: elsewhere, the ledger's postings could not be counted."""
    registry.clear()
    with (
        override_settings(
            INSTALLED_DOMAINS=[ACCOUNTING, LIMITS, ALPHA],
            DATABASES={
                **SQLITE,
                "books": {"URL": "sqlite+aiosqlite://"},
                "other": {"URL": "sqlite+aiosqlite://"},
            },
            DATABASE_ROUTING={"accounting": "books", "limits": "other"},
        ),
        pytest.raises(CheckFailed, match=r"limits\.E001") as raised,
    ):
        setup()
    registry.clear()
    assert str(raised.value) == (
        "LIMITS cannot be enforced as configured:\n"
        "  limits.E001: The ledger's postings are counted in their own "
        "transaction, so the limits must live in the ledger's database "
        "('books'), not in 'other'.\n"
        "    Hint: Route 'limits' where 'accounting' is, in DATABASE_ROUTING."
    )


def test_defaults_that_are_not_limits_fail_setup() -> None:
    """``limits.E002``, naming the default."""
    registry.clear()
    with (
        override_settings(
            INSTALLED_DOMAINS=[LIMITS, ALPHA],
            DATABASES=SQLITE,
            LIMITS={"DEFAULTS": [{"code": "x", "kind": "holder", "currency": "EGP"}]},
        ),
        pytest.raises(CheckFailed, match=r"limits.E002: LIMITS\['DEFAULTS'\]\[0\]"),
    ):
        setup()
    registry.clear()


@pytest.mark.parametrize(
    ("domains", "posting", "purging"),
    [
        ([LIMITS, ALPHA], False, False),
        ([ACCOUNTING, LIMITS, ALPHA], True, False),
        ([MESSAGING, LIMITS, ALPHA], False, True),
    ],
)
def test_the_ledger_and_the_schedule_are_joined_when_installed(
    domains: list[str], posting: bool, purging: bool
) -> None:
    """The posting hook with the ledger; the purge every hour with messaging."""
    registry.clear()
    with override_settings(INSTALLED_DOMAINS=domains, DATABASES=SQLITE):
        setup()
        hooked = [hook.domain for hook in on_booking.hooks()]
        scheduled = {
            spec.key: every
            for spec, every in periodic_tasks()
            if spec.key == PURGE_USAGE
        }
    registry.clear()
    assert hooked == (["limits"] if posting else [])
    assert scheduled == ({PURGE_USAGE: timedelta(hours=1)} if purging else {})


@pytest.mark.parametrize(
    ("domains", "deployed", "warned"),
    [
        ([LIMITS, ALPHA], True, ["limits.W001"]),
        ([MESSAGING, LIMITS, ALPHA], True, []),
        ([LIMITS, ALPHA], False, []),
    ],
)
def test_usage_that_nothing_purges_is_warned_of_where_deployed(
    monkeypatch: pytest.MonkeyPatch,
    domains: list[str],
    deployed: bool,
    warned: list[str],
) -> None:
    """Without messaging no worker purges old usage: ``limits.W001``."""
    if deployed:
        monkeypatch.setattr(
            "domainkit.contrib.limits.apps.DEPLOYED_ENVIRONMENTS", frozenset({"test"})
        )
    registry.clear()
    with override_settings(INSTALLED_DOMAINS=domains, DATABASES=SQLITE):
        setup()
        found = domain_warnings("limits")
    registry.clear()
    assert [getattr(each, "id", None) for each in found] == warned
    if found:
        assert found[0] == (
            "Old usage is not purged on a schedule: domainkit.contrib.messaging is "
            "not installed, so no worker deletes it, and the usage table grows "
            "with every movement."
        )
        assert found[0].hint == (  # type: ignore[attr-defined]
            "Install domainkit.contrib.messaging, or run 'manage.py limits purge' "
            "from a scheduler and list limits.W001 in SILENCED_CHECKS."
        )


def test_money_of_more_places_than_limits_keep_is_refused() -> None:
    """A movement of a currency of six places cannot be counted exactly."""
    from domainkit.contrib.limits import InvalidLimit
    from domainkit.contrib.limits.usage import _tally
    from domainkit.money import register_currency

    register_currency("USDC6", minor_units=6)
    with pytest.raises(InvalidLimit, match="at most four decimal places"):
        _tally([Movement(Money.of("1", "USDC6"), {"holder": "h"})])


@pytest.mark.usefixtures("project")
def test_an_export_lists_limits_by_code_and_usage_by_kind_direction_currency() -> None:
    """Each kind, direction, and currency of the subject on a line of its own."""

    async def define() -> None:
        for code, fields in (
            ("b", {"kind": "holder", "currency": "EGP", "per_transaction": egp("5")}),
            (
                "a",
                {
                    "kind": "holder",
                    "currency": "EGP",
                    "window": Window.DAY,
                    "max_count": 9,
                },
            ),
        ):
            await set_limit(
                SetLimit(code=code, subject="p-1", **fields),  # type: ignore[arg-type]
                LimitsUnitOfWork(),
            )
        for kind, direction, currency in (
            ("holder", "out", "USD"),
            ("holder", "in", "EGP"),
            ("account", "out", "EGP"),
        ):
            await set_limit(
                SetLimit(
                    code=f"{kind}-{direction}-{currency.lower()}",
                    kind=kind,
                    currency=currency,
                    direction=direction,  # type: ignore[arg-type]
                    window=Window.DAY,
                    max_count=9,
                ),
                LimitsUnitOfWork(),
            )
        with freeze_time(NOON):
            async with LimitsUnitOfWork() as uow:
                for movement in (
                    Movement(Money.of("1", "USD"), {"holder": "p-1"}),
                    Movement(egp("1"), {"holder": "p-1", "account": "p-1"}),
                    Movement(egp("1"), {"holder": "p-1"}, "in"),  # type: ignore[arg-type]
                    Movement(egp("1"), {"holder": "p-1"}),
                ):
                    await consume(uow, movement)
                await uow.commit()

    run_async(define())
    (export,) = on_export_subject.of("limits")

    exported = cast(dict[str, Any], run_async(export.call("p-1")))

    assert [(each["code"], each["window"]) for each in exported["limits"]] == [
        ("a", "day"),
        ("b", None),
    ]
    assert [
        (each["kind"], each["direction"], each["currency"], each["movements"])
        for each in exported["usage"]
    ] == [
        ("account", "out", "EGP", 1),
        ("holder", "in", "EGP", 1),
        ("holder", "out", "EGP", 2),
        ("holder", "out", "USD", 1),
    ]


def test_limits_may_live_in_another_database_with_the_ledger() -> None:
    """Both routed to one database: setup passes, and the hook is registered."""
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[ACCOUNTING, LIMITS, ALPHA],
        DATABASES={**SQLITE, "books": {"URL": "sqlite+aiosqlite://"}},
        DATABASE_ROUTING={"accounting": "books", "limits": "books"},
    ):
        setup()
        hooked = [hook.domain for hook in on_booking.hooks()]
    registry.clear()
    assert hooked == ["limits"]


@pytest.mark.usefixtures("project")
def test_set_keeps_the_bounds_it_is_not_given() -> None:
    """Each bound left out stays as it was; a new limit has no note."""
    key = ("set", "k", "--kind", "holder", "--currency", "EGP")
    run(*key, "--per-transaction", "5")
    run(*key, "--window", "day", "--max-count", "2")
    run(*key, "--note", "n")

    async def stored() -> tuple[object, ...]:
        async with LimitsUnitOfWork(readonly=True) as uow:
            (found,) = await uow.limits.filter()
        return found.per_transaction, found.window, found.max_count, found.note

    assert run_async(stored()) == (egp("5"), Window.DAY, 2, "n")
    run(
        "set",
        "fresh",
        "--kind",
        "holder",
        "--currency",
        "USD",
        "--max-count",
        "1",
        "--window",
        "day",
    )

    async def fresh() -> str:
        async with LimitsUnitOfWork(readonly=True) as uow:
            found = await uow.limits.filter()
        return next(each.note for each in found if each.code == "fresh")

    assert run_async(fresh()) == ""
