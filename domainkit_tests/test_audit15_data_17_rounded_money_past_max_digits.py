"""DATA-17: ``Money.of(..., rounding=...)`` makes money that money refuses.

``domainkit.money._amount`` checks ``MAX_DIGITS`` (38 digits before the
point) before ``_quantized`` rounds, so an amount that rounds up past it
is kept: 38 nines and .995 USD rounded ``HALF_UP`` is a 39-digit
``Money``. Every later use that builds money again refuses it:
``Money(m.amount, m.currency)``, ``copy``/``pickle`` (``__reduce__``),
its JSON read back, ``m + Money.zero("USD")``. A product ``multiply``
rounds goes the same way. The check belongs after the rounding.
"""

import copy
import pickle

import pytest

from domainkit.money import MAX_DIGITS, Money, MoneyError, Rounding

NEAR_THE_TOP = "9" * MAX_DIGITS + ".995"


def test_rounding_up_past_max_digits_is_refused_as_any_such_amount() -> None:
    """The rounded amount has at most ``MAX_DIGITS`` digits, or is refused."""
    try:
        made = Money.of(NEAR_THE_TOP, "USD", rounding=Rounding.HALF_UP)
    except MoneyError:
        return  # Refused, as Money("1" + "0" * 38, "USD") is.
    assert len(str(made.amount).split(".")[0].lstrip("-")) <= MAX_DIGITS


def test_money_made_is_money_that_copies() -> None:
    """Whatever ``Money.of`` returns survives ``copy`` and ``pickle``."""
    try:
        made = Money.of(NEAR_THE_TOP, "USD", rounding=Rounding.HALF_UP)
    except MoneyError:
        return
    assert copy.copy(made) == made
    assert pickle.loads(pickle.dumps(made)) == made  # noqa: S301 - our own bytes.


@pytest.mark.parametrize("rounding", [Rounding.UP, Rounding.CEILING])
def test_a_rounded_product_stays_within_max_digits(rounding: Rounding) -> None:
    """``multiply`` rounds no product past ``MAX_DIGITS`` either."""
    top = Money.of("9" * MAX_DIGITS + ".99", "USD")
    try:
        product = top.multiply("1.0000000000000000000000000000000000000001", rounding)
    except MoneyError:
        return
    assert len(str(product.amount).split(".")[0]) <= MAX_DIGITS
