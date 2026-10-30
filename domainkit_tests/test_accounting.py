"""The ledger's proofs: concurrent postings, messages delivered twice, exports.

Each runs on SQLite, and on PostgreSQL when ``DOMAINKIT_TEST_POSTGRES_URL``
is set (CI sets it): there the transfers really run at once, each taking
row locks on two of six accounts. Every test starts from a project
migrated as ``makemigrations`` and ``migrate`` leave it, with the
triggers that keep entries append-only.
"""

import asyncio
import csv
import io
import json
import logging
import random
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError

from domainkit.conf import override_settings
from domainkit.contrib.accounting import (
    Account,
    AccountingUnitOfWork,
    AccountType,
    Entry,
    Line,
    OpenAccount,
    OpenPeriod,
    PeriodId,
    PostEntry,
    defer_entry,
    general_ledger,
    open_account,
    open_period,
    post_entry,
    trial_balance,
    verify_ledger,
)
from domainkit.contrib.accounting.tables import entries, postings
from domainkit.contrib.accounting.tasks import POST_ENTRY
from domainkit.contrib.messaging import MessagingSettings, Worker
from domainkit.contrib.messaging.tables import DONE, messages
from domainkit.contrib.messaging.worker import Claimed
from domainkit.db import connections
from domainkit.money import Money
from domainkit_tests.conftest import Migrated, Shape

pytestmark = pytest.mark.anyio

MESSAGING: Final = "domainkit.contrib.messaging"
ACCOUNTING: Final = "domainkit.contrib.accounting"
ALPHA: Final = "domainkit_tests.sample_domains.alpha"
FAST: Final = {
    "RETRY_BASE_SECONDS": 0.001,
    "RETRY_MAX_SECONDS": 0.001,
    "TASK_TIMEOUT": 30,
    "VISIBILITY_TIMEOUT": 60,
}
LEDGER: Final = Shape(
    "accounting",
    "accounting",
    {"INSTALLED_DOMAINS": [MESSAGING, ACCOUNTING, ALPHA], "MESSAGING": FAST},
)
"""A project with the ledger and messaging, migrated once per run."""
TODAY: Final = date(2026, 10, 30)
TRANSFERS: Final = 50


@pytest.fixture
async def books(migrated: Migrated) -> AsyncIterator[str]:
    """The ledger in a migrated database of the test's own; its URL."""
    with migrated(LEDGER) as url:
        yield url
        await connections.close_all()


def usd(amount: str) -> Money:
    """``amount`` dollars."""
    return Money.of(amount, "USD")


async def account(code: str, currency: str = "USD", **options: object) -> Account:
    """Open an asset account ``code``."""
    return await open_account(
        OpenAccount(
            code=code,
            name=f"Account {code}",
            type=options.pop("type", AccountType.ASSET),  # type: ignore[arg-type]
            currency=currency,
            **options,  # type: ignore[arg-type]
        ),
        AccountingUnitOfWork(),
    )


async def period(
    name: str = "2026-10", starts_on: date = date(2026, 10, 1), ends_on: date = TODAY
) -> PeriodId:
    """Open a period; its id."""
    opened = await open_period(
        OpenPeriod(name=name, starts_on=starts_on, ends_on=ends_on),
        AccountingUnitOfWork(),
    )
    return opened.id


def transfer(
    source: Account, target: Account, amount: Money, reference: str
) -> PostEntry:
    """Move ``amount`` from ``source`` to ``target`` (assets: credit, then debit)."""
    return PostEntry(
        source="tests",
        reference=reference,
        booked_on=TODAY,
        lines=(Line.credit(source.id, amount), Line.debit(target.id, amount)),
    )


async def balances() -> dict[uuid.UUID, Decimal]:
    """Each account's stored balance."""
    async with AccountingUnitOfWork(readonly=True) as uow:
        found = await uow.accounts.filter()
    return {each.id: each.balance for each in found}


async def count(table: object) -> int:
    """Rows in ``table``."""
    async with connections.engine().connect() as connection:
        return int(
            await connection.scalar(select(func.count()).select_from(table))  # type: ignore[arg-type]
            or 0
        )


async def test_concurrent_transfers_keep_every_entry_balanced_and_lose_no_update(
    books: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Fifty transfers at once between overlapping pairs: no deadlock, nothing lost.

    Each transfer locks its two accounts by ascending id, so two that share
    an account wait for each other in one order and never deadlock; each
    balance moves in its posting's transaction, under its lock, so none is
    overwritten. No run is retried (no race was lost), every balance is the
    sum of its postings, and every entry balances.
    """
    pooled: dict[str, object] = {"POOL_SIZE": 25, "MAX_OVERFLOW": 30}
    if books.startswith("sqlite"):  # One writer at a time: wait as long as it takes.
        pooled = {"CONNECT_ARGS": {"timeout": 60}}
    with override_settings(DATABASES={"default": {"URL": books, **pooled}}):
        await connections.close_all()
        await period()
        chart = [await account(f"10{n}") for n in range(6)]
        funding = await account("3000", type=AccountType.EQUITY)
        for each in chart:
            await post_entry(
                PostEntry(
                    source="tests",
                    reference=f"fund-{each.code}",
                    booked_on=TODAY,
                    lines=(
                        Line.debit(each.id, usd("1000")),
                        Line.credit(funding.id, usd("1000")),
                    ),
                ),
                AccountingUnitOfWork(),
            )
        pick = random.Random(14)  # noqa: S311 - a fixed, repeatable choice.
        pairs = [pick.sample(chart, 2) for _ in range(TRANSFERS)]
        amounts = [usd(f"{pick.randint(1, 5000) / 100:.2f}") for _ in range(TRANSFERS)]
        start = asyncio.Event()

        async def one(number: int) -> Entry:
            await start.wait()
            source, target = pairs[number]
            return await post_entry(
                transfer(source, target, amounts[number], f"t-{number}"),
                AccountingUnitOfWork(),
            )

        with caplog.at_level(logging.INFO, "domainkit.ddd.uow"):
            running = [asyncio.create_task(one(number)) for number in range(TRANSFERS)]
            await asyncio.sleep(0)
            start.set()
            posted = await asyncio.wait_for(asyncio.gather(*running), 60)

        assert len({entry.id for entry in posted}) == TRANSFERS
        assert not [r for r in caplog.records if "lost a race" in r.getMessage()]
        expected = {each.id: Decimal("1000.0000") for each in chart}
        for (source, target), amount in zip(pairs, amounts, strict=True):
            expected[source.id] -= amount.amount
            expected[target.id] += amount.amount
        stored = await balances()
        assert {each.id: stored[each.id] for each in chart} == expected
        assert stored[funding.id] == Decimal("6000.0000")
        assert await verify_ledger() == []
        assert await count(entries) == TRANSFERS + len(chart)
        assert await count(postings) == 2 * (TRANSFERS + len(chart))
        await connections.close_all()


async def test_a_retried_posting_books_once(books: str) -> None:
    """The same source and reference posted again returns the first entry."""
    await period()
    cash, sales = await account("1000"), await account("4000", type=AccountType.INCOME)
    command = transfer(sales, cash, usd("12.50"), "invoice-7")

    first = await post_entry(command, AccountingUnitOfWork())
    again = await post_entry(command, AccountingUnitOfWork())

    assert again.id == first.id
    assert await count(entries) == 1
    assert (await balances())[cash.id] == Decimal("12.5000")


async def test_the_same_message_delivered_twice_books_once(books: str) -> None:
    """A message run again after its entry committed is skipped by the inbox.

    The first worker commits the entry, then loses the message's outcome,
    as a crash would; once its lease runs out, another worker takes the
    message again. The entry is booked once, and the message is done.
    """
    await period()
    cash, sales = await account("1000"), await account("4000", type=AccountType.INCOME)
    async with AccountingUnitOfWork() as uow:
        defer_entry(uow, transfer(sales, cash, usd("40.00"), "order-1"))
        await uow.commit()
    first = Worker(MessagingSettings(**FAST))
    done = first._done
    lost: list[uuid.UUID] = []

    async def losing(message: Claimed, follow_up: dict[str, Any] | None) -> bool:
        if message.target == POST_ENTRY and not lost:
            lost.append(message.id)
            raise OperationalError("UPDATE", {}, Exception("the outbox went away"))
        return await done(message, follow_up)

    first._done = losing  # type: ignore[method-assign]
    await first.run_once()
    assert lost
    assert await count(entries) == 1
    async with connections.engine().begin() as connection:
        await connection.execute(
            update(messages).values(locked_until=datetime.now(UTC) - timedelta(1))
        )

    await Worker(MessagingSettings(**FAST)).run_once()

    assert await count(entries) == 1
    assert (await balances())[cash.id] == Decimal("40.0000")
    async with connections.engine().connect() as connection:
        row = (
            await connection.execute(
                select(messages.c.status, messages.c.attempts).where(
                    messages.c.id == lost[0]
                )
            )
        ).one()
    assert (row.status, row.attempts) == (DONE, 2)


async def test_two_messages_about_one_entry_book_it_once(books: str) -> None:
    """Two messages with the same source and reference: the second finds the first's."""
    await period()
    cash, sales = await account("1000"), await account("4000", type=AccountType.INCOME)
    for _ in range(2):
        async with AccountingUnitOfWork() as uow:
            defer_entry(uow, transfer(sales, cash, usd("40.00"), "order-2"))
            await uow.commit()

    await Worker(MessagingSettings(**FAST)).run_once()

    assert await count(entries) == 1
    assert (await balances())[cash.id] == Decimal("40.0000")


async def test_the_export_of_a_period_sums_to_zero_per_currency(books: str) -> None:
    """The general ledger and the trial balance, CSV and JSON, net zero by currency."""
    october = await period()
    cash, sales = await account("1000"), await account("4000", type=AccountType.INCOME)
    euros = await account("1100", "EUR")
    equity = await account("3000", "EUR", type=AccountType.EQUITY)
    for number in range(3):
        await post_entry(
            transfer(sales, cash, usd(f"{number + 1}0.25"), f"s-{number}"),
            AccountingUnitOfWork(),
        )
    await post_entry(
        PostEntry(
            source="tests",
            reference="mixed",
            booked_on=TODAY,
            description="=SUM(A1:A9)",
            lines=(
                Line.debit(cash.id, usd("5.00")),
                Line.credit(sales.id, usd("5.00")),
                Line.debit(euros.id, Money.of("7.10", "EUR")),
                Line.credit(equity.id, Money.of("7.10", "EUR")),
            ),
        ),
        AccountingUnitOfWork(),
    )

    as_json = "".join([part async for part in general_ledger(october, "json")])
    as_csv = "".join([part async for part in general_ledger(october, "csv")])
    report = await trial_balance(october)

    document = json.loads(as_json)
    nets: defaultdict[str, Decimal] = defaultdict(Decimal)
    for posting in document["postings"]:
        nets[posting["currency"]] += Decimal(posting["net"])
    assert nets == {"USD": Decimal(0), "EUR": Decimal(0)}
    assert [total["net"] for total in document["totals"]] == ["0.00", "0.00"]
    assert len(document["postings"]) == 10
    rows = list(csv.DictReader(io.StringIO(as_csv)))
    by_currency: defaultdict[str, Decimal] = defaultdict(Decimal)
    for row in rows:
        if row["description"] != "total":
            by_currency[row["currency"]] += Decimal(row["net"])
    assert by_currency == {"USD": Decimal(0), "EUR": Decimal(0)}
    assert {row["description"] for row in rows} >= {"'=SUM(A1:A9)", "total"}
    assert report.balanced
    assert [(t.currency, t.net) for t in report.totals] == [
        ("EUR", Decimal(0)),
        ("USD", Decimal(0)),
    ]
    assert json.loads(report.to_json())["totals"][1]["debits"] == "65.75"
    assert as_json == "".join([part async for part in general_ledger(october, "json")])
