"""The reports byte for byte: stable JSON and CSV, in their order, at their places."""

import json
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Final

import pytest

from domainkit.clock import freeze_time
from domainkit.contrib.accounting import (
    Account,
    AccountId,
    AccountingUnitOfWork,
    AccountType,
    Entry,
    Line,
    PeriodId,
    PeriodStatus,
    PostEntry,
    general_ledger,
    post_entry,
    trial_balance,
)
from domainkit.contrib.accounting.reports import (
    CurrencyTotal,
    PeriodSummary,
    TrialBalance,
    TrialBalanceRow,
    _totals,
)
from domainkit.money import Money
from domainkit_tests.test_accounting import (
    account,
    books,  # noqa: F401 - the fixture.
    period,
    usd,
)

pytestmark = pytest.mark.anyio
NOW: Final = datetime(2026, 10, 30, 12, tzinfo=UTC)


def row(currency: str, opening: str, debits: str, credits: str) -> TrialBalanceRow:
    """A trial balance row of made-up amounts."""
    return TrialBalanceRow(
        AccountId(uuid.uuid4()),
        "1",
        "n",
        AccountType.ASSET,
        currency,
        Decimal(opening),
        Decimal(debits),
        Decimal(credits),
    )


def test_totals_add_each_currencys_rows() -> None:
    """Whatever the rows hold, their sums by currency, by code."""
    totals = _totals(
        [
            row("USD", "1", "2", "3"),
            row("EUR", "5", "0", "7"),
            row("USD", "10", "20", "30"),
        ]
    )
    assert [(t.currency, t.opening, t.debits, t.credits, t.net) for t in totals] == [
        ("EUR", Decimal(5), Decimal(0), Decimal(7), Decimal(-2)),
        ("USD", Decimal(11), Decimal(22), Decimal(33), Decimal(0)),
    ]
    assert not TrialBalance(
        PeriodSummary(
            PeriodId(uuid.uuid4()), "p", NOW.date(), NOW.date(), PeriodStatus.OPEN
        ),
        [],
        totals,
    ).balanced
    assert CurrencyTotal("USD").document() == {
        "currency": "USD",
        "opening": "0.00",
        "debits": "0.00",
        "credits": "0.00",
        "net": "0.00",
    }


async def post(
    reference: str, day: int, lines: tuple[Line, ...], description: str = ""
) -> Entry:
    """Post an entry dated on ``day`` of October 2026."""
    return await post_entry(
        PostEntry(
            source="tests",
            reference=reference,
            booked_on=date(2026, 10, day),
            description=description,
            lines=lines,
        ),
        AccountingUnitOfWork(),
    )


async def ledger(
    october: PeriodId,
) -> tuple[Account, Account, Account, list[Entry]]:
    """Cash, sales, and euros, and four entries in and around October."""
    cash, sales = await account("1000"), await account("4000", type=AccountType.INCOME)
    euros = await account("1100", "EUR")
    posted = []
    for minute, (reference, day, lines, description) in enumerate(
        [
            (
                "b",
                31,
                (Line.debit(cash.id, usd("2")), Line.credit(sales.id, usd("2"))),
                "Café",
            ),
            (
                "a",
                1,
                (
                    Line.credit(sales.id, usd("1.5")),
                    Line.debit(cash.id, usd("1.5"), "m"),
                ),
                "",
            ),
            (
                "c",
                31,
                (
                    Line.debit(euros.id, Money.of("3", "EUR")),
                    Line.credit(euros.id, Money.of("3", "EUR")),
                ),
                "=1+1",
            ),
        ]
    ):
        with freeze_time(NOW + timedelta(minutes=minute)):
            posted.append(await post(reference, day, lines, description))
    return cash, sales, euros, posted


@pytest.mark.usefixtures("books")
async def test_the_general_ledger_is_the_same_bytes_every_time() -> None:
    """Sorted keys, no spaces, amounts at their places, postings in their order."""
    october = await period("2026-10", date(2026, 10, 1), date(2026, 10, 31))
    await period("2026-11", date(2026, 11, 1), date(2026, 11, 30))
    cash, sales, euros, (b, a, c) = await ledger(october)
    await post_entry(
        PostEntry(
            source="tests",
            reference="next",
            booked_on=date(2026, 11, 1),
            lines=(Line.debit(cash.id, usd("9")), Line.credit(sales.id, usd("9"))),
        ),
        AccountingUnitOfWork(),
    )

    as_json = "".join([piece async for piece in general_ledger(october, "json")])
    as_csv = "".join([piece async for piece in general_ledger(october, "csv")])

    def posting(
        entry: Entry,
        line: int,
        account: Account,
        side: str,
        amount: str,
        net: str,
        memo: str = "",
    ) -> str:
        description = json.dumps(entry.description, ensure_ascii=False)
        return (
            f'{{"account_code":"{account.code}","account_id":"{account.id}",'
            f'"amount":"{amount}","booked_on":"{entry.booked_on.isoformat()}",'
            f'"currency":"{account.currency}","description":{description},'
            f'"entry_id":"{entry.id}","line":{line},"memo":"{memo}","net":"{net}",'
            f'"reference":"{entry.reference}","side":"{side}","source":"tests"}}'
        )

    assert as_json == (
        '{"format":"domainkit.accounting.general_ledger",'
        f'"period":{{"ends_on":"2026-10-31","id":"{october}","name":"2026-10",'
        '"starts_on":"2026-10-01","status":"open"},"postings":['
        + ",".join(
            [
                posting(a, 1, sales, "credit", "1.50", "-1.50"),
                posting(a, 2, cash, "debit", "1.50", "1.50", "m"),
                posting(b, 1, cash, "debit", "2.00", "2.00"),
                posting(b, 2, sales, "credit", "2.00", "-2.00"),
                posting(c, 1, euros, "debit", "3.00", "3.00"),
                posting(c, 2, euros, "credit", "3.00", "-3.00"),
            ]
        )
        + '],"totals":[{"credits":"3.00","currency":"EUR","debits":"3.00",'
        '"net":"0.00","opening":"0.00"},{"credits":"3.50","currency":"USD",'
        '"debits":"3.50","net":"0.00","opening":"0.00"}],"version":1}\n'
    )
    lines = as_csv.split("\r\n")
    assert lines[0] == (
        "booked_on,entry_id,source,reference,description,line,account_code,"
        "account_id,side,amount,currency,net,memo"
    )
    assert lines[5] == (
        f"2026-10-31,{c.id},tests,c,'=1+1,1,1100,{euros.id},debit,3.00,EUR,3.00,"
    )
    assert lines[7:] == [",,,,total,,,,,,EUR,0.00,", ",,,,total,,,,,,USD,0.00,", ""]


@pytest.mark.usefixtures("books")
async def test_the_trial_balance_opens_with_what_came_before_its_first_day() -> None:
    """An entry on the first day is a movement, one the day before an opening."""
    september = await period("2026-09", date(2026, 9, 1), date(2026, 9, 30))
    october = await period("2026-10", date(2026, 10, 1), date(2026, 10, 31))
    cash, sales = await account("1000"), await account("4000", type=AccountType.INCOME)
    for reference, day, amount in (
        ("s", date(2026, 9, 30), "4"),
        ("o", date(2026, 10, 1), "1"),
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

    report = await trial_balance(october)
    before = await trial_balance(september)

    assert [(r.code, r.opening, r.debits, r.credits) for r in report.rows] == [
        ("1000", Decimal("4.0000"), Decimal("1.0000"), Decimal(0)),
        ("4000", Decimal("-4.0000"), Decimal(0), Decimal("1.0000")),
    ]
    assert report.to_json() == (
        '{"accounts":['
        f'{{"account_id":"{cash.id}","closing":"5.00","code":"1000","credits":"0.00",'
        '"currency":"USD","debits":"1.00","name":"Account 1000","opening":"4.00",'
        '"type":"asset"},'
        f'{{"account_id":"{sales.id}","closing":"-5.00","code":"4000","credits":"1.00",'
        '"currency":"USD","debits":"0.00","name":"Account 4000","opening":"-4.00",'
        '"type":"income"}],"format":"domainkit.accounting.trial_balance",'
        f'"period":{{"ends_on":"2026-10-31","id":"{october}","name":"2026-10",'
        '"starts_on":"2026-10-01","status":"open"},"totals":[{"credits":"1.00",'
        '"currency":"USD","debits":"1.00","net":"0.00","opening":"0.00"}],'
        '"version":1}\n'
    )
    assert [(r.opening, r.debits) for r in before.rows] == [
        (Decimal(0), Decimal("4.0000")),
        (Decimal(0), Decimal(0)),
    ]
