"""The ledger in operation: its protection, its hooks, its command, and its checks."""

import io
import json
import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, cast

import pytest
from sqlalchemy import insert, text, update
from sqlalchemy.exc import OperationalError

from domainkit import setup
from domainkit.apps import registry
from domainkit.apps.hooks import on_erase_subject, on_export_subject
from domainkit.checks import CheckFailed
from domainkit.clock import freeze_time
from domainkit.conf import override_settings
from domainkit.contrib.accounting import (
    AccountingUnitOfWork,
    AccountType,
    Line,
    PeriodNotFound,
    PostEntry,
    expire_holds,
    general_ledger,
    post_entry,
    protection,
    trial_balance,
    verify_ledger,
)
from domainkit.contrib.accounting.reconciliation import (
    CsvStatement,
    breaks_of,
    import_statement,
    reconcile,
)
from domainkit.contrib.accounting.tables import accounts, postings
from domainkit.contrib.accounting.tasks import POST_ENTRY
from domainkit.contrib.messaging.registry import get_task
from domainkit.db import connections
from domainkit.exceptions import ImproperlyConfigured
from domainkit.management import CommandError, call_command, operators
from domainkit.management.base import run_async
from domainkit_tests.conftest import POSTGRES_VARIABLE, MakeDatabase, Migrated
from domainkit_tests.test_accounting import (
    ACCOUNTING,
    ALPHA,
    LEDGER,
    MESSAGING,
    TODAY,
    account,
    books,  # noqa: F401 - the fixture.
    period,
    refused,
    transfer,
    usd,
)

pytestmark = pytest.mark.anyio
NOW: Final = datetime(2026, 10, 30, 12, tzinfo=UTC)
ON_POSTGRES: Final = bool(__import__("os").environ.get(POSTGRES_VARIABLE))
postgres_only = pytest.mark.skipif(not ON_POSTGRES, reason=f"set {POSTGRES_VARIABLE}")
TRIGGERS: Final = [
    "domainkit_accounting_entries_append_only",
    "domainkit_accounting_entries_append_only_truncate",
    "domainkit_accounting_postings_append_only",
    "domainkit_accounting_postings_append_only_truncate",
]


@pytest.fixture
def deployed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check as staging and production do, on the test database."""
    monkeypatch.setattr(protection, "DEPLOYED_ENVIRONMENTS", frozenset({"test"}))


@pytest.mark.usefixtures("books")
async def test_nothing_is_checked_outside_staging_and_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Development and tests start without asking the database."""

    async def unexpected() -> list[str]:
        raise AssertionError("asked")

    monkeypatch.setattr(protection, "protection_problems", unexpected)
    await protection.check_protection()


@pytest.mark.usefixtures("books")
async def test_a_protected_ledger_starts_where_deployed(
    deployed: None, caplog: pytest.LogCaptureFixture
) -> None:
    """After ``migrate`` the triggers are there (none needed on SQLite)."""
    with caplog.at_level(logging.WARNING, logger=protection.__name__):
        await protection.check_protection()
    assert await protection.protection_problems() == []
    assert caplog.records == []


@pytest.mark.usefixtures("books")
@postgres_only
async def test_missing_triggers_refuse_the_start_and_migrate_restores_them(
    deployed: None, caplog: pytest.LogCaptureFixture
) -> None:
    """A disabled or dropped trigger is ``accounting.E001``; ``protect`` makes it."""
    async with connections.engine().begin() as connection:
        await connection.execute(
            text(
                "ALTER TABLE domainkit_accounting_entries DISABLE TRIGGER "
                "domainkit_accounting_entries_append_only"
            )
        )
        await connection.execute(
            text(
                "DROP TRIGGER domainkit_accounting_postings_append_only_truncate "
                "ON domainkit_accounting_postings"
            )
        )

    (problem,) = await protection.protection_problems()
    with pytest.raises(CheckFailed, match=r"accounting\.E001") as refused:
        await protection.check_protection()
    with (
        override_settings(ACCOUNTING={"REQUIRE_PROTECTION": False}),
        caplog.at_level(logging.WARNING, logger=protection.__name__),
    ):
        await protection.check_protection()

    assert problem.id == "accounting.E001"
    assert problem == (
        "Entries and postings in 'default' can be changed: triggers "
        "domainkit_accounting_entries_append_only, "
        "domainkit_accounting_postings_append_only_truncate are missing, disabled, "
        "or changed. Run 'manage.py migrate', which makes them."
    )
    assert "REQUIRE_PROTECTION refuses to start in test" in str(refused.value)
    assert [record.getMessage() for record in caplog.records] == [
        f"accounting.W001: {problem}"
    ]
    async with connections.engine().begin() as connection:
        await protection.protect(connection, "default")
        assert await protection.missing(connection) == []


@pytest.mark.usefixtures("books")
async def test_a_database_that_does_not_answer_is_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``accounting.E002``, with the error's class and nothing of its detail."""

    def unreachable(alias: str) -> object:
        raise OperationalError("SELECT 1", {}, OSError("password=hunter2"))

    monkeypatch.setattr(connections, "engine", unreachable)

    (problem,) = await protection.protection_problems()

    assert problem.id == "accounting.E002"
    assert str(problem) == (
        "Could not check the ledger's triggers in 'default' (OperationalError)."
    )


@pytest.mark.usefixtures("books")
@postgres_only
async def test_protect_leaves_other_databases_and_missing_tables_alone() -> None:
    """Only the ledger's database, once its tables exist, gets triggers."""
    async with connections.engine().begin() as connection:
        await protection.protect(connection, "elsewhere")
        await connection.execute(
            text("ALTER TABLE domainkit_accounting_postings RENAME TO kept_aside")
        )
        await protection.protect(connection, "default")
        await connection.execute(
            text("ALTER TABLE kept_aside RENAME TO domainkit_accounting_postings")
        )
        await connection.execute(
            text(
                "DROP TRIGGER domainkit_accounting_entries_append_only "
                "ON domainkit_accounting_entries"
            )
        )
        await protection.protect(connection, "elsewhere")
        assert await protection.missing(connection) == [
            "domainkit_accounting_entries_append_only"
        ]
        await protection.protect(connection, "default")
        assert await protection.missing(connection) == []


@pytest.mark.usefixtures("books")
async def test_sqlite_gets_no_triggers() -> None:
    """On SQLite ``protect`` does nothing, and nothing is missing."""
    async with connections.engine().begin() as connection:
        await protection.protect(connection, "default")
        if connection.dialect.name == "sqlite":
            found = await connection.scalar(
                text("SELECT count(*) FROM sqlite_master WHERE type = 'trigger'")
            )
            assert found == 0


def test_the_statements_make_each_trigger() -> None:
    """Rows before update or delete; the table before truncation."""
    from domainkit.contrib.accounting.tables import entries

    names = protection.triggers(entries)
    assert names == {
        "domainkit_accounting_entries_append_only": 27,
        "domainkit_accounting_entries_append_only_truncate": 34,
    }
    row, statement = (protection.statements(entries, name) for name in names)
    assert row[1] == (
        "CREATE TRIGGER domainkit_accounting_entries_append_only BEFORE UPDATE OR "
        "DELETE ON domainkit_accounting_entries FOR EACH ROW EXECUTE FUNCTION "
        "domainkit_accounting_entries_append_only()"
    )
    assert statement == (
        (
            "DROP TRIGGER IF EXISTS domainkit_accounting_entries_append_only_truncate "
            "ON domainkit_accounting_entries"
        ),
        (
            "CREATE TRIGGER domainkit_accounting_entries_append_only_truncate BEFORE "
            "TRUNCATE ON domainkit_accounting_entries FOR EACH STATEMENT EXECUTE "
            "FUNCTION domainkit_accounting_entries_append_only()"
        ),
    )
    assert "RAISE EXCEPTION 'domainkit_accounting_entries is append-only'" in (
        protection.function_statement(entries)
    )


def test_the_ledger_lives_in_one_database() -> None:
    """Routed to a shard group, the app refuses to start."""
    registry.clear()
    with (
        override_settings(
            INSTALLED_DOMAINS=[ACCOUNTING, ALPHA],
            DATABASES={
                "default": {"URL": "sqlite+aiosqlite://"},
                "a": {"URL": "sqlite+aiosqlite://"},
                "b": {"URL": "sqlite+aiosqlite://"},
            },
            SHARDS={"ledgers": {"DATABASES": ["a", "b"]}},
            DATABASE_ROUTING={"accounting": "ledgers"},
        ),
        pytest.raises(ImproperlyConfigured, match=r"DATABASE_ROUTING\['accounting'\]"),
    ):
        setup()
    registry.clear()


def test_without_messaging_no_task_is_registered() -> None:
    """Entries are posted from messages only where a worker runs them."""
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[ACCOUNTING, ALPHA],
        DATABASES={"default": {"URL": "sqlite+aiosqlite://"}},
    ):
        setup()
        assert get_task(POST_ENTRY) is None
    registry.clear()


@pytest.mark.usefixtures("books")
async def test_a_holders_accounts_are_exported_and_unlinked() -> None:
    """Export lists their accounts and postings, in order; erasure keeps the books."""
    await period()
    bank = await account("1000")
    with freeze_time(NOW + timedelta(hours=1)):
        wallet = await account("2000", type=AccountType.LIABILITY, holder="subject-1")
    with freeze_time(NOW):
        savings = await account("2001", type=AccountType.LIABILITY, holder="subject-1")
    for minute, (reference, day, lines) in enumerate(
        [
            (
                "late",
                20,
                (Line.debit(bank.id, usd("5")), Line.credit(wallet.id, usd("5"), "m")),
            ),
            (
                "early",
                10,
                (Line.credit(wallet.id, usd("2")), Line.debit(bank.id, usd("2"))),
            ),
            (
                "last",
                20,
                (Line.credit(wallet.id, usd("1")), Line.debit(bank.id, usd("1"))),
            ),
        ]
    ):
        with freeze_time(NOW + timedelta(minutes=minute)):
            entry = await post_entry(
                PostEntry(
                    source="tests",
                    reference=reference,
                    booked_on=date(2026, 10, day),
                    description=f"d-{reference}",
                    lines=lines,
                ),
                AccountingUnitOfWork(),
            )
        if reference == "early":
            early = entry.id
    (export,) = on_export_subject.of("accounting")
    (erase,) = on_erase_subject.of("accounting")

    exported = await export.call("subject-1")
    nothing = await export.call("subject-2")
    erased = await erase.call("subject-1", "asked")
    again = await erase.call("subject-1", "asked")

    first, second = cast(list[dict[str, Any]], exported["accounts"])
    assert first == {
        "id": str(savings.id),
        "code": "2001",
        "name": "Account 2001",
        "currency": "USD",
        "status": "open",
        "balance": "0.00",
        "held": "0.00",
        "opened_at": NOW.isoformat(),
        "postings": [],
    }
    assert (second["id"], second["balance"]) == (str(wallet.id), "8.00")
    assert second["postings"][0] == {
        "entry_id": str(early),
        "booked_on": "2026-10-10",
        "side": "credit",
        "amount": "2.00",
        "reference": "early",
        "description": "d-early",
        "memo": "",
    }
    assert [(p["reference"], p["memo"]) for p in second["postings"]] == [
        ("early", ""),
        ("late", "m"),
        ("last", ""),
    ]
    assert (nothing, erased, again) == ({}, {"accounts": 2}, {})
    async with AccountingUnitOfWork(readonly=True) as uow:
        stored = await uow.accounts.get(wallet.id)
    assert (stored.holder, stored.version, stored.balance) == (
        None,
        5,
        Decimal("8.0000"),
    )
    assert await export.call("subject-1") == {}


@pytest.mark.usefixtures("books")
async def test_reports_of_a_missing_period_are_not_found() -> None:
    """Both reports refuse a period no one opened."""
    nobody = uuid.uuid4()
    with refused(PeriodNotFound):
        await trial_balance(nobody)  # type: ignore[arg-type]
    with refused(PeriodNotFound):
        await anext(general_ledger(nobody, "json"))  # type: ignore[arg-type]


@pytest.mark.usefixtures("books")
async def test_the_trial_balance_carries_balances_into_later_periods() -> None:
    """Movements before a period are its opening balances; after it, nothing."""
    september = await period("2026-09", date(2026, 9, 1), date(2026, 9, 30))
    october = await period()
    cash, sales = await account("1000"), await account("4000", type=AccountType.INCOME)
    for reference, day, amount in (
        ("a", date(2026, 9, 10), "3"),
        ("b", TODAY, "4"),
    ):
        await post_entry(
            PostEntry(
                source="tests",
                reference=reference,
                booked_on=day,
                lines=(
                    Line.debit(cash.id, usd(amount)),
                    Line.credit(sales.id, usd(amount)),
                ),
            ),
            AccountingUnitOfWork(),
        )

    before = await trial_balance(september)
    report = await trial_balance(october)

    cash_row = report.rows[0]
    assert (cash_row.code, cash_row.opening, cash_row.debits, cash_row.credits) == (
        "1000",
        Decimal("3.0000"),
        Decimal("4.0000"),
        Decimal(0),
    )
    assert cash_row.closing == Decimal("7.0000")
    assert [row.closing for row in before.rows] == [
        Decimal("3.0000"),
        Decimal("-3.0000"),
    ]
    document = json.loads(report.to_json())
    assert document["format"] == "domainkit.accounting.trial_balance"
    assert document["version"] == 1
    assert document["period"]["name"] == "2026-10"
    assert document["accounts"][1] == {
        "account_id": str(sales.id),
        "code": "4000",
        "name": "Account 4000",
        "type": "income",
        "currency": "USD",
        "opening": "-3.00",
        "debits": "0.00",
        "credits": "4.00",
        "closing": "-7.00",
    }
    assert report.to_csv().splitlines() == [
        "code,name,type,currency,opening,debits,credits,closing",
        "1000,Account 1000,asset,USD,3.00,4.00,0.00,7.00",
        "4000,Account 4000,income,USD,-3.00,0.00,4.00,-7.00",
        ",total,,USD,0.00,4.00,4.00,0.00",
    ]


@pytest.mark.usefixtures("books")
async def test_verify_finds_what_does_not_add_up() -> None:
    """A balance moved behind the ledger's back, and a posting without its pair."""
    october = await period()
    cash, sales = await account("1000"), await account("4000")
    entry = await post_entry(
        transfer(sales, cash, usd("2"), "r"), AccountingUnitOfWork()
    )
    async with connections.engine().begin() as connection:
        await connection.execute(
            update(accounts).where(accounts.c.id == cash.id).values(balance=Decimal(9))
        )
        await connection.execute(
            insert(postings).values(
                id=uuid.uuid4(),
                entry_id=entry.id,
                line=3,
                account_id=sales.id,
                side="debit",
                amount=Decimal(1),
                currency="USD",
                booked_on=TODAY,
                memo="",
            )
        )

    problems = await verify_ledger()
    ledger = json.loads("".join([p async for p in general_ledger(october, "json")]))

    assert ledger["totals"] == [
        {
            "currency": "USD",
            "opening": "0.00",
            "debits": "3.00",
            "credits": "2.00",
            "net": "1.00",
        }
    ]

    assert [(p.kind, p.id) for p in problems] == [
        ("unbalanced_entry", str(entry.id)),
        ("balance_differs", str(cash.id)),
        ("balance_differs", str(sales.id)),
    ]
    assert problems[0].detail == "USD: debits 3.0000, credits 2.0000"
    assert problems[1].detail == "1000: balance 9.0000, postings 2.0000"


@pytest.fixture
def project(migrated: Migrated) -> Iterator[str]:
    """The ledger in a migrated database, for commands (which run their own loop)."""
    with migrated(LEDGER) as url:
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
    """``accounting`` with ``args``; what it printed."""
    output = io.StringIO()
    call_command("accounting", *args, "--operator", "tester", stdout=output)
    return output.getvalue()


def books_with_a_sale() -> None:
    """A period, two accounts, and a sale of 10.50."""

    async def make() -> None:
        await period()
        cash, sales = await account("1000"), await account("4000")
        await post_entry(
            transfer(sales, cash, usd("10.50"), "s-1"), AccountingUnitOfWork()
        )

    run_async(make())


def test_the_command_writes_reports(project: str, tmp_path: Path) -> None:
    """Trial balances and ledgers, CSV or JSON, to stdout or a file."""
    books_with_a_sale()
    target = tmp_path / "ledger.json"

    balance = run("trialbalance", "2026-10")
    run("ledger", "2026-10", "--format", "json", "--output", str(target))
    ledger_csv = run("ledger", "2026-10")
    as_json = run("trialbalance", "2026-10", "--format", "json")
    by_id = json.loads(as_json)["period"]["id"]

    assert balance.splitlines()[-1] == ",total,,USD,0.00,10.50,10.50,0.00"
    assert json.loads(target.read_text(encoding="utf-8"))["totals"][0]["net"] == "0.00"
    assert ledger_csv.splitlines()[0].startswith("booked_on,entry_id,")
    assert run("trialbalance", by_id) == balance
    with pytest.raises(CommandError, match=r"^No period is 'nope'\.$"):
        run("ledger", "nope")
    with pytest.raises(SystemExit):
        run("ledgers", "2026-10")
    with pytest.raises(CommandError, match=r"^ledger needs a period or an account\.$"):
        run("ledger")


def test_the_command_checks_the_books(project: str) -> None:
    """``verify`` passes balanced books and fails others, listing each problem."""
    books_with_a_sale()
    assert run("verify") == "The books balance.\n"

    async def tamper() -> None:
        async with connections.engine().begin() as connection:
            await connection.execute(update(accounts).values(balance=Decimal(1)))

    run_async(tamper())
    output = io.StringIO()
    with pytest.raises(CommandError, match=r"^The books have 2 problem\(s\)\.$"):
        call_command("accounting", "verify", stdout=output)
    assert output.getvalue().count("balance_differs") == 2


def test_the_command_expires_holds(project: str, actions: list[Action]) -> None:
    """``expireholds`` releases what is due, says how many, and is recorded."""
    assert run("expireholds") == "Released 0 expired hold(s).\n"
    assert actions == [("accounting.expireholds", "success", {"holds": 0})]


def test_the_command_reconciles_a_statement(
    project: str, tmp_path: Path, actions: list[Action]
) -> None:
    """Import by account code, reconcile, and list what is unmatched."""
    books_with_a_sale()
    statement = tmp_path / "october.csv"
    statement.write_text(
        "﻿id,date,amount,currency,reference\n"
        "b-1,2026-10-30,10.50,USD,s-1\n"
        "b-2,2026-10-30,1.25,USD,x\n",
        encoding="utf-8",
    )

    imported = run("import", "1000", str(statement))
    known = run("import", "1000", str(statement))
    reconciled = run("reconcile", "1000")
    unmatched = run("unmatched", "1000")

    assert imported == "Imported 2 line(s); 0 known.\n"
    assert known == "Imported 0 line(s); 2 known.\n"
    assert reconciled == "Matched 1 by reference.\n1 unmatched.\n"
    assert unmatched == "2026-10-30  unmatched_line     1.25 USD  x\n"
    account = json.loads(run("trialbalance", "2026-10", "--format", "json"))[
        "accounts"
    ][0]["account_id"]
    file = {"account": account, "file": "october.csv"}
    assert actions == [
        ("accounting.import", "success", {**file, "imported": 2, "skipped": 0}),
        ("accounting.import", "success", {**file, "imported": 0, "skipped": 2}),
        (
            "accounting.reconcile",
            "success",
            {"account": account, "matched": 1, "breaks": 1},
        ),
    ]
    with pytest.raises(CommandError, match=r"^import needs the statement's file\.$"):
        run("import", "1000")
    with pytest.raises(CommandError, match=r"^No account is '9999'\.$"):
        run("reconcile", "9999")
    with pytest.raises(CommandError, match="No account is"):
        run("unmatched", str(uuid.uuid4()))


def test_the_ledger_works_in_a_database_of_its_own(
    make_database: MakeDatabase, tmp_path: Path
) -> None:
    """Routed to another alias, every part of it reads and writes there."""
    registry.clear()
    urls = {"default": make_database("main"), "books": make_database("books")}
    with override_settings(
        INSTALLED_DOMAINS=[MESSAGING, ACCOUNTING, ALPHA],
        DATABASES={alias: {"URL": url} for alias, url in urls.items()},
        DATABASE_ROUTING={"accounting": "books"},
        MIGRATIONS_DIR=tmp_path / "migrations",
    ):
        setup()
        call_command("makemigrations", "-m", "books", stdout=io.StringIO())
        call_command("migrate", stdout=io.StringIO())

        async def scenario() -> dict[str, object]:
            october = await period()
            wallet = await account("2000", type=AccountType.LIABILITY, holder="s-1")
            bank = await account("1000")
            await post_entry(
                transfer(wallet, bank, usd("5"), "s-1"), AccountingUnitOfWork()
            )
            await import_statement(
                bank.id,
                CsvStatement(
                    "id,date,amount,currency,reference\nb,2026-10-30,5,USD,s-1\n"
                ),
            )
            async with connections.engine("books").connect() as connection:
                postgres = connection.dialect.name == "postgresql"
                missing = await protection.missing(connection) if postgres else []
            (export,) = on_export_subject.of("accounting")
            return {
                "balance": (await trial_balance(october)).balanced,
                "ledger": "".join([p async for p in general_ledger(october, "csv")]),
                "verify": await verify_ledger(),
                "matched": (await reconcile(bank.id)).matched,
                "breaks": await breaks_of(bank.id),
                "expired": await expire_holds(),
                "export": await export.call("s-1"),
                "problems": await protection.protection_problems(),
                "missing": missing,
                "elsewhere": await _tables("default"),
            }

        found = run_async(scenario())
    registry.clear()

    assert found["balance"] is True
    assert "s-1" in str(found["ledger"])
    assert (found["verify"], found["breaks"], found["expired"]) == ([], [], 0)
    assert found["matched"] == {"reference": 1}
    assert found["export"] != {}
    assert (found["problems"], found["missing"]) == ([], [])
    assert not [name for name in found["elsewhere"] if "accounting" in name]  # type: ignore[attr-defined]


async def _tables(alias: str) -> list[str]:
    """The tables of ``alias``'s database."""
    async with connections.engine(alias).connect() as connection:
        return await connection.run_sync(
            lambda sync: sorted(sync.dialect.get_table_names(sync))
        )
