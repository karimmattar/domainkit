"""FIN-89: a currency no limit names moves any amount past every limit.

Limits are per currency, and a movement in a currency none of them names
is bounded by nothing. The fintech profile sets ``LIMITS.DEFAULTS`` for
USD and EUR only (10,000 a transaction, 25,000 a day per customer), and
its transfer takes ``Money`` in any ISO 4217 currency; a holder with a
wallet in another currency pays out without bound. As with the approval
thresholds, the control should fail closed for an unlisted currency, or
``check`` should refuse a configuration that leaves a ledger currency
unbounded.
"""

from decimal import Decimal

import pytest

from domainkit.clock import freeze_time
from domainkit.conf import override_settings
from domainkit.contrib.accounting import (
    AccountingUnitOfWork,
    AccountType,
    Line,
    OpenAccount,
    PostEntry,
    open_account,
    post_entry,
)
from domainkit.contrib.limits import LimitExceeded, TransactionLimitExceeded
from domainkit.money import Money
from domainkit_tests.test_limits import (  # noqa: F401 - the fixture.
    FUNDED,
    NOON,
    TODAY,
    books,
    period,
)

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("books")]
DEFAULTS = [
    {
        "code": "payments.customer",
        "kind": "holder",
        "currency": "EGP",
        "per_transaction": "10000.00",
        "window": "day",
        "max_amount": "25000.00",
    }
]
"""The fintech profile's customer limit, in the one currency it names."""


async def test_a_holders_payout_in_another_currency_is_still_limited() -> None:
    """A million in GBP out of the holder's wallet is not left unbounded."""
    await period()
    gbp = Money.of("1000000.00", "GBP")
    opened = []
    for code, kind, holder in (
        ("till", AccountType.ASSET, None),
        ("wallet", AccountType.LIABILITY, "customer-1"),
    ):
        opened.append(
            await open_account(
                OpenAccount(
                    code=code, name=code, type=kind, currency="GBP", holder=holder
                ),
                AccountingUnitOfWork(),
            )
        )
    till, wallet = opened

    def move(reference: str, debit: object, credit: object) -> PostEntry:
        return PostEntry(
            source="payments",
            reference=reference,
            booked_on=TODAY,
            lines=(
                Line.debit(debit.id, gbp),  # type: ignore[attr-defined]
                Line.credit(credit.id, gbp),  # type: ignore[attr-defined]
            ),
        )

    with override_settings(LIMITS={"DEFAULTS": DEFAULTS}):
        with freeze_time(FUNDED):
            await post_entry(move("fund", till, wallet), AccountingUnitOfWork())
        with (
            freeze_time(NOON),
            pytest.raises((LimitExceeded, TransactionLimitExceeded)),
        ):
            await post_entry(move("payout", wallet, till), AccountingUnitOfWork())

    async with AccountingUnitOfWork(readonly=True) as uow:
        assert (await uow.accounts.get(wallet.id)).balance == Decimal("1000000.0000")
