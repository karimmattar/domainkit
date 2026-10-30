"""Money and currencies: exact amounts, named rounding, allocation (FIN-01..03)."""

import copy
import pickle
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from importlib.resources import files

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel, TypeAdapter, ValidationError

from domainkit.contrib.messaging.registry import dump_arguments, dump_event, load_event
from domainkit.ddd import DomainEvent, ValueObject
from domainkit.exceptions import DomainkitError, ImproperlyConfigured
from domainkit.money import (
    ISO_4217_PUBLISHED,
    MAX_DIGITS,
    Currency,
    CurrencyMismatch,
    InexactAmount,
    Money,
    MoneyError,
    Rounding,
    UnknownCurrency,
    currencies,
    currencies_kept,
    currency,
    register_currency,
)
from domainkit.testing import Factory, MoneyFactory, SelfAttribute, SubFactory
from domainkit.utils.decimals import jsonable

USD = currency("USD")
ISO = [each for each in currencies() if each.numeric is not None]


def usd(amount: str) -> Money:
    """``amount`` US dollars."""
    return Money.of(amount, "USD")


# Currencies.


@pytest.mark.parametrize(
    ("code", "numeric", "minor_units"),
    [
        ("JPY", 392, 0),
        ("USD", 840, 2),
        ("EGP", 818, 2),
        ("KWD", 414, 3),
        ("BHD", 48, 3),
        ("OMR", 512, 3),
        ("JOD", 400, 3),
        ("TND", 788, 3),
        ("LYD", 434, 3),
        ("IQD", 368, 3),
        ("CLF", 990, 4),
        ("UYW", 927, 4),
        ("ISK", 352, 0),
        ("VES", 928, 2),
        ("XCG", 532, 2),
    ],
)
def test_iso_4217_currencies_have_their_numeric_codes_and_minor_units(
    code: str, numeric: int, minor_units: int
) -> None:
    """The list holds the active currencies as ISO 4217 publishes them."""
    found = currency(code)

    assert (found.code, found.numeric, found.minor_units) == (
        code,
        numeric,
        minor_units,
    )
    assert found.name
    assert currency(found) is found


def test_the_list_is_iso_4217s_active_one_without_units_that_have_no_minor_units() -> (
    None
):
    """165 currencies, as published, and none of gold, SDR, or the test codes."""
    text = files("domainkit.money").joinpath("iso4217.csv").read_text(encoding="utf-8")

    assert len(ISO) == 165
    assert date(2026, 9, 17) == ISO_4217_PUBLISHED
    assert "Published 2026-09-17" in text
    assert "six-group.com" in text
    assert {each.minor_units for each in ISO} == {0, 2, 3, 4}
    for code in ("XAU", "XDR", "XXX", "XTS", "ANG", "HRK", "SLL"):
        with pytest.raises(UnknownCurrency):
            currency(code)
    assert [each.code for each in ISO] == sorted(each.code for each in ISO)


@pytest.mark.parametrize("code", ["usd", "US", "", "U$D", "USD\n", "ABCDEFGHI"])
def test_unknown_codes_are_refused(code: str) -> None:
    """Codes are in capitals; an unknown one is an error, never a guess."""
    with pytest.raises(UnknownCurrency) as caught:
        currency(code)

    assert isinstance(caught.value, ValueError)
    assert isinstance(caught.value, DomainkitError)
    assert caught.value.code == "unknown_currency"


def test_an_unknown_code_is_quoted_only_when_short() -> None:
    """The message names a code a person could read, not a page of text."""
    with pytest.raises(UnknownCurrency, match="'EUROS'"):
        currency("EUROS")
    with pytest.raises(UnknownCurrency, match="the code") as caught:
        currency("X" * 1000)
    assert "XXXX" not in str(caught.value)
    with pytest.raises(UnknownCurrency, match="the code"):
        currency(840)  # type: ignore[arg-type]


def test_a_project_registers_its_own_currencies() -> None:
    """A token or an internal unit, known to currency() once registered."""
    token = register_currency("USDC", minor_units=6, name="USD Coin")

    assert currency("USDC") is token
    assert (token.code, token.numeric, token.minor_units, token.name) == (
        "USDC",
        None,
        6,
        "USD Coin",
    )
    assert register_currency("USDC", minor_units=6, name="other name") is token
    assert token in currencies()
    assert Money.of("1.000001", "USDC").minor == 1_000_001


def test_registering_is_refused_for_iso_codes_changed_units_and_taken_numbers() -> None:
    """A currency's units cannot change under the amounts that use them."""
    register_currency("PTS", minor_units=0, numeric=990 - 1)

    with pytest.raises(ImproperlyConfigured, match="ISO 4217"):
        register_currency("USD", minor_units=2, numeric=840)
    with pytest.raises(ImproperlyConfigured, match="registered already"):
        register_currency("PTS", minor_units=2, numeric=989)
    with pytest.raises(ImproperlyConfigured, match="numeric code 840"):
        register_currency("ABC", minor_units=2, numeric=840)
    with pytest.raises(ImproperlyConfigured, match="numeric code 989"):
        register_currency("ABD", minor_units=2, numeric=989)
    assert currency("PTS").numeric == 989
    with pytest.raises(UnknownCurrency):
        currency("ABC")


def test_registered_currencies_are_dropped_when_the_block_ends() -> None:
    """currencies_kept() puts the registry back, as the test plugin does."""
    with currencies_kept():
        register_currency("TMP", minor_units=1)
        assert currency("TMP").minor_units == 1

    with pytest.raises(UnknownCurrency):
        currency("TMP")


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (("usd", None, 2), "capital letters"),
        ((840, None, 2), "capital letters"),
        (("AB", None, 2), "capital letters"),
        (("ABCDEFGHI", None, 2), "capital letters"),
        (("1BC", None, 2), "capital letters"),
        (("ABC", 0, 2), "numeric code"),
        (("ABC", 1000, 2), "numeric code"),
        (("ABC", True, 2), "numeric code"),
        (("ABC", "840", 2), "numeric code"),
        (("ABC", None, -1), "minor units"),
        (("ABC", None, 19), "minor units"),
        (("ABC", None, True), "minor units"),
        (("ABC", None, 2.0), "minor units"),
    ],
)
def test_currencies_are_refused_out_of_bounds(
    arguments: tuple[object, object, object], message: str
) -> None:
    """Codes of three to eight, numeric codes of 1 to 999, 0 to 18 places."""
    with pytest.raises(ValueError, match=message):
        Currency(*arguments)  # type: ignore[arg-type]


def test_currencies_are_values() -> None:
    """Equal by code, numeric code, and units; immutable; copied and pickled as is."""
    kwd = currency("KWD")
    other = Currency("KWD", 414, 3, "another name")

    assert other == kwd
    assert hash(other) == hash(kwd)
    assert Currency("KWD", 414, 2) != kwd
    assert Currency("KWD", None, 3) != kwd
    assert Currency("KWX", 414, 3) != kwd
    assert kwd != "KWD"
    assert (repr(kwd), str(kwd)) == ("Currency('KWD', 414, 3)", "KWD")
    with pytest.raises(AttributeError, match="immutable"):
        kwd.minor_units = 2
    assert pickle.loads(pickle.dumps(kwd)) == kwd  # noqa: S301 - our own bytes.
    assert copy.deepcopy(kwd).name == kwd.name == "Kuwaiti Dinar"


# Amounts.


@pytest.mark.parametrize(
    ("amount", "code", "held"),
    [
        ("10.5", "USD", "10.50"),
        (Decimal("10.500"), "USD", "10.50"),
        (10, "USD", "10.00"),
        ("-0.00", "USD", "0.00"),
        (Decimal("-0"), "KWD", "0.000"),
        ("1000", "JPY", "1000"),
        (Decimal("1E+3"), "JPY", "1000"),
        ("1.234", "KWD", "1.234"),
        ("0.0001", "CLF", "0.0001"),
        ("9" * MAX_DIGITS, "JPY", "9" * MAX_DIGITS),
    ],
)
def test_amounts_hold_exactly_their_currencys_places(
    amount: Decimal | int | str, code: str, held: str
) -> None:
    """Text, int, or Decimal; zero has no sign; places are the currency's."""
    money = Money(amount, code)

    assert str(money.amount) == held
    assert money.currency is currency(code)
    assert Money.of(amount, currency(code)) == money


@pytest.mark.parametrize(
    ("amount", "code"),
    [("10.555", "USD"), ("0.5", "JPY"), ("1.2345", "KWD"), ("0.00001", "CLF")],
)
def test_more_places_than_the_currency_are_refused_not_rounded(
    amount: str, code: str
) -> None:
    """InexactAmount, unless a rounding is named."""
    with pytest.raises(InexactAmount, match=f"{code} has") as caught:
        Money.of(amount, code)

    assert caught.value.code == "inexact_amount"
    assert isinstance(caught.value, MoneyError)


@pytest.mark.parametrize(
    ("rounding", "amounts"),
    [
        (Rounding.HALF_EVEN, ["2.68", "2.66", "-2.68", "0.00"]),
        (Rounding.HALF_UP, ["2.68", "2.67", "-2.68", "0.01"]),
        (Rounding.DOWN, ["2.67", "2.66", "-2.67", "0.00"]),
        (Rounding.UP, ["2.68", "2.67", "-2.68", "0.01"]),
        (Rounding.CEILING, ["2.68", "2.67", "-2.67", "0.01"]),
        (Rounding.FLOOR, ["2.67", "2.66", "-2.68", "0.00"]),
    ],
)
def test_a_named_rounding_rounds(rounding: Rounding, amounts: list[str]) -> None:
    """Each policy, on halves, on both signs, and on a fraction of a cent."""
    given = ["2.675", "2.665", "-2.675", "0.005"]

    rounded = [Money.of(Decimal(each), "USD", rounding=rounding) for each in given]

    assert [str(each.amount) for each in rounded] == amounts


def test_roundings_are_the_decimal_modules_constants() -> None:
    """Rounding.HALF_EVEN can be given wherever decimal takes one."""
    assert Rounding.HALF_EVEN.value == ROUND_HALF_EVEN
    assert Decimal("2.5").quantize(Decimal(1), rounding=Rounding.HALF_EVEN) == 2
    assert Money.of("2.675", "USD", rounding=ROUND_HALF_EVEN).amount == Decimal(  # type: ignore[arg-type]
        "2.68"
    )


@pytest.mark.parametrize("amount", [10.5, 0.0, float("nan")])
def test_floats_are_refused_everywhere(amount: float) -> None:
    """Amounts, factors, ratios, and minor units: a float is inexact."""
    with pytest.raises(TypeError, match="float"):
        Money(amount, "USD")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="float"):
        usd("1").multiply(amount)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="float"):
        usd("1").allocate([1, amount])  # type: ignore[list-item]
    with pytest.raises(TypeError, match="whole number"):
        Money.from_minor(amount, "USD")  # type: ignore[arg-type]


@pytest.mark.parametrize("amount", [True, None, b"10", [10]])
def test_other_types_are_refused(amount: object) -> None:
    """A bool is no amount, and neither is anything but a number or text."""
    with pytest.raises(TypeError, match="not"):
        Money(amount, "USD")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="currency is a Currency or its code"):
        Money("1", 840)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "amount",
    ["1e3", " 10", "10 ", "1,000", "+1", "1.", ".5", "", "NaN", "--1", "1" * 200],
)
def test_text_is_plain_digits(amount: str) -> None:
    """No exponent, spaces, separators, signs but minus, or endless digits."""
    with pytest.raises(ValueError, match="digits with an optional point"):
        Money(amount, "USD")


@pytest.mark.parametrize(
    "amount",
    [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity"), Decimal("sNaN")],
)
def test_amounts_are_finite(amount: Decimal) -> None:
    """NaN and the infinities are no amounts."""
    with pytest.raises(ValueError, match="finite"):
        Money(amount, "USD")


def test_amounts_have_at_most_max_digits_before_the_point() -> None:
    """Past NUMERIC(38) is no amount, nor a cost to compute; zero of any exponent is."""
    with pytest.raises(ValueError, match="38 digits"):
        Money(10**MAX_DIGITS, "JPY")
    with pytest.raises(ValueError, match="38 digits"):
        Money(Decimal("1E+999999999"), "USD")
    with pytest.raises(ValueError, match="38 digits"):
        Money.from_minor(10 ** (MAX_DIGITS + 2), "USD")
    assert Money(Decimal("0E-50"), "USD") == Money.zero("USD")
    assert Money(-(10**MAX_DIGITS - 1), "JPY").minor == -(10**MAX_DIGITS - 1)


def test_minor_units() -> None:
    """from_minor and minor convert at the currency's places."""
    assert Money.from_minor(1050, "USD") == usd("10.50")
    assert Money.from_minor(-5, "KWD").amount == Decimal("-0.005")
    assert Money.from_minor(7, "JPY").amount == Decimal(7)
    assert usd("10.50").minor == 1050
    assert Money.of("-1.0001", "CLF").minor == -10001
    assert Money.zero("KWD").amount == Decimal("0.000")
    with pytest.raises(TypeError):
        Money.from_minor(True, "USD")


# Arithmetic.


def test_arithmetic_takes_one_currency() -> None:
    """+, -, unary -, abs: exact, in the currency of both."""
    a, b = usd("10.25"), usd("0.75")

    assert a + b == usd("11.00")
    assert a - b == usd("9.50")
    assert b - a == usd("-9.50")
    assert -a == usd("-10.25")
    assert +a is a
    assert abs(usd("-3.10")) == usd("3.10")
    assert -Money.zero("USD") == Money.zero("USD")
    assert str((-Money.zero("USD")).amount) == "0.00"
    assert Money.of("99999999999999999999999999999999999999", "JPY") + Money.of(
        "-1", "JPY"
    ) == Money.of("99999999999999999999999999999999999998", "JPY")


@pytest.mark.parametrize(
    "operation",
    [
        lambda a, b: a + b,
        lambda a, b: a - b,
        lambda a, b: a < b,
        lambda a, b: a <= b,
        lambda a, b: a > b,
        lambda a, b: a >= b,
        lambda a, b: Money.sum([a, b], a.currency),
    ],
)
def test_two_currencies_are_refused(operation: object) -> None:
    """CurrencyMismatch, a ValueError, never a silent result."""
    with pytest.raises(CurrencyMismatch, match="EUR and USD") as caught:
        operation(usd("1"), Money.of("1", "EUR"))  # type: ignore[operator]

    assert caught.value.code == "currency_mismatch"
    assert isinstance(caught.value, ValueError)


def test_equality_across_currencies_is_false() -> None:
    """Different currencies are different money; equal places are not needed."""
    assert usd("1") != Money.of("1", "EUR")
    assert usd("1") == Money.of(Decimal("1.000"), "USD")
    assert hash(usd("1")) == hash(Money.of(1, "USD"))
    assert usd("1") != Decimal("1.00")
    assert len({usd("1"), usd("1.0"), Money.of("1", "EUR")}) == 2


def test_comparisons() -> None:
    """Ordered within a currency; with anything else a TypeError."""
    assert usd("1") < usd("2") <= usd("2") < usd("10")
    assert usd("10") > usd("2") >= usd("2")
    assert not usd("2") < usd("2")
    assert not usd("1") >= usd("2")
    assert sorted([usd("3"), usd("-1"), usd("2")]) == [usd("-1"), usd("2"), usd("3")]
    with pytest.raises(TypeError, match="not Decimal"):
        assert usd("1") < Decimal(2)


def test_arithmetic_with_other_types_is_a_type_error() -> None:
    """Money adds money; numbers are scaled with multiply."""
    with pytest.raises(TypeError):
        usd("1") + 1
    with pytest.raises(TypeError):
        usd("1") - Decimal(1)
    with pytest.raises(TypeError):
        1 + usd("1")
    with pytest.raises(TypeError):
        usd("1") * 2  # type: ignore[operator]


def test_sums() -> None:
    """sum() of amounts works; Money.sum also of none, and checks the currency."""
    items = [usd("1.10"), usd("2.20"), usd("3.30")]

    assert sum(items) == usd("6.60")
    assert Money.sum(items, "USD") == usd("6.60")
    assert Money.sum([], "KWD") == Money.zero("KWD")
    assert Money.sum(iter(items), USD) == usd("6.60")
    with pytest.raises(CurrencyMismatch):
        Money.sum(items, "EUR")


def test_multiply_is_exact_or_rounds_as_named() -> None:
    """A product that fits is kept; one that does not needs a rounding."""
    price = usd("10.00")

    assert price.multiply(3) == usd("30.00")
    assert price.multiply("0.5") == usd("5.00")
    assert price.multiply(Decimal("-0.25")) == usd("-2.50")
    assert price.multiply("0.155") == usd("1.55")
    assert usd("1.00").multiply("0.155", rounding=Rounding.HALF_EVEN) == usd("0.16")
    assert usd("1.00").multiply("0.145", Rounding.HALF_EVEN) == usd("0.14")
    assert price.multiply("0.1551", Rounding.UP) == usd("1.56")
    assert Money.of("0.333", "KWD").multiply(3) == Money.of("0.999", "KWD")
    with pytest.raises(InexactAmount):
        price.multiply("0.1551")
    with pytest.raises(TypeError, match="factor"):
        price.multiply(True)
    with pytest.raises(ValueError, match="finite"):
        price.multiply(Decimal("Infinity"))
    big = Money.of("1" * 30, "JPY").multiply(Decimal("1" * 40) / Decimal("1" * 40))
    assert big == Money.of("1" * 30, "JPY")


def test_immutable() -> None:
    """Money cannot change: a new amount is a new object."""
    price = usd("1")

    with pytest.raises(AttributeError, match="immutable"):
        price.amount = Decimal(2)
    with pytest.raises(AttributeError, match="immutable"):
        del price.currency
    assert copy.copy(price) == price
    assert copy.deepcopy(price) == price
    assert pickle.loads(pickle.dumps(price)) == price  # noqa: S301 - our own bytes.
    assert price.is_zero() is False
    assert Money.zero("USD").is_zero() is True


def test_text_forms() -> None:
    """Repr and str in fixed point, with the currency's places."""
    assert repr(Money.of(Decimal("1E+3"), "JPY")) == "Money('1000', 'JPY')"
    assert str(Money.of("-1.5", "KWD")) == "-1.500 KWD"
    assert usd("2").__composite_values__() == (Decimal("2.00"), "USD")


# Allocation.


@pytest.mark.parametrize(
    ("amount", "ratios", "parts"),
    [
        ("100.00", [1, 1, 1], ["33.34", "33.33", "33.33"]),
        ("0.05", [1, 1], ["0.03", "0.02"]),
        ("0.05", [3, 7], ["0.02", "0.03"]),
        ("-100.00", [1, 1, 1], ["-33.34", "-33.33", "-33.33"]),
        (
            "100.00",
            [Decimal("0.7"), Decimal("0.2"), Decimal("0.1")],
            ["70.00", "20.00", "10.00"],
        ),
        ("10.00", [1, 0, 1], ["5.00", "0.00", "5.00"]),
        ("0.01", [0, 1, 1], ["0.00", "0.01", "0.00"]),
        ("0.00", [1, 2], ["0.00", "0.00"]),
        ("1.00", [1], ["1.00"]),
        ("0.07", [1, 1, 1, 1, 1, 1, 1, 1, 1, 1], ["0.01"] * 7 + ["0.00"] * 3),
        (
            "100.00",
            [Decimal("33.333"), Decimal("33.333"), Decimal("33.334")],
            ["33.33", "33.33", "33.34"],
        ),
    ],
)
def test_allocation_by_largest_remainder(
    amount: str, ratios: list[int | Decimal], parts: list[str]
) -> None:
    """Shares rounded down; the leftover units to the largest remainders, in order."""
    allocated = usd(amount).allocate(ratios)

    assert [str(each.amount) for each in allocated] == parts
    assert Money.sum(allocated, "USD") == usd(amount)


def test_allocation_in_other_currencies() -> None:
    """Minor units are the currency's: yen, and fils of a dinar."""
    assert Money.of("100", "JPY").allocate([1, 1, 1]) == [
        Money.of(34, "JPY"),
        Money.of(33, "JPY"),
        Money.of(33, "JPY"),
    ]
    assert [each.minor for each in Money.of("1.000", "KWD").allocate([1, 2])] == [
        333,
        667,
    ]


@pytest.mark.parametrize(
    ("ratios", "error", "message"),
    [
        ([], ValueError, "at least one ratio"),
        ([0, 0], ValueError, "more than zero"),
        ([1, -1], ValueError, "negative"),
        ([True], TypeError, "ratio"),
        (["1"], TypeError, "not text"),
        ([Decimal("NaN")], ValueError, "finite"),
        ([Decimal("1E+38")], ValueError, "38 digits"),
        ([Decimal("1E-39")], ValueError, "38 digits"),
    ],
)
def test_allocation_refuses_ratios_that_are_no_proportions(
    ratios: list[object], error: type[Exception], message: str
) -> None:
    """At least one positive ratio, none negative, none a float or text."""
    with pytest.raises(error, match=message):
        usd("1").allocate(ratios)  # type: ignore[arg-type]


def test_allocation_takes_any_iterable_of_ratios() -> None:
    """A generator works, as a list does."""
    assert usd("1").allocate(each for each in (1, 1)) == [usd("0.50"), usd("0.50")]
    assert usd("1").allocate([Decimal("1E-38"), Decimal("1E-38")]) == [
        usd("0.50"),
        usd("0.50"),
    ]


# Properties.

amounts = st.integers(min_value=-(10**20), max_value=10**20)
known = st.sampled_from(ISO)
ratio_lists = st.lists(
    st.one_of(
        st.integers(min_value=0, max_value=10**6),
        st.decimals(min_value=0, max_value=10**6, places=3, allow_nan=False),
    ),
    min_size=1,
    max_size=12,
).filter(any)


@given(minor=amounts, unit=known, ratios=ratio_lists)
def test_allocation_never_loses_or_creates_a_minor_unit(
    minor: int, unit: Currency, ratios: list[int | Decimal]
) -> None:
    """The parts sum to the whole, each within one unit of its exact share."""
    whole = Money.from_minor(minor, unit)

    parts = whole.allocate(ratios)

    assert len(parts) == len(ratios)
    assert sum(part.minor for part in parts) == minor
    assert Money.sum(parts, unit) == whole
    total = sum(Decimal(each) for each in ratios)
    for part, ratio in zip(parts, ratios, strict=True):
        exact = Decimal(minor) * Decimal(ratio) / total
        assert abs(part.minor - exact) < 1
        assert part.currency is unit
        if ratio == 0:
            assert part.is_zero()


@given(a=amounts, b=amounts, unit=known)
def test_addition_is_exact(a: int, b: int, unit: Currency) -> None:
    """Sums and differences are those of the minor units, never rounded."""
    left, right = Money.from_minor(a, unit), Money.from_minor(b, unit)

    assert (left + right).minor == a + b
    assert (left - right).minor == a - b
    assert (left + right) - right == left
    assert sum([left, right]) == Money.sum([left, right], unit) == left + right
    assert (left < right) is (a < b)


@given(minor=amounts, unit=known)
def test_minor_units_round_trip_for_every_currency(minor: int, unit: Currency) -> None:
    """from_minor and minor are inverse, in every ISO 4217 currency."""
    money = Money.from_minor(minor, unit)

    assert money.minor == minor
    assert Money(money.amount, unit.code) == money
    assert -int(money.amount.as_tuple().exponent) == unit.minor_units
    assert Money.of(str(money.amount), unit) == money


def test_every_currency_round_trips_its_extremes() -> None:
    """The edges, in each currency: 0, 1 and -1 minor unit, 38 digits."""
    for unit in ISO:
        for minor in (0, 1, -1, 10 ** (MAX_DIGITS + unit.minor_units) - 1):
            assert Money.from_minor(minor, unit).minor == minor


# Pydantic.


class Priced(BaseModel):
    """A model with money and a currency."""

    total: Money
    fee: Money | None = None
    unit: Currency = USD


def test_money_validates_from_and_serialises_to_amount_text_and_currency() -> None:
    """{"amount": "10.50", "currency": "USD"} in JSON; Money in Python."""
    priced = Priced.model_validate_json(
        '{"total": {"amount": "10.5", "currency": "KWD"}, "unit": "EUR"}'
    )

    assert priced.total == Money.of("10.500", "KWD")
    assert priced.unit is currency("EUR")
    assert priced.model_dump_json() == (
        '{"total":{"amount":"10.500","currency":"KWD"},"fee":null,"unit":"EUR"}'
    )
    assert priced.model_dump()["total"] == priced.total
    assert priced.model_dump()["unit"] is currency("EUR")
    assert Priced(total=usd("1"), fee={"amount": 2, "currency": "USD"}).fee == usd("2")
    assert Priced.model_validate({"total": usd("3")}).total == usd("3")
    assert TypeAdapter(Money).dump_python(usd("1"), mode="json") == {
        "amount": "1.00",
        "currency": "USD",
    }


@pytest.mark.parametrize(
    ("total", "kind", "message"),
    [
        ({"amount": "10.555", "currency": "USD"}, "inexact_amount", "decimal places"),
        ({"amount": "1", "currency": "XYZ"}, "unknown_currency", "not one this"),
        ({"amount": 10.5, "currency": "USD"}, "money_amount", "as text"),
        ({"amount": "1e3", "currency": "USD"}, "money_amount", "as text"),
        ({"amount": "1", "currency": 840}, "money_amount", "as text"),
        ({"amount": "1"}, "money_type", "an amount and a currency"),
        ({"amount": "1", "currency": "USD", "x": 1}, "money_type", "nothing else"),
        ("10.00 USD", "money_type", "an amount and a currency"),
    ],
)
def test_invalid_money_is_a_validation_error(
    total: object, kind: str, message: str
) -> None:
    """Each with its own type and a message that quotes nothing."""
    with pytest.raises(ValidationError) as caught:
        Priced.model_validate({"total": total})

    (error,) = caught.value.errors()
    assert error["type"] == kind
    assert message in error["msg"]


@pytest.mark.parametrize("unit", ["usd", 840, None])
def test_an_unknown_currency_is_a_validation_error(unit: object) -> None:
    """A currency field takes a known code or a Currency."""
    with pytest.raises(ValidationError) as caught:
        Priced.model_validate({"total": usd("1"), "unit": unit})

    assert caught.value.errors()[0]["type"] == "unknown_currency"


def test_the_json_schema_is_one_money_definition() -> None:
    """An object of amount text and currency code, referred to by each field."""
    schema = Priced.model_json_schema()

    money = schema["$defs"]["Money"]
    assert money["required"] == ["amount", "currency"]
    assert money["properties"]["amount"]["type"] == "string"
    assert money["properties"]["currency"]["pattern"] == "^[A-Z][A-Z0-9]{2,7}$"
    assert money["additionalProperties"] is False
    assert schema["properties"]["total"] == {"$ref": "#/$defs/Money"}
    assert schema["properties"]["unit"]["type"] == "string"


def test_json_values_of_money_and_currencies() -> None:
    """Audit details, task arguments, and exports write money as its schema does."""
    assert jsonable({"total": Money.of(Decimal("1E+3"), "JPY"), "unit": USD}) == {
        "total": {"amount": "1000", "currency": "JPY"},
        "unit": "USD",
    }
    with pytest.raises(Exception, match="Unable to serialize unknown type"):
        jsonable(object())


# Factories.


class Bill(ValueObject):
    """A value object of money and a currency, as a project's would be."""

    total: Money
    tip: Money


class BillFactory(Factory[Bill]):
    """Bills whose tip is in the total's currency."""

    class Meta:
        """Make bills."""

        model = Bill

    total = SubFactory(MoneyFactory, currency="EUR")
    tip = SubFactory(MoneyFactory, currency=SelfAttribute("..total.currency"), amount=0)


def test_money_factory_makes_whole_amounts_in_the_currency_given() -> None:
    """1, 2, 3 ... of USD by default; any currency's places fit a whole amount."""
    MoneyFactory.reset_sequence()

    assert MoneyFactory.build() == usd("1")
    assert MoneyFactory.build(currency="KWD") == Money.of("2", "KWD")
    assert MoneyFactory.build(currency=currency("JPY"), amount="7") == Money.of(
        7, "JPY"
    )
    assert MoneyFactory.build_batch(2, amount=Decimal("0.5")) == [usd("0.5")] * 2
    assert MoneyFactory.stub(currency="EUR").currency == "EUR"


def test_a_sub_factory_of_money_takes_its_parts() -> None:
    """``total__currency=`` and ``total__amount=`` reach the money."""
    bill = BillFactory.build(total__currency="KWD", total__amount="1.5")

    assert bill.total == Money.of("1.500", "KWD")
    assert bill.tip == Money.zero("KWD")
    assert BillFactory.build().total.currency is currency("EUR")
    with pytest.raises(InexactAmount):
        BillFactory.build(total__amount="1.005")


# Events and task arguments.


class Paid(DomainEvent):
    """An event with an amount."""

    amount: Money
    fee: Money | None = None


def test_events_and_task_arguments_carry_money_as_its_json() -> None:
    """The outbox writes the amount as text and reads the same money back."""
    event = Paid(amount=Money.of(Decimal("1E+3"), "JPY"))

    payload = dump_event(event)

    assert payload["amount"] == {"amount": "1000", "currency": "JPY"}
    assert load_event(Paid, payload) == event
    assert dump_arguments({"amount": usd("2.5")}) == {
        "amount": {"amount": "2.50", "currency": "USD"}
    }


# What each refusal says.


def test_currencies_take_their_bounds_and_default_to_no_name() -> None:
    """1 and 999, 0 and 18 are in bounds; the name is empty unless given."""
    assert Currency("ABC", 1, 0).name == ""
    assert Currency("ABCDEFGH", 999, 18).minor_units == 18
    assert hash(currency("USD")) != hash(currency("EUR"))
    assert hash(usd("1")) != hash(usd("2"))


@pytest.mark.parametrize(
    ("make", "error", "message"),
    [
        (
            lambda: Currency("usd", None, 2),
            ValueError,
            (
                "A currency code is three to eight capital letters or digits, starting "
                "with a letter (ISO 4217's are three letters)."
            ),
        ),
        (
            lambda: Currency("ABC", 0, 2),
            ValueError,
            "ABC's numeric code is from 1 to 999, or None, not 0.",
        ),
        (
            lambda: Currency("ABC", None, 19),
            ValueError,
            "ABC's minor units are from 0 to 18, not 19.",
        ),
        (
            lambda: register_currency("USD", minor_units=2),
            ImproperlyConfigured,
            (
                "USD is an ISO 4217 currency, known already; register only currencies "
                "ISO 4217 does not list."
            ),
        ),
        (
            lambda: register_currency("XYZ", minor_units=2, numeric=840),
            ImproperlyConfigured,
            "The numeric code 840 is another currency's; give XYZ its own, or none.",
        ),
        (
            lambda: currency("EUROS"),
            UnknownCurrency,
            (
                "No currency has 'EUROS': use an ISO 4217 code, in capitals, or "
                "register the currency with register_currency."
            ),
        ),
        (
            lambda: Money.of("1.005", "USD"),
            InexactAmount,
            (
                "USD has 2 decimal places; an amount with more is refused rather than "
                "rounded: name a Rounding."
            ),
        ),
        (
            lambda: usd("1") + Money.of("1", "EUR"),
            CurrencyMismatch,
            (
                "Cannot add EUR and USD amounts: convert one first, at a rate the "
                "project trusts."
            ),
        ),
        (
            lambda: Money.sum([Money.of("1", "EUR")], "USD"),
            CurrencyMismatch,
            "Cannot add EUR and USD amounts",
        ),
        (lambda: usd("1") - Money.of("1", "EUR"), CurrencyMismatch, "Cannot subtract"),
        (lambda: usd("1") < Money.of("1", "EUR"), CurrencyMismatch, "Cannot compare"),
        (
            lambda: Money.from_minor(1.5, "USD"),  # type: ignore[arg-type]
            TypeError,
            "Money.from_minor takes a whole number of minor units, not float.",
        ),
        (
            lambda: usd("1").multiply([2]),  # type: ignore[arg-type]
            TypeError,
            "A factor is a Decimal, an int, or text, not list.",
        ),
        (
            lambda: usd("1").multiply("2x"),
            ValueError,
            (
                "A factor as text is digits with an optional point and sign, such as "
                "10.50, with no exponent."
            ),
        ),
        (
            lambda: usd("1").allocate([]),
            ValueError,
            "Allocate to at least one ratio.",
        ),
        (
            lambda: usd("1").allocate(["1"]),  # type: ignore[list-item]
            TypeError,
            "A ratio is a Decimal or an int, not text.",
        ),
        (
            lambda: usd("1").allocate([True]),
            TypeError,
            "A ratio is a Decimal, an int, or text, not bool.",
        ),
        (
            lambda: usd("1").allocate([-1]),
            ValueError,
            "Ratios cannot be negative.",
        ),
        (
            lambda: usd("1").allocate([0]),
            ValueError,
            "At least one ratio must be more than zero.",
        ),
        (
            lambda: usd("1").allocate([Decimal("1E+40")]),
            ValueError,
            "A ratio has at most 38 digits on either side of its point.",
        ),
    ],
)
def test_each_refusal_says_why(
    make: object, error: type[Exception], message: str
) -> None:
    """The messages, word for word, for developers."""
    with pytest.raises(error) as caught:
        make()  # type: ignore[operator]

    assert str(caught.value).startswith(message)


def test_comparisons_at_their_edges() -> None:
    """Equal amounts are neither more nor less; each operator holds where it should."""
    one, two = usd("1"), usd("2")

    assert (two <= one, two > two, one >= two, two < two) == (False,) * 4
    assert (one <= one, two >= two, one < two, two > one) == (True,) * 4


def test_ratios_of_any_places_and_exponents_split_alike() -> None:
    """Integers, decimals, and exponents: only the proportions count."""
    whole = usd("100")
    expected = [usd("25"), usd("75")]

    assert whole.allocate([1, 3]) == expected
    assert whole.allocate([Decimal("0.25"), Decimal("0.75")]) == expected
    assert whole.allocate([Decimal("1E+2"), Decimal("3E+2")]) == expected
    assert whole.allocate([Decimal("5E+1"), 150]) == expected
    assert whole.allocate([Decimal("1" * 37), Decimal("3" * 37)]) == expected


def test_the_validation_messages_are_the_catalogs() -> None:
    """Fixed texts, so they translate."""
    messages = {}
    for total in ({"amount": "1"}, {"amount": 1.5, "currency": "USD"}):
        with pytest.raises(ValidationError) as caught:
            Priced.model_validate({"total": total})
        (error,) = caught.value.errors()
        messages[error["type"]] = error["msg"]

    assert messages == {
        "money_type": (
            "Give money as an object of an amount and a currency, and nothing else."
        ),
        "money_amount": (
            "Give the amount as text, such as 10.50, with at most 38 digits before "
            "the point."
        ),
    }


def test_the_json_schema_word_for_word() -> None:
    """What OpenAPI shows of money."""
    assert Priced.model_json_schema()["$defs"]["Money"] == {
        "type": "object",
        "title": "Money",
        "description": (
            "An amount of money: the amount as text, with its currency's decimal "
            "places, and the ISO 4217 code of the currency."
        ),
        "properties": {
            "amount": {
                "type": "string",
                "pattern": "^-?[0-9]+(\\.[0-9]+)?$",
                "examples": ["10.50"],
            },
            "currency": {
                "type": "string",
                "pattern": "^[A-Z][A-Z0-9]{2,7}$",
                "description": "An ISO 4217 currency code.",
                "examples": ["USD"],
            },
        },
        "required": ["amount", "currency"],
        "additionalProperties": False,
    }
