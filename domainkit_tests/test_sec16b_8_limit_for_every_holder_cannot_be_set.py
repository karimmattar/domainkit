"""SEC-16b-8: a limit for every holder cannot be set once a few thousand moved.

When a windowed limit is set for every subject of a kind (``subject``
``""``, the way a bank tightens its daily limit for all customers in a
fraud wave), ``count_earlier`` counts every holder that moved in the
window, and ``_count`` writes their subject rows in one multi-row
``INSERT ... VALUES`` (``_ensure``: five bound parameters a subject),
then reads them with ``subject IN (...)`` and their usage with
``subject_id IN (...)``: one parameter each. PostgreSQL's protocol takes
32,767 parameters a statement, so with more than 6,553 holders that
moved in the window ``set_limit`` (``PUT /limits``, ``manage.py limits
set``) fails with ``InterfaceError``, a 500, and the limit is not set.
PERF-23's pattern, closed in the ledger's reports, comes back in the
code written to keep SEC-15b-5's promise. The control a bank reaches for
in an incident is the one it cannot apply.

PostgreSQL only (SQLite's build here binds more). The test asserts the
safe behaviour (the limit is set, and counts what every holder moved)
and fails on ``675dfd6``.
"""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Final

import pytest
from sqlalchemy import func, insert

from domainkit.contrib.accounting.tables import accounts, entries, postings
from domainkit.contrib.limits import Direction, Window, usage_of
from domainkit.db import connections
from domainkit_tests.test_limits import (  # noqa: F401 - the fixture.
    books,
    egp,
    limit,
    period,
)

pytestmark = pytest.mark.anyio
HOLDERS: Final = 7_000
"""More holders than one multi-row insert of subjects can bind (32,767 / 5)."""
DAY: Final = date(2026, 10, 15)
OPENED: Final = datetime(2026, 9, 1, tzinfo=UTC)


def _account(number: int, type_: str, holder: str | None) -> dict[str, object]:
    """An open account's row, in pounds."""
    return {
        "id": uuid.uuid4(),
        "code": f"A{number:06d}",
        "name": f"Account {number}",
        "type": type_,
        "currency": "EGP",
        "status": "open",
        "holder": holder,
        "may_go_negative": True,
        "balance": Decimal(1),
        "held": Decimal(0),
        "opened_at": OPENED,
        "closed_at": None,
        "version": 1,
    }


async def test_a_monthly_limit_for_every_holder_is_set_and_counts_them_all(
    books: str,
) -> None:
    """7,000 wallets each credited today; a monthly limit on what holders take in."""
    if books.startswith("sqlite"):
        pytest.skip("SQLite's build here binds more parameters than PostgreSQL.")
    await period()
    cash = _account(0, "asset", None)
    cash["balance"] = Decimal(HOLDERS)
    wallets = [
        _account(number, "liability", f"h-{number}")
        for number in range(1, HOLDERS + 1)
    ]
    entry_id = uuid.uuid4()
    lines = [
        {
            "id": uuid.uuid4(),
            "entry_id": entry_id,
            "line": 1,
            "account_id": cash["id"],
            "side": "debit",
            "amount": Decimal(HOLDERS),
            "currency": "EGP",
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
            "currency": "EGP",
            "booked_on": DAY,
            "memo": "",
        }
        for number, wallet in enumerate(wallets)
    ]
    async with connections.engine("default").begin() as connection:
        for start in range(0, len(wallets) + 1, 1000):
            await connection.execute(
                insert(accounts), [cash, *wallets][start : start + 1000]
            )
        await connection.execute(
            insert(entries).values(
                id=entry_id,
                source="payroll",
                reference="2026-10",
                booked_on=DAY,
                description="",
                period_id=None,
                reverses=None,
                fingerprint="0" * 64,
                posted_at=func.now(),  # In the window, on the database's clock.
            )
        )
        for start in range(0, len(lines), 1000):
            await connection.execute(insert(postings), lines[start : start + 1000])

    await limit(
        code="inflow.monthly",
        direction=Direction.IN,
        window=Window.MONTH,
        max_amount=egp("100"),
        time_zone="UTC",
    )

    (used,) = await usage_of("holder", "h-1", direction=Direction.IN)
    assert (used.used, used.count) == (egp("1"), 1)
