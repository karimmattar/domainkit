# Money: amounts, currencies, rounding, allocation

`domainkit.money` holds an amount of money as a value: a `Decimal` with
exactly its currency's places, and the currency. Nothing is rounded unless
the code names how, no float is taken anywhere, and amounts of two
currencies never meet by accident. Tables keep the amount in an exact
column and the currency's code beside it; schemas, filters, cursors,
fixtures, factories, and the generators know it
([ADR 0115](adr/0115-store-money-as-an-exact-amount-and-a-currency-code.md)).

```python
from domainkit.money import Money, Rounding

price = Money.of("10.5", "USD")      # Money('10.50', 'USD')
price + Money.of("0.25", "USD")      # Money('10.75', 'USD')
price.multiply("0.15", rounding=Rounding.HALF_UP)  # Money('1.58', 'USD')
Money.of("100", "USD").allocate([1, 1, 1])  # 33.34, 33.33, 33.33
```

Converting between currencies needs a rate, and a rate needs a source the
project trusts, at a time it chose: domainkit has neither, and converts
nothing. Convert in the project, and round as its rules say:
`Money.of(euros.amount * rate, "USD", rounding=Rounding.HALF_EVEN)`.

## Currencies

`currency("KWD")` is `Currency("KWD", 414, 3)`: the ISO 4217 code, its
numeric code, and its minor units, the places of its amounts (0 for JPY, 2
for USD and EGP, 3 for KWD, BHD, OMR, JOD, TND, LYD, and IQD, 4 for CLF and
UYW). The known currencies are the 165 of ISO 4217's list of active
currencies that have minor units, as SIX, its maintenance agency,
published it on 2026-09-17 (`ISO_4217_PUBLISHED`), kept as data in
`domainkit/money/iso4217.csv`. Codes whose minor units ISO 4217 gives as
"N.A." (gold, silver, the SDR, `XTS`, `XXX`) are not among them. A code is
upper case; another code is `UnknownCurrency`. `currencies()` lists them
all.

A project's own currencies, a token or a unit of its own, are registered
before any amount uses them, in a domain's `ready()`:

```python
from domainkit.money import register_currency

register_currency("USDC", minor_units=6, name="USD Coin")
register_currency("PTS", minor_units=0)  # loyalty points
```

A code is three to eight capitals or digits, starting with a letter, and
has 0 to 18 minor units. ISO 4217's codes cannot be registered, nor a code
registered again with other units (`ImproperlyConfigured`): the stored
amounts would change their meaning. Registering the same currency again
returns it. The pytest plugin keeps each test's registrations to itself
(`currencies_kept()`).

## Amounts

`Money(amount, currency)` and `Money.of(amount, currency)` take the amount
as a `Decimal`, an `int`, or text (`"10.50"`: digits, a point, a sign; no
exponent), and the currency as a `Currency` or its code. A float is a
`TypeError`. The amount is kept with the currency's places:
`Money.of("10.5", "USD").amount` is `Decimal("10.50")`, and zero has no
sign. More places than the currency has are refused, `InexactAmount`,
unless a rounding is named: `Money.of("2.675", "USD",
rounding=Rounding.HALF_UP)` is 2.68. An amount has at most `MAX_DIGITS`
(38) digits before its point.

- `Money.zero("USD")`, `Money.from_minor(1050, "USD")` (10.50), and
  `price.minor` (1050) count in minor units.
- `+`, `-`, unary `-`, `abs()`, and `<`, `<=`, `>`, `>=` take amounts of
  one currency, and are exact at any size; two currencies raise
  `CurrencyMismatch`. `==` is false between currencies, never an error.
- `sum(amounts)` works when there is at least one; `Money.sum(amounts,
  "USD")` is also zero of none, and checks each one's currency.
- Money is immutable and hashable; `str()` is `10.50 USD`.

### Scaling and rounding

`price.multiply(factor)` is the only way to scale an amount. The product is
exact; one with more places than the currency is refused unless
`rounding` says how to round it:

| `Rounding` | 2.675 | 2.665 | -2.675 | 0.005 |
| --- | --- | --- | --- | --- |
| `HALF_EVEN` (banker's) | 2.68 | 2.66 | -2.68 | 0.00 |
| `HALF_UP` (away from zero at a half) | 2.68 | 2.67 | -2.68 | 0.01 |
| `DOWN` (toward zero) | 2.67 | 2.66 | -2.67 | 0.00 |
| `UP` (away from zero) | 2.68 | 2.67 | -2.68 | 0.01 |
| `CEILING` | 2.68 | 2.67 | -2.67 | 0.01 |
| `FLOOR` | 2.67 | 2.66 | -2.68 | 0.00 |

Each is the `decimal` module's constant of the same name
(`Rounding.HALF_EVEN == decimal.ROUND_HALF_EVEN`).

### Allocation

`amount.allocate(ratios)` splits an amount in proportion, and the parts
always sum to it: no minor unit is lost or made.

```python
Money.of("100", "USD").allocate([1, 1, 1])  # 33.34, 33.33, 33.33
Money.of("0.05", "USD").allocate([3, 7])     # 0.02, 0.03
Money.of("100", "USD").allocate([Decimal("0.7"), Decimal("0.3")])  # 70.00, 30.00
```

Each part gets its share of the minor units, rounded toward zero; the
units left over go one each to the parts whose shares lost most (the
largest remainders), the first of equal ones first. A part of ratio 0 gets
nothing; a negative amount is split as its opposite. Ratios are `int` or
`Decimal`, none negative, at least one above zero.

## In the domain

A domain layer may import `domainkit.money` (the layer rules allow it), and
a field holds `Money` as any value:

```python
from typing import Annotated

from domainkit.ddd import AggregateRoot, Digits
from domainkit.money import Money


class Wallet(AggregateRoot[WalletId]):
    balance: Annotated[Money, Digits(12, 2)] = Money.zero("USD")
    limit: Money | None = None

    def pay(self, *, to: WalletId, amount: Money) -> None:
        if amount > self.balance:  # Another currency: CurrencyMismatch.
            raise InsufficientFunds(f"Wallet {self.id} holds less than {amount}.")
        self.balance -= amount
        self.record(WalletPaid(wallet_id=self.id, to=to, amount=amount))
```

Events, commands, and task arguments carry money as any pydantic field;
the outbox, audit details, and exports write it as
`{"amount": "10.50", "currency": "USD"}`.

## In tables

`table_for` stores a money field in two columns, so the database compares,
orders, and sums the amount, and the code reads as one:

| Field | Columns |
| --- | --- |
| `balance: Money` | `balance` `ExactDecimal(18, 4)` and `balance_currency` (`CurrencyCode`, eight characters) |
| `balance: Annotated[Money, Digits(12, 2)]` | `balance` `ExactDecimal(12, 2)`, and the code |
| `limit: Money \| None` | both nullable, and a check that both are missing or neither is |

The amount column's scale must hold the places of every currency it
stores: the default 4 holds every ISO 4217 currency, and 14 digits before
the point; `Digits(12, 2)` holds dollars and euros, not dinars of three
places (a write schema refuses them, `money_digits`). On SQLite the amount
is whole minor units of the column's scale, so it stays exact
([exact amounts](databases.md#exact-amounts)). `map_imperatively` maps the
pair to the one attribute, and `makemigrations` writes plain `NUMERIC` and
`VARCHAR(8)` columns. A hand-written table takes the same columns from
`MoneyColumns`:

```python
from domainkit.db import MoneyColumns

wallets = Table(
    "wallets_wallets",
    metadata,
    Column("id", Uuid, primary_key=True),
    *MoneyColumns("balance", 12, 2),
    *MoneyColumns("limit", nullable=True),
    Column("version", Integer, nullable=False),
    Index("ix_wallets_wallets_balance", "balance_currency", "balance"),
)
```

An index on `(<name>_currency, <name>)` serves a filter by currency and
amount, and its ordering, on both databases. A field hinted `Currency`
alone is one `CurrencyCode` column. A stored code must name a known
currency when it is read back: register a project's currencies before
reading their rows.

Totals per currency are one query:

```python
from sqlalchemy import func, select

totals = select(wallets.c.balance_currency, func.sum(wallets.c.balance)).group_by(
    wallets.c.balance_currency
)
```

Each total is an exact `Decimal` with the column's places, on SQLite too.

### In queries

Money compares only with money of its currency, in memory and in SQL
alike:

```python
F("balance") >= Money.of("10", "USD")  # balances in USD of at least 10.00
F("balance").range(Money.of("0", "EUR"), Money.of("5", "EUR"))
F("balance.currency") == "USD"            # its parts are fields too
F("balance.amount") > Decimal("9.995")    # of any places, compared exactly
```

A condition on money is a condition on its currency and its amount, so
`~(F("balance") > Money.of("0", "USD"))` holds for every balance in another
currency. A range of two currencies is `InvalidQuery`. Money orders by
currency code, then amount (`ordering=["-balance"]`), and keyset pages
follow that order. `update_rows(criteria, balance=Money.of("1", "EUR"))`
sets both columns.

## Over HTTP

Money is `{"amount": "10.50", "currency": "USD"}`, the amount as text with
the currency's places; OpenAPI has one `Money` schema that each field
refers to, and a `Currency` field is its code. A request is refused, 422
`validation_error`, with each error's type:

| Type | Why |
| --- | --- |
| `money_type` | not an object of exactly `amount` and `currency` |
| `money_amount` | the amount is not text of digits (a JSON number with a fraction, an exponent) |
| `inexact_amount` | more places than the currency has |
| `unknown_currency` | the code names no known currency |
| `money_digits` | a write schema's field cannot hold the amount (its `Digits`) |

A `MoneyError` raised by the domain or a service (`CurrencyMismatch`,
`InexactAmount`, `UnknownCurrency`) is 422 too, with its code
(`currency_mismatch`, ...) and a message that names no amount.

A `FilterSet` filter of a money field compares amounts within one
currency, so it brings a `<name>_currency` parameter:
`?balance__gte=10.00&balance_currency=USD`. An amount without the currency
is a 422 `invalid_filter`; the currency alone filters by currency
([queries.md](queries.md#filters-over-http)). Cursors of a money ordering
hold its amount and code. A CSV export writes money as its JSON in one
cell; the admin lists, filters, and describes it as any field.

## In tests and fixtures

`MoneyFactory` (`domainkit.testing`) makes amounts: 1, 2, 3, ... in USD,
or in the currency given:

```python
from domainkit.testing import Factory, MoneyFactory, SelfAttribute, SubFactory


class InvoiceFactory(Factory[Invoice]):
    class Meta:
        model = Invoice

    total = SubFactory(MoneyFactory, currency="EUR")
    tip = SubFactory(MoneyFactory, currency=SelfAttribute("..total.currency"))


InvoiceFactory.build(total__amount="9.99", total__currency="KWD")
```

`dumpdata` writes the two columns, the amount as text
(`{"balance": "10.5000", "balance_currency": "USD"}`); `loaddata` refuses
an amount with more places than its currency, or one of a pair without
the other.

## Generators

`--field balance:money` (or `balance:money:12:2`) gives an aggregate a
money field, and one that may not be None the transfer of
[databases.md](databases.md#a-transfer-of-money): methods that move it and
refuse an overdraft, a command whose use case locks both rows in id order
and runs again when it loses a race, `POST .../transfer-balance`, and
their tests ([resources.md](resources.md#generators)). `addcommand ...
--field amount:money` gives a command an amount.
