"""The payout's own rules, in plain Python: no database, no HTTP."""

import pytest
from domainkit.money import Money

from domains.payouts.domain import InvalidPayout, Payout, PayoutSent


def test_a_payout_records_that_it_was_sent() -> None:
    """The IBAN is kept as banks print it, and the event names the payout."""
    payout = Payout.send("Acme Supplies Ltd", "de89 3704 0044 0532 0130 00", usd("1"))

    assert payout.iban == "DE89370400440532013000"
    assert [type(event) for event in payout.pull_events()] == [PayoutSent]


@pytest.mark.parametrize(
    ("payee", "iban", "amount"),
    [
        (" ", "DE89370400440532013000", "1.00"),
        ("Acme Supplies Ltd", "DE89-3704", "1.00"),
        ("Acme Supplies Ltd", "DE88370400440532013000", "1.00"),  # Check digits.
        ("Acme Supplies Ltd", "DE89370400440532013000", "0.00"),
    ],
)
def test_a_payout_names_its_payee_and_account_and_moves_money(
    payee: str, iban: str, amount: str
) -> None:
    """Refused (422): no payee, no IBAN, wrong check digits, nothing to move."""
    with pytest.raises(InvalidPayout):
        Payout.send(payee, iban, usd(amount))


def usd(amount: str) -> Money:
    """``amount`` US dollars."""
    return Money.of(amount, "USD")
