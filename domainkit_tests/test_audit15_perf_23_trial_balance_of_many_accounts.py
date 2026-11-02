"""PERF-23: a trial balance of more than 32,767 accounts cannot be made.

``trial_balance`` sums the postings, then reads the chart of every
account it found with ``accounts.c.id.in_(ids)``: one bound parameter
per account. PostgreSQL's protocol (asyncpg) takes at most 32,767
parameters a statement, and SQLite 32,766, so a ledger with more
accounts that moved than that (a bank's customer wallets: one account
each) gets ``InterfaceError`` / ``OperationalError`` from
``trial_balance``, ``GET /periods/{id}/trial-balance`` answers 500, and
``manage.py accounting trial-balance`` fails, however small the period.

The right behaviour: the report reads the chart in the same statement
(a join), or in bounded chunks, and lists every account.
"""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Final

import pytest
from sqlalchemy import insert

from domainkit.contrib.accounting import trial_balance
from domainkit.contrib.accounting.tables import accounts, entries, postings
from domainkit.db import connections
from domainkit_tests.test_accounting import (
    books,  # noqa: F401 - the fixture.
    period,
)

pytestmark = pytest.mark.anyio
WALLETS: Final = 33_000
"""More accounts than one statement can bind as parameters."""
DAY: Final = date(2026, 10, 15)
NOW: Final = datetime(2026, 10, 15, 12, tzinfo=UTC)


def _account(number: int, type_: str) -> dict[str, object]:
    """An open dollar account's row."""
    return {
        "id": uuid.uuid4(),
        "code": f"A{number:06d}",
        "name": f"Account {number}",
        "type": type_,
        "currency": "USD",
        "status": "open",
        "holder": None,
        "may_go_negative": True,
        "balance": Decimal(1),
        "held": Decimal(0),
        "opened_at": NOW,
        "closed_at": None,
        "version": 1,
    }


async def test_a_trial_balance_lists_every_account_that_moved(books: str) -> None:  # noqa: F811
    """33,000 wallets credited by one entry: the report has 33,001 rows."""
    period_id = await period()
    cash = _account(0, "asset")
    cash["balance"] = Decimal(WALLETS)
    wallets = [_account(number, "liability") for number in range(1, WALLETS + 1)]
    entry_id = uuid.uuid4()
    lines = [
        {
            "id": uuid.uuid4(),
            "entry_id": entry_id,
            "line": 1,
            "account_id": cash["id"],
            "side": "debit",
            "amount": Decimal(WALLETS),
            "currency": "USD",
            "booked_on": DAY,
            "memo": "",
        }
    ] + [
        {
            "id": uuid.uuid4(),
            "entry_id": entry_id,
            "line": number + 2,
            "account_id": wallet["id"],
            "side": "credit",
            "amount": Decimal(1),
            "currency": "USD",
            "booked_on": DAY,
            "memo": "",
        }
        for number, wallet in enumerate(wallets)
    ]
    async with connections.engine("default").begin() as connection:
        await connection.execute(insert(accounts), [cash, *wallets])
        await connection.execute(
            insert(entries).values(
                id=entry_id,
                source="payroll",
                reference="2026-10",
                booked_on=DAY,
                description="",
                period_id=period_id,
                reverses=None,
                fingerprint="0" * 64,
                posted_at=NOW,
            )
        )
        await connection.execute(insert(postings), lines)

    report = await trial_balance(period_id)

    assert len(report.rows) == WALLETS + 1
    assert report.balanced
