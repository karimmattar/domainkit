"""FIN-87: a closed period's figures change when an earlier date takes entries.

Closing a period is meant to freeze it: "A closed period stays closed:
corrections go in an open one" (``docs/contrib/accounting.md``). But only
dates *inside* a closed period are refused. A period opened afterwards
for dates before it (``open_period`` checks overlaps only), or, with
``REQUIRE_PERIOD = False``, any date in a gap before it, takes entries
that move the closed period's opening and closing balances: its trial
balance, already reported, reads otherwise. A ledger's close must lock
every date up to the end of the latest closed period.
"""

from contextlib import suppress
from datetime import date

import pytest

from domainkit.conf import override_settings
from domainkit.contrib.accounting import (
    AccountingUnitOfWork,
    AccountType,
    ClosePeriod,
    Line,
    OpenPeriod,
    PostEntry,
    close_period,
    open_period,
    post_entry,
    trial_balance,
)
from domainkit.ddd import DomainError
from domainkit_tests.test_accounting import (  # noqa: F401 - the fixture.
    account,
    books,
    period,
    usd,
)

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("books")]
FEBRUARY = (date(2026, 2, 1), date(2026, 2, 28))


def entry(reference: str, booked_on: date, cash: object, equity: object) -> PostEntry:
    """Cash of 100 (or 500 for the late one) from the owners, on ``booked_on``."""
    amount = usd("100") if reference == "feb" else usd("500")
    return PostEntry(
        source="tests",
        reference=reference,
        booked_on=booked_on,
        lines=(
            Line.debit(cash.id, amount),  # type: ignore[attr-defined]
            Line.credit(equity.id, amount),  # type: ignore[attr-defined]
        ),
    )


async def closed_february() -> tuple[object, object, object, str]:
    """February booked and closed; its accounts, its id, its trial balance."""
    february = await period("2026-02", *FEBRUARY)
    cash = await account("1000")
    equity = await account("3000", type=AccountType.EQUITY)
    await post_entry(
        entry("feb", date(2026, 2, 10), cash, equity), AccountingUnitOfWork()
    )
    await close_period(ClosePeriod(period_id=february), AccountingUnitOfWork())
    return cash, equity, february, (await trial_balance(february)).to_json()


async def test_a_period_opened_before_a_closed_one_leaves_it_as_reported() -> None:
    """January, opened after February closed, may not move February's balances."""
    cash, equity, february, reported = await closed_february()

    try:
        await open_period(
            OpenPeriod(
                name="2026-01", starts_on=date(2026, 1, 1), ends_on=date(2026, 1, 31)
            ),
            AccountingUnitOfWork(),
        )
        await post_entry(
            entry("jan", date(2026, 1, 15), cash, equity), AccountingUnitOfWork()
        )
    except DomainError:
        pass  # Refused: the books before February's end are closed.

    assert (await trial_balance(february)).to_json() == reported  # type: ignore[arg-type]


async def test_an_entry_outside_every_period_leaves_a_closed_one_as_reported() -> None:
    """With REQUIRE_PERIOD False, a date before a closed period is closed too."""
    cash, equity, february, reported = await closed_february()

    with (
        override_settings(ACCOUNTING={"REQUIRE_PERIOD": False}),
        suppress(DomainError),  # Refused: the date is before a closed period.
    ):
        await post_entry(
            entry("jan", date(2026, 1, 15), cash, equity), AccountingUnitOfWork()
        )

    assert (await trial_balance(february)).to_json() == reported  # type: ignore[arg-type]
