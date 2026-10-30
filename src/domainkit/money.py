"""Stand-in for ``domainkit.money`` until work package M1 lands (internal).

Only what ``domainkit.contrib.accounting`` uses, named as the contract
names it: ``Currency``, ``currency``, ``Money`` and its exceptions. M1's
module replaces this one whole, with its documentation.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Context, Decimal, Inexact, InvalidOperation
from typing import Self

from domainkit.exceptions import DomainkitError

__all__ = [
    "Currency",
    "CurrencyMismatch",
    "InexactAmount",
    "Money",
    "UnknownCurrency",
    "currency",
]


class UnknownCurrency(LookupError, DomainkitError):
    """No currency has that code."""


class InexactAmount(ValueError, DomainkitError):
    """An amount with more places than its currency's minor units."""


class CurrencyMismatch(ValueError, DomainkitError):
    """Arithmetic or a comparison between amounts of two currencies."""


@dataclass(frozen=True, slots=True)
class Currency:
    """An ISO 4217 currency: its code, number, and minor units."""

    code: str
    numeric: int
    minor_units: int


_CURRENCIES = {
    each.code: each
    for each in (
        Currency("EGP", 818, 2),
        Currency("EUR", 978, 2),
        Currency("GBP", 826, 2),
        Currency("JPY", 392, 0),
        Currency("KWD", 414, 3),
        Currency("SAR", 682, 2),
        Currency("USD", 840, 2),
        Currency("CLF", 990, 4),
    )
}


def currency(code: str) -> Currency:
    """The currency of ``code``.

    Raises:
        UnknownCurrency: No currency has that code.
    """
    found = _CURRENCIES.get(code)
    if found is None:
        raise UnknownCurrency(f"No currency has the code {code!r}.")
    return found


_EXACT = Context(prec=60, traps=[Inexact, InvalidOperation])


@dataclass(frozen=True, slots=True)
class Money:
    """An exact amount of one currency, with exactly its minor units."""

    amount: Decimal
    currency: Currency

    def __post_init__(self) -> None:
        """Hold the amount to the currency's places, refusing more.

        Raises:
            TypeError: The amount is not a ``Decimal``.
            InexactAmount: It has more places than the currency's.
        """
        if not isinstance(self.amount, Decimal) or not self.amount.is_finite():
            raise TypeError(f"An amount is a finite Decimal, not {self.amount!r}.")
        places = Decimal(1).scaleb(-self.currency.minor_units)
        try:
            exact = self.amount.quantize(places, context=_EXACT)
        except (Inexact, InvalidOperation):
            raise InexactAmount(
                f"{self.amount} has more places than {self.currency.code} has."
            ) from None
        object.__setattr__(self, "amount", exact)

    @classmethod
    def of(cls, amount: str | int | Decimal, code: str | Currency) -> Self:
        """``amount`` of the currency ``code``, from text, an int, or a Decimal.

        Raises:
            TypeError: ``amount`` is a float, or another type.
        """
        if isinstance(amount, bool) or not isinstance(amount, str | int | Decimal):
            raise TypeError(f"An amount is text, an int, or a Decimal: {amount!r}.")
        try:
            value = Decimal(amount)
        except InvalidOperation:
            raise TypeError(f"{amount!r} is not an amount.") from None
        return cls(value, code if isinstance(code, Currency) else currency(code))

    @classmethod
    def zero(cls, code: str | Currency) -> Self:
        """Nothing, in ``code``."""
        return cls.of(0, code)

    @classmethod
    def sum(cls, items: Iterable["Money"], code: str | Currency) -> Self:
        """The sum of ``items``, all in ``code``; zero when there are none."""
        total = cls.zero(code)
        for item in items:
            total = total + item
        return total

    def _same(self, other: "Money") -> None:
        """Refuse ``other`` of another currency."""
        if other.currency != self.currency:
            raise CurrencyMismatch(
                f"{self.currency.code} and {other.currency.code} do not mix."
            )

    def __add__(self, other: "Money") -> Self:
        """The sum, in the same currency."""
        self._same(other)
        return type(self)(self.amount + other.amount, self.currency)

    def __sub__(self, other: "Money") -> Self:
        """The difference, in the same currency."""
        self._same(other)
        return type(self)(self.amount - other.amount, self.currency)

    def __neg__(self) -> Self:
        """The amount with its sign turned."""
        return type(self)(-self.amount, self.currency)

    def __lt__(self, other: "Money") -> bool:
        """Less, in the same currency."""
        self._same(other)
        return self.amount < other.amount

    def __le__(self, other: "Money") -> bool:
        """Less or equal, in the same currency."""
        self._same(other)
        return self.amount <= other.amount

    def __gt__(self, other: "Money") -> bool:
        """More, in the same currency."""
        self._same(other)
        return self.amount > other.amount

    def __ge__(self, other: "Money") -> bool:
        """More or equal, in the same currency."""
        self._same(other)
        return self.amount >= other.amount
