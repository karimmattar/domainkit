"""SEC-14b-3: inputs the ledger should refuse escape as 500s.

- ``CsvStatement.lines`` lets ``csv.Error`` through: a field over
  ``csv.field_size_limit()`` (128 KiB, well inside the route's 10 MiB).
  ``POST /accounts/{id}/statements`` answers 500 and ``manage.py
  accounting import`` dies with a traceback, where the docs promise
  ``invalid_statement`` for anything that cannot be read.
- ``Hold.place`` compares ``expires_at`` with an aware ``now``: a naive
  ``expires_at`` (``HoldIn`` takes ``"2027-01-01T00:00:00"``) raises
  ``TypeError``, so ``POST /holds`` answers 500.

Each fails on ``ac71361``.
"""

from datetime import datetime

import pytest

from domainkit.contrib.accounting import (
    AccountingUnitOfWork,
    InvalidStatement,
    PlaceHold,
    place_hold,
)
from domainkit.contrib.accounting.reconciliation import CsvStatement, import_statement
from domainkit.ddd import DomainError
from domainkit_tests.test_accounting import account, books, usd  # noqa: F401 - fixture.

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("books")]


async def test_an_oversized_field_is_an_invalid_statement() -> None:
    """A 200 KB description is refused as ``invalid_statement``."""
    bank = await account("1000")
    text = (
        "id,date,amount,currency,description\n"
        f"b-1,2026-10-01,1.00,USD,{'x' * 200_000}\n"
    )
    with pytest.raises(InvalidStatement):
        await import_statement(bank.id, CsvStatement(text))


async def test_a_naive_expiry_is_refused_as_a_domain_error() -> None:
    """A hold's ``expires_at`` without a time zone is a 4xx, not a crash."""
    wallet = await account("2100")
    with pytest.raises(DomainError):
        await place_hold(
            PlaceHold(
                account_id=wallet.id,
                amount=usd("1"),
                source="cards",
                reference="naive",
                expires_at=datetime(2027, 1, 1),  # noqa: DTZ001 - the point.
            ),
            AccountingUnitOfWork(),
        )
