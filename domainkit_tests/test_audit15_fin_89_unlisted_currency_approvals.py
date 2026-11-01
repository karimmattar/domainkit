"""FIN-89: a currency no threshold names moves any amount without approval.

``Threshold`` reads "a currency without an amount is never over", so
``APPROVALS.ENTRY_THRESHOLDS = {"USD": ...}`` (the fintech profile names
USD and EUR) guards nothing in pounds, yen, or any other currency the
ledger holds: one clerk posts a million GBP that a thousand USD would
have needed two approvers for. A maker-checker control fails open for
every currency it does not list; it should fail closed (an unlisted
currency needs approval, or ``check`` refuses a ledger account in a
currency the thresholds do not name).
"""

import pytest

from domainkit.contrib.accounting import (
    AccountingUnitOfWork,
    AccountType,
    Line,
    OpenAccount,
    PostEntry,
    open_account,
    post_entry,
)
from domainkit.contrib.accounting.tables import entries
from domainkit.contrib.approvals import ApprovalPending, ApprovalRequired
from domainkit.money import Money
from domainkit_tests.test_approvals import (  # noqa: F401 - the fixture.
    TODAY,
    books,
    count,
    project,
)

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("project")]


async def test_a_million_pounds_needs_approval_where_a_thousand_dollars_does() -> None:
    """ENTRY_THRESHOLDS names USD only: the GBP entry is still not one person's."""
    await books()  # The October period.
    accounts = []
    for code, kind in (("1100", AccountType.ASSET), ("4100", AccountType.INCOME)):
        opened = await open_account(
            OpenAccount(code=code, name=code, type=kind, currency="GBP"),
            AccountingUnitOfWork(),
        )
        accounts.append(opened)
    cash, sales = accounts
    million = Money.of("1000000.00", "GBP")

    with pytest.raises((ApprovalRequired, ApprovalPending)):
        await post_entry(
            PostEntry(
                source="shop",
                reference="gbp",
                booked_on=TODAY,
                lines=(Line.debit(cash.id, million), Line.credit(sales.id, million)),
            ),
            AccountingUnitOfWork(),
        )

    assert await count(entries, entries.c.reference == "gbp") == 0
