"""Money: an amount in a currency, exact, with named rounding and allocation.

``Money`` is a value object: an amount (``Decimal``) with exactly its
currency's minor units, and the currency::

    from domainkit.money import Money, Rounding

    price = Money.of("10.5", "USD")          # Money('10.50', 'USD')
    price + Money.of("0.25", "USD")          # Money('10.75', 'USD')
    price.multiply("0.15", rounding=Rounding.HALF_UP)   # Money('1.58', 'USD')
    Money.of("100", "USD").allocate([1, 1, 1])  # 33.34, 33.33, 33.33

Nothing is rounded unless a rounding is named: an amount with more places
than its currency has (``Money.of("10.555", "USD")``) is refused with
``InexactAmount``, and so is a product that does not fit
(``multiply("0.15")`` without ``rounding``). Floats are refused everywhere.
Arithmetic and comparisons take amounts of one currency, and raise
``CurrencyMismatch`` for two; ``==`` is just false. Converting between
currencies needs a rate, which is the project's, from a source it trusts.

``Currency`` holds an ISO 4217 code, its numeric code, and its minor units
(the places of its amounts): ``currency("KWD")`` is ``Currency("KWD", 414,
3)``. The active ISO 4217 currencies are known (``ISO_4217_PUBLISHED`` says
from which list); ``register_currency`` adds a project's own, such as a
token or an internal unit, before any amount uses it.
"""

import csv
import re
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from datetime import date
from decimal import (
    MAX_EMAX,
    MAX_PREC,
    MIN_EMIN,
    ROUND_CEILING,
    ROUND_DOWN,
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    ROUND_UP,
    Context,
    Decimal,
    DivisionByZero,
    Inexact,
    InvalidOperation,
    Overflow,
)
from enum import StrEnum
from functools import cache
from importlib.resources import files
from types import MappingProxyType
from typing import Any, ClassVar, Final, Self

from pydantic import GetCoreSchemaHandler, GetJsonSchemaHandler
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import PydanticCustomError, core_schema

from domainkit.exceptions import DomainkitError, ImproperlyConfigured
from domainkit.utils.decimals import decimal_text

__all__ = [
    "ISO_4217_PUBLISHED",
    "MAX_DIGITS",
    "Currency",
    "CurrencyMismatch",
    "InexactAmount",
    "Money",
    "MoneyError",
    "Rounding",
    "UnknownCurrency",
    "currencies",
    "currencies_kept",
    "currency",
    "register_currency",
]

ISO_4217_PUBLISHED: Final = date(2026, 9, 17)
"""The date of the ISO 4217 list the known currencies come from (``iso4217.csv``)."""
MAX_DIGITS: Final = 38
"""The most digits an amount has before its point, as ``NUMERIC(38)`` holds."""
MAX_MINOR_UNITS: Final = 18
"""The most minor units a currency may have: the places ``ExactDecimal`` can hold."""
CODE_PATTERN: Final = r"^[A-Z][A-Z0-9]{2,7}$"
"""A currency code: ISO 4217's three letters, or up to eight for a registered one."""
AMOUNT_PATTERN: Final = r"^-?[0-9]+(\.[0-9]+)?$"
"""An amount as text: digits, a point and more digits, no exponent."""

_CODE: Final = re.compile(CODE_PATTERN)
_AMOUNT: Final = re.compile(AMOUNT_PATTERN)
_AMOUNT_LENGTH: Final = 2 * MAX_DIGITS + MAX_MINOR_UNITS + 2
"""The longest amount text worth parsing: past it, there are too many digits."""
_EXACT: Final = Context(
    prec=MAX_PREC,
    Emax=MAX_EMAX,
    Emin=MIN_EMIN,
    traps=[InvalidOperation, DivisionByZero, Overflow, Inexact],
)
"""Arithmetic that never rounds: a result that would is an error."""
_ROUNDING: Final = Context(
    prec=MAX_PREC, Emax=MAX_EMAX, Emin=MIN_EMIN, traps=[InvalidOperation, Overflow]
)
"""Quantizing to a currency's places, with the rounding each call names."""


class MoneyError(ValueError, DomainkitError):
    """An amount or a currency that money cannot hold; the HTTP layer answers 422.

    ``code`` names the error for clients, and ``summary`` says what is
    wrong in words safe to show (and translated); the message itself is
    for developers.
    """

    code: ClassVar[str] = "invalid_money"
    summary: ClassVar[str] = "The amount of money is not valid."


class UnknownCurrency(MoneyError):
    """A code that is neither an ISO 4217 currency nor a registered one."""

    code: ClassVar[str] = "unknown_currency"
    summary: ClassVar[str] = "The currency is not one this service knows."


class InexactAmount(MoneyError):
    """An amount with more places than its currency has; nothing is rounded."""

    code: ClassVar[str] = "inexact_amount"
    summary: ClassVar[str] = "The amount has more decimal places than its currency has."


class CurrencyMismatch(MoneyError):
    """Amounts of two currencies were added, subtracted, or compared."""

    code: ClassVar[str] = "currency_mismatch"
    summary: ClassVar[str] = "The amounts are in different currencies."


class Rounding(StrEnum):
    """How an amount that does not fit its currency's places is rounded.

    Each value is the ``decimal`` module's constant of the same name, so
    ``Rounding.HALF_EVEN`` can be given wherever ``decimal`` takes one.
    """

    HALF_EVEN = ROUND_HALF_EVEN
    """To the nearest; a half to the even neighbour (banker's rounding)."""
    HALF_UP = ROUND_HALF_UP
    """To the nearest; a half away from zero (as taught at school)."""
    DOWN = ROUND_DOWN
    """Toward zero: the part past the last place is dropped."""
    UP = ROUND_UP
    """Away from zero."""
    CEILING = ROUND_CEILING
    """Toward positive infinity."""
    FLOOR = ROUND_FLOOR
    """Toward negative infinity."""


class Currency:
    """A currency: its code, its ISO 4217 numeric code, and its minor units.

    ``minor_units`` are the places of its amounts: 2 for USD (cents), 0 for
    JPY, 3 for KWD. Take currencies from ``currency("USD")``, or make one
    with ``register_currency``; a ``Currency`` made by hand is not known to
    ``currency()``, so its amounts could not be read back from storage.
    Currencies are immutable, and equal when their code, numeric code, and
    minor units are.
    """

    __slots__ = ("code", "minor_units", "name", "numeric")

    code: str
    """The ISO 4217 alphabetic code (``"EGP"``), or a registered one."""
    numeric: int | None
    """The ISO 4217 numeric code (818); None for a registered currency without one."""
    minor_units: int
    """The places of an amount: the exponent of its minor unit, 0 to 18."""
    name: str
    """The currency's English name, as ISO 4217 gives it."""

    def __init__(
        self, code: str, numeric: int | None, minor_units: int, name: str = ""
    ) -> None:
        """Describe a currency; ``currency()`` and ``register_currency`` make them.

        Raises:
            ValueError: The code, numeric code, or minor units are out of bounds.
        """
        if not isinstance(code, str) or not _CODE.fullmatch(code):
            raise ValueError(
                "A currency code is three to eight capital letters or digits, "
                "starting with a letter (ISO 4217's are three letters)."
            )
        if numeric is not None and not (_whole(numeric) and 1 <= numeric <= 999):
            raise ValueError(
                f"{code}'s numeric code is from 1 to 999, or None, not {numeric!r}."
            )
        if not (_whole(minor_units) and 0 <= minor_units <= MAX_MINOR_UNITS):
            raise ValueError(
                f"{code}'s minor units are from 0 to {MAX_MINOR_UNITS}, "
                f"not {minor_units!r}."
            )
        for slot, value in (
            ("code", code),
            ("numeric", numeric),
            ("minor_units", minor_units),
            ("name", name),
        ):
            object.__setattr__(self, slot, value)

    def __eq__(self, other: object) -> bool:
        """The same code, numeric code, and minor units."""
        if not isinstance(other, Currency):
            return NotImplemented
        return self._key() == other._key()

    def __hash__(self) -> int:
        """Hash as ``==`` compares."""
        return hash(self._key())

    def _key(self) -> tuple[str, int | None, int]:
        """What makes two currencies the same."""
        return self.code, self.numeric, self.minor_units

    def __setattr__(self, name: str, value: object) -> None:
        """Refuse: currencies are immutable.

        Raises:
            AttributeError: Always.
        """
        raise AttributeError(f"A currency is immutable; it has no {name} to set.")

    def __reduce__(self) -> tuple[type["Currency"], tuple[str, int | None, int, str]]:
        """Pickle (and copy) as the constructor's arguments."""
        return Currency, (self.code, self.numeric, self.minor_units, self.name)

    def __repr__(self) -> str:
        """``Currency('KWD', 414, 3)``."""
        return f"Currency({self.code!r}, {self.numeric!r}, {self.minor_units!r})"

    def __str__(self) -> str:
        """The code: ``USD``."""
        return self.code

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: type[Any], handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        """A code in JSON (``"USD"``); a ``Currency`` or a code in Python."""
        return core_schema.no_info_plain_validator_function(
            _validated_currency,
            serialization=core_schema.plain_serializer_function_ser_schema(
                _code, when_used="json-unless-none"
            ),
        )

    @classmethod
    def __get_pydantic_json_schema__(
        cls, schema: core_schema.CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        """A string: the currency's code."""
        return _CURRENCY_SCHEMA.copy()


def _whole(value: object) -> bool:
    """Whether ``value`` is an ``int``, and not a ``bool``."""
    return isinstance(value, int) and not isinstance(value, bool)


@cache
def _iso_4217() -> Mapping[str, Currency]:
    """The active ISO 4217 currencies that have minor units, by code.

    Read from ``iso4217.csv`` on first use, comment lines skipped.
    """
    text = files(__name__).joinpath("iso4217.csv").read_text(encoding="utf-8")
    rows = csv.DictReader(
        line for line in text.splitlines() if not line.startswith("#")
    )
    return MappingProxyType(
        {
            row["code"]: Currency(
                row["code"], int(row["numeric"]), int(row["minor_units"]), row["name"]
            )
            for row in rows
        }
    )


_registered: dict[str, Currency] = {}
"""The currencies ``register_currency`` added, by code."""


def currency(code: str | Currency) -> Currency:
    """The currency of ``code`` (``"KWD"``): ISO 4217's, or a registered one.

    A ``Currency`` is returned as it is.

    Raises:
        UnknownCurrency: No currency has the code (codes are in capitals).
    """
    if isinstance(code, Currency):
        return code
    found = (
        _iso_4217().get(code) or _registered.get(code)
        if isinstance(code, str)
        else None
    )
    if found is None:
        shown = repr(code) if isinstance(code, str) and len(code) <= 8 else "the code"
        raise UnknownCurrency(
            f"No currency has {shown}: use an ISO 4217 code, in capitals, or "
            "register the currency with register_currency."
        )
    return found


def currencies() -> tuple[Currency, ...]:
    """Every known currency, ISO 4217's and the registered ones, by code."""
    known = {**_iso_4217(), **_registered}
    return tuple(known[code] for code in sorted(known))


def register_currency(
    code: str, *, minor_units: int, numeric: int | None = None, name: str = ""
) -> Currency:
    """Make ``code`` a currency, such as a token or a unit of the project's own.

    Register it where the project starts (a domain's ``ready()``), before
    any amount uses it. Registering the same currency again returns it.

    Raises:
        ImproperlyConfigured: The code is ISO 4217's, it was registered
            with other units, or the numeric code is another currency's.
        ValueError: The code, numeric code, or minor units are invalid.
    """
    made = Currency(code, numeric, minor_units, name)
    if code in _iso_4217():
        raise ImproperlyConfigured(
            f"{code} is an ISO 4217 currency, known already; register only "
            "currencies ISO 4217 does not list."
        )
    if (known := _registered.get(code)) is not None:
        if known != made:
            raise ImproperlyConfigured(
                f"{code} is registered already, as {known!r}; a currency's "
                "units cannot change while amounts use them."
            )
        return known
    if numeric is not None and any(
        other.numeric == numeric
        for other in (*_iso_4217().values(), *_registered.values())
    ):
        raise ImproperlyConfigured(
            f"The numeric code {numeric} is another currency's; give {code} "
            "its own, or none."
        )
    _registered[code] = made
    return made


@contextmanager
def currencies_kept() -> Iterator[None]:
    """Put the registered currencies back as they were when the block ends.

    What the block registered is dropped: the test plugin keeps each
    test's to itself.
    """
    kept = dict(_registered)
    try:
        yield
    finally:
        _registered.clear()
        _registered.update(kept)


class Money:
    """An amount of money in one currency; immutable, and exact.

    ``Money(amount, currency)`` takes the amount as a ``Decimal``, an
    ``int``, or text (``"10.50"``), never a float, and the currency as a
    ``Currency`` or a code. The amount is kept with exactly the currency's
    places (``Money("10.5", "USD").amount`` is ``Decimal("10.50")``); one
    with more places than that is refused, unless ``Money.of`` is told how
    to round it.

    ``+``, ``-``, ``abs()``, and ``<`` (and the other orderings) take
    amounts of the same currency; scale an amount with ``multiply``, and
    split it with ``allocate``. In JSON an amount is
    ``{"amount": "10.50", "currency": "USD"}``, the amount as text.
    """

    __slots__ = ("amount", "currency")

    amount: Decimal
    """The amount, with exactly ``currency.minor_units`` places."""
    currency: Currency
    """The currency of the amount."""

    def __init__(self, amount: Decimal | int | str, currency: Currency | str) -> None:
        """Hold ``amount`` of ``currency``, exactly.

        Raises:
            TypeError: The amount is a float, a ``bool``, or not a number.
            ValueError: The text is not an amount, the amount is not finite,
                or it has more than ``MAX_DIGITS`` digits before the point.
            InexactAmount: It has more places than the currency.
            UnknownCurrency: The code names no currency.
        """
        held = _currency(currency)
        _set(self, _quantized(_amount(amount), held, None), held)

    @classmethod
    def of(
        cls,
        amount: Decimal | int | str,
        currency: Currency | str,
        *,
        rounding: Rounding | None = None,
    ) -> Self:
        """``amount`` of ``currency``: ``Money.of("10.5", "USD")``.

        Without ``rounding``, as ``Money(amount, currency)``: an amount with
        more places than the currency is refused. With it, it is rounded
        that way (``Money.of(Decimal("2.675"), "USD", rounding=Rounding.HALF_UP)``
        is 2.68).

        Raises:
            InexactAmount: It has more places, and no rounding was named.
        """
        held = _currency(currency)
        made = cls.__new__(cls)
        _set(made, _quantized(_amount(amount), held, rounding), held)
        return made

    @classmethod
    def zero(cls, currency: Currency | str) -> Self:
        """Nothing, in ``currency``: ``0.00`` USD."""
        return cls.from_minor(0, currency)

    @classmethod
    def from_minor(cls, minor: int, currency: Currency | str) -> Self:
        """The amount of ``minor`` minor units: ``from_minor(1050, "USD")`` is 10.50.

        Raises:
            TypeError: ``minor`` is not an ``int``.
        """
        if not _whole(minor):
            raise TypeError(
                f"Money.from_minor takes a whole number of minor units, not "
                f"{type(minor).__name__}."
            )
        held = _currency(currency)
        return cls.of(Decimal(minor).scaleb(-held.minor_units, context=_EXACT), held)

    @classmethod
    def sum(cls, items: Iterable["Money"], currency: Currency | str) -> Self:
        """The total of ``items``, all in ``currency``; zero if there are none.

        Raises:
            CurrencyMismatch: An item is in another currency.
        """
        held = _currency(currency)
        total = Decimal(0)
        for item in items:
            _check_same(held, item.currency, "add")
            total = _EXACT.add(total, item.amount)
        return cls.of(total, held)

    @property
    def minor(self) -> int:
        """The amount in minor units: 1050 for 10.50 USD."""
        return int(self.amount.scaleb(self.currency.minor_units, context=_EXACT))

    def multiply(
        self, factor: Decimal | int | str, rounding: Rounding | None = None
    ) -> "Money":
        """This amount times ``factor``, in the same currency.

        The product is exact; ``rounding`` says how to round one with more
        places than the currency (a share, a rate, a tax), and without it
        such a product is refused rather than rounded.

        Raises:
            TypeError: ``factor`` is a float, a ``bool``, or not a number.
            InexactAmount: The product needs rounding, and none was named.
        """
        product = _EXACT.multiply(self.amount, _number(factor, "factor"))
        return Money.of(product, self.currency, rounding=rounding)

    def allocate(self, ratios: Iterable[int | Decimal]) -> list["Money"]:
        """This amount split in the proportions of ``ratios``; the parts sum to it.

        ``Money.of("100", "USD").allocate([1, 1, 1])`` is 33.34, 33.33, and
        33.33. Each part first gets its share of the minor units, rounded
        toward zero; the minor units left over go one each to the parts
        whose shares lost most (the largest remainders), the first of equal
        ones first. A part of ratio 0 gets nothing, and a negative amount is
        split as its opposite, each part negated.

        Raises:
            ValueError: No ratios, a negative one, all of them zero, or one
                with more than ``MAX_DIGITS`` digits on either side of its point.
            TypeError: A ratio is a float, a ``bool``, or not a number.
        """
        if self.minor < 0:
            return [-part for part in (-self).allocate(ratios)]
        weights = _weights(ratios)
        whole = sum(weights)
        units = self.minor
        shares = [units * weight // whole for weight in weights]
        lost = [units * weight % whole for weight in weights]
        left = units - sum(shares)
        for index in sorted(range(len(weights)), key=lambda i: -lost[i])[:left]:
            shares[index] += 1
        return [Money.from_minor(share, self.currency) for share in shares]

    def is_zero(self) -> bool:
        """Whether the amount is zero."""
        return self.amount.is_zero()

    def __add__(self, other: object) -> "Money":
        """The sum of two amounts of one currency.

        Raises:
            CurrencyMismatch: ``other`` is in another currency.
        """
        if not isinstance(other, Money):
            return NotImplemented
        _check_same(self.currency, other.currency, "add")
        return Money.of(_EXACT.add(self.amount, other.amount), self.currency)

    def __radd__(self, other: object) -> "Money":
        """``0 + amount`` is the amount, so ``sum()`` of amounts works.

        ``sum()`` of none is the integer 0: use ``Money.sum`` for a total
        that may have no items.
        """
        if _whole(other) and other == 0:
            return self
        return NotImplemented

    def __sub__(self, other: object) -> "Money":
        """The difference of two amounts of one currency.

        Raises:
            CurrencyMismatch: ``other`` is in another currency.
        """
        if not isinstance(other, Money):
            return NotImplemented
        _check_same(self.currency, other.currency, "subtract")
        return Money.of(_EXACT.subtract(self.amount, other.amount), self.currency)

    def __neg__(self) -> "Money":
        """The opposite amount."""
        return Money.of(self.amount.copy_negate(), self.currency)

    def __pos__(self) -> "Money":
        """The amount itself."""
        return self

    def __abs__(self) -> "Money":
        """The amount without its sign."""
        return Money.of(self.amount.copy_abs(), self.currency)

    def __eq__(self, other: object) -> bool:
        """Equal amounts of the same currency; another currency is never equal."""
        if not isinstance(other, Money):
            return NotImplemented
        return self.currency == other.currency and self.amount == other.amount

    def __hash__(self) -> int:
        """Hash by amount and currency, as ``==`` compares."""
        return hash((self.amount, self.currency))

    def __lt__(self, other: object) -> bool:
        """Less than ``other``, of the same currency."""
        return self._ordered(other) < 0

    def __le__(self, other: object) -> bool:
        """At most ``other``, of the same currency."""
        return self._ordered(other) <= 0

    def __gt__(self, other: object) -> bool:
        """More than ``other``, of the same currency."""
        return self._ordered(other) > 0

    def __ge__(self, other: object) -> bool:
        """At least ``other``, of the same currency."""
        return self._ordered(other) >= 0

    def _ordered(self, other: object) -> int:
        """-1, 0, or 1 as this amount sorts before, with, or after ``other``.

        Raises:
            TypeError: ``other`` is not money.
            CurrencyMismatch: It is in another currency.
        """
        if not isinstance(other, Money):
            raise TypeError(f"Money compares with Money, not {type(other).__name__}.")
        _check_same(self.currency, other.currency, "compare")
        return int(self.amount.compare(other.amount))

    def __setattr__(self, name: str, value: object) -> None:
        """Refuse: money is immutable.

        Raises:
            AttributeError: Always.
        """
        raise AttributeError(f"Money is immutable; it has no {name} to set.")

    def __delattr__(self, name: str) -> None:
        """Refuse: money is immutable.

        Raises:
            AttributeError: Always.
        """
        raise AttributeError(f"Money is immutable; it has no {name} to delete.")

    def __reduce__(self) -> tuple[type["Money"], tuple[Decimal, Currency]]:
        """Pickle (and copy) as the constructor's arguments."""
        return Money, (self.amount, self.currency)

    def __repr__(self) -> str:
        """``Money('10.50', 'USD')``."""
        return f"Money({decimal_text(self.amount)!r}, {self.currency.code!r})"

    def __str__(self) -> str:
        """``10.50 USD``."""
        return f"{decimal_text(self.amount)} {self.currency.code}"

    def __composite_values__(self) -> tuple[Decimal, str]:
        """The amount and the code, as the two columns of a table hold them."""
        return self.amount, self.currency.code

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: type[Any], handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        """``{"amount": "10.50", "currency": "USD"}`` in JSON; ``Money`` in Python."""
        return core_schema.no_info_plain_validator_function(
            _validated_money,
            serialization=core_schema.plain_serializer_function_ser_schema(
                _dumped, when_used="json-unless-none"
            ),
            ref=f"{__name__}.Money",  # One definition, Money, that schemas refer to.
        )

    @classmethod
    def __get_pydantic_json_schema__(
        cls, schema: core_schema.CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        """An object of the amount, as text, and the currency's code."""
        return {
            "type": "object",
            "title": "Money",
            "description": (
                "An amount of money: the amount as text, with its currency's "
                "decimal places, and the ISO 4217 code of the currency."
            ),
            "properties": {
                "amount": {
                    "type": "string",
                    "pattern": AMOUNT_PATTERN,
                    "examples": ["10.50"],
                },
                "currency": _CURRENCY_SCHEMA.copy(),
            },
            "required": ["amount", "currency"],
            "additionalProperties": False,
        }


_CURRENCY_SCHEMA: Final[JsonSchemaValue] = {
    "type": "string",
    "pattern": CODE_PATTERN,
    "description": "An ISO 4217 currency code.",
    "examples": ["USD"],
}


def _set(money: Money, amount: Decimal, currency: Currency) -> None:
    """Fill a new ``Money``, past its ``__setattr__``."""
    object.__setattr__(money, "amount", amount)
    object.__setattr__(money, "currency", currency)


def _currency(value: object) -> Currency:
    """A ``Currency`` or a code, as the currency.

    Raises:
        TypeError: ``value`` is neither.
    """
    if isinstance(value, Currency | str):
        return currency(value)
    raise TypeError(
        f"A currency is a Currency or its code, not {type(value).__name__}."
    )


def _number(value: object, what: str) -> Decimal:
    """``value`` (a ``Decimal``, an ``int``, or text) as a finite ``Decimal``.

    Raises:
        TypeError: It is a float, a ``bool``, or not a number.
        ValueError: Text that is not a number, or a value that is not finite.
    """
    if isinstance(value, float):
        raise TypeError(
            f"Money never takes a float {what} (floats are inexact): give a "
            "Decimal, an int, or text."
        )
    if isinstance(value, bool) or not isinstance(value, Decimal | int | str):
        raise TypeError(
            f"A {what} is a Decimal, an int, or text, not {type(value).__name__}."
        )
    if isinstance(value, str):
        if len(value) > _AMOUNT_LENGTH or not _AMOUNT.fullmatch(value):
            raise ValueError(
                f"A {what} as text is digits with an optional point and sign, "
                "such as 10.50, with no exponent."
            )
        return Decimal(value)
    number = Decimal(value)
    if not number.is_finite():
        raise ValueError(f"A {what} is a finite number, not {number}.")
    return number


def _amount(value: object) -> Decimal:
    """``value`` as an amount: a finite ``Decimal`` of at most ``MAX_DIGITS``.

    Raises:
        ValueError: It has more digits before the point.
    """
    amount = _number(value, "amount")
    if not amount.is_zero() and amount.adjusted() >= MAX_DIGITS:
        raise ValueError(f"An amount has at most {MAX_DIGITS} digits before the point.")
    return amount


def _quantized(
    amount: Decimal, currency: Currency, rounding: Rounding | None
) -> Decimal:
    """``amount`` with exactly ``currency``'s places; rounded only as asked.

    Zero has no sign.

    Raises:
        InexactAmount: It has more places, and no rounding was named.
    """
    places = Decimal(1).scaleb(-currency.minor_units)
    if rounding is not None:
        rule = Rounding(rounding).value
        quantized = amount.quantize(places, rounding=rule, context=_ROUNDING)
    else:
        try:
            quantized = amount.quantize(places, context=_EXACT)
        except Inexact:
            raise InexactAmount(
                f"{currency.code} has {currency.minor_units} decimal places; an "
                "amount with more is refused rather than rounded: name a Rounding."
            ) from None
    return quantized.copy_abs() if quantized.is_zero() else quantized


def _check_same(mine: Currency, theirs: Currency, verb: str) -> None:
    """Refuse two currencies where one is needed.

    Raises:
        CurrencyMismatch: They differ.
    """
    if mine != theirs:
        raise CurrencyMismatch(
            f"Cannot {verb} {theirs.code} and {mine.code} amounts: convert one "
            "first, at a rate the project trusts."
        )


def _weights(ratios: Iterable[int | Decimal]) -> list[int]:
    """``ratios`` as integers in the same proportions.

    Raises:
        ValueError: No ratios, a negative one, all zero, or too many digits.
        TypeError: One is a float, a ``bool``, or not a number.
    """
    values = list(ratios)
    if not values:
        raise ValueError("Allocate to at least one ratio.")
    numbers = []
    for value in values:
        if isinstance(value, str):
            raise TypeError("A ratio is a Decimal or an int, not text.")
        number = _number(value, "ratio")
        if number < 0:
            raise ValueError("Ratios cannot be negative.")
        exponent = int(number.as_tuple().exponent)
        if number.adjusted() >= MAX_DIGITS or exponent < -MAX_DIGITS:
            raise ValueError(
                f"A ratio has at most {MAX_DIGITS} digits on either side of its point."
            )
        numbers.append(number)
    places = max(-int(number.as_tuple().exponent) for number in numbers)
    weights = [_whole_units(number, places) for number in numbers]
    if not any(weights):
        raise ValueError("At least one ratio must be more than zero.")
    return weights


def _whole_units(number: Decimal, places: int) -> int:
    """``number`` times ``10 ** places``, which is whole, computed in integers."""
    _, digits, exponent = number.as_tuple()
    coefficient = int("".join(map(str, digits)))
    scale: int = 10 ** (int(exponent) + places)  # Not negative: places is the most.
    return coefficient * scale


def _validated_money(value: object) -> Money:
    """Money from pydantic's input: ``Money``, or the amount and currency.

    Raises:
        PydanticCustomError: The input is not money, with a message to show.
    """
    if isinstance(value, Money):
        return value
    if not isinstance(value, Mapping) or set(value) != {"amount", "currency"}:
        raise PydanticCustomError(
            "money_type",
            "Give money as an object of an amount and a currency, and nothing else.",
        )
    try:
        return Money(value["amount"], value["currency"])
    except MoneyError as exc:
        raise PydanticCustomError(exc.code, exc.summary) from None
    except (TypeError, ValueError):
        raise PydanticCustomError(
            "money_amount",
            "Give the amount as text, such as 10.50, with at most 38 digits "
            "before the point.",
        ) from None


def _validated_currency(value: object) -> Currency:
    """A currency from pydantic's input: a ``Currency``, or its code.

    Raises:
        PydanticCustomError: The input names no currency.
    """
    try:
        return _currency(value)
    except (UnknownCurrency, TypeError):
        raise PydanticCustomError(
            UnknownCurrency.code, UnknownCurrency.summary
        ) from None


def _dumped(money: Money) -> dict[str, str]:
    """Money as JSON: the amount as fixed-point text, and the currency's code."""
    return {"amount": decimal_text(money.amount), "currency": money.currency.code}


def _code(value: Currency) -> str:
    """A currency as JSON: its code."""
    return value.code
