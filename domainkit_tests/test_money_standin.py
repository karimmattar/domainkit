"""The stand-in for ``domainkit.money`` (dropped when M1's module lands)."""

from decimal import Decimal

import pytest

from domainkit.money import (
    Currency,
    CurrencyMismatch,
    InexactAmount,
    Money,
    UnknownCurrency,
    currency,
)


def test_currencies_are_looked_up_by_code() -> None:
    """Known codes give their currency; others are refused."""
    assert currency("KWD") == Currency("KWD", 414, 3)
    assert currency("JPY").minor_units == 0
    with pytest.raises(UnknownCurrency, match="'XXX'"):
        currency("XXX")


def test_amounts_keep_their_currencys_places_exactly() -> None:
    """Amounts are held to the minor units; more places are refused."""
    assert Money.of("10.5", "USD").amount == Decimal("10.50")
    assert str(Money.of("10.5", "USD").amount) == "10.50"
    assert str(Money.of(Decimal("1.5000"), "USD").amount) == "1.50"
    assert str(Money.of(7, "KWD").amount) == "7.000"
    assert Money.of("1", currency("JPY")).currency.code == "JPY"
    with pytest.raises(InexactAmount, match=r"1\.005 has more places than USD"):
        Money.of("1.005", "USD")
    with pytest.raises(InexactAmount):
        Money.of("0.5", "JPY")


@pytest.mark.parametrize("value", [1.5, True, None, "abc", Decimal("NaN")])
def test_floats_and_other_values_are_refused(value: object) -> None:
    """Only text, ints, and finite Decimals are amounts."""
    with pytest.raises(TypeError):
        Money.of(value, "USD")  # type: ignore[arg-type]


def test_arithmetic_and_comparisons_keep_to_one_currency() -> None:
    """Sums, differences, and comparisons of one currency; others refused."""
    ten, three = Money.of("10", "USD"), Money.of("3", "USD")
    assert (ten + three).amount == Decimal("13.00")
    assert (ten - three).amount == Decimal("7.00")
    assert (-three).amount == Decimal("-3.00")
    assert three < ten
    assert three <= three
    assert ten > three
    assert ten >= ten
    assert not ten < three
    assert not ten <= three
    assert not three > ten
    assert not three >= ten
    assert Money.zero("EUR").amount == Decimal("0.00")
    assert Money.sum([ten, three], "USD") == Money.of("13", "USD")
    assert Money.sum([], "USD") == Money.zero("USD")
    euro = Money.of("1", "EUR")
    for operation in (
        lambda: ten + euro,
        lambda: ten - euro,
        lambda: ten < euro,
        lambda: ten <= euro,
        lambda: ten > euro,
        lambda: ten >= euro,
    ):
        with pytest.raises(CurrencyMismatch, match="USD and EUR do not mix"):
            operation()
