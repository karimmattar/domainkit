"""FIN-91: an amount or a balance past the ledger's columns fails as a server error.

The ledger stores amounts as ``NUMERIC(18, 4)``: 14 digits before the
point in every currency, so 10^14 of any unit (a hundred trillion; in
dong or rial, a few billion dollars, an ordinary total for a bank's own
accounts). ``Money`` takes 38 digits, and nothing in ``Entry.draft``,
``Account.post``, or ``Hold.place`` checks the columns: the posting
reaches the flush, where ``ExactDecimal`` raises a ``ValueError`` that
SQLAlchemy wraps in ``StatementError``. Over HTTP that is a 500 with no
code, not the 422 ``amount_out_of_range`` that ``domainkit.money``
promises for a mapped amount; posted from a message, it is not one of
``tasks.REFUSALS``, so the worker retries it until it dead-letters.
"""

import pytest

from domainkit.contrib.accounting import AccountingUnitOfWork, post_entry
from domainkit.contrib.accounting.tables import entries
from domainkit.ddd import DomainError
from domainkit.money import MoneyError
from domainkit_tests.test_accounting import (  # noqa: F401 - the fixture.
    account,
    books,
    count,
    period,
    transfer,
    usd,
)

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("books")]


async def test_a_line_too_large_for_the_books_is_refused_as_such() -> None:
    """10^15 dollars: refused as an amount the ledger cannot hold."""
    await period()
    cash, bank = await account("1000"), await account("1001")

    with pytest.raises((DomainError, MoneyError)):
        await post_entry(
            transfer(cash, bank, usd("1000000000000000"), "big"),
            AccountingUnitOfWork(),
        )

    assert await count(entries) == 0


async def test_a_balance_that_would_overflow_is_refused_as_such() -> None:
    """Twice 9 * 10^13 on one account: the second is refused as out of range."""
    await period()
    cash, bank = await account("1000"), await account("1001")
    await post_entry(
        transfer(cash, bank, usd("90000000000000"), "first"), AccountingUnitOfWork()
    )

    with pytest.raises((DomainError, MoneyError)):
        await post_entry(
            transfer(cash, bank, usd("90000000000000"), "second"),
            AccountingUnitOfWork(),
        )

    assert await count(entries) == 1
