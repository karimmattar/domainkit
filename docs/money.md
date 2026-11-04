# Money

`domainkit.money` holds an amount of money as one value: an exact
`Decimal` amount and its currency. A service that handles money must
never lose a cent to a float, round where nobody decided to, or add
dollars to euros. `Money` makes each of these an error. The database,
the request schemas, the filters, the fixtures, and the generators all
know it, so an amount keeps its meaning from the request to the table
([ADR 0115](adr/0115-store-money-as-an-exact-amount-and-a-currency-code.md)).

On this page you learn how to:

- make amounts, and add, compare, and scale them;
- round an amount on purpose, with a named `Rounding`;
- split an amount into parts that always sum to it (`allocate`);
- use currencies of ISO 4217, and register your own;
- keep money in an aggregate, a table, a query, and a request;
- read the errors that money raises.

## A first example

```python
from domainkit.money import Money, Rounding

price = Money.of("10.5", "USD")
print(repr(price))
print(repr(price + Money.of("0.25", "USD")))
print(repr(price.multiply("0.15", rounding=Rounding.HALF_UP)))
print(Money.of("100", "USD").allocate([1, 1, 1]))
```

```text
Money('10.50', 'USD')
Money('10.75', 'USD')
Money('1.58', 'USD')
[Money('33.34', 'USD'), Money('33.33', 'USD'), Money('33.33', 'USD')]
```

The amount always has its currency's places: `"10.5"` becomes `10.50`.
15% of 10.50 is 1.575, which a dollar cannot hold, so `multiply` asks
how to round it. The three parts of 100.00 sum to 100.00 exactly.

domainkit converts nothing between currencies. A rate needs a source
your project trusts, read at a time it chose. Convert in your own code,
and round as your rules say:

```python
from decimal import Decimal

from domainkit.money import Money, Rounding

euros = Money.of("20.00", "EUR")
rate = Decimal("1.0842")  # from the source your project trusts
dollars = Money.of(euros.amount * rate, "USD", rounding=Rounding.HALF_EVEN)
```

## Currencies

A currency is a code, a numeric code, and its minor units: the number of
places of its amounts. `currency(code)` gives the `Currency` of a code:

```python
from domainkit.money import currencies, currency

print(repr(currency("KWD")), repr(currency("JPY")), len(currencies()))
```

```text
Currency('KWD', 414, 3) Currency('JPY', 392, 0) 165
```

| Minor units | Currencies |
| --- | --- |
| 0 | JPY and others |
| 2 | USD, EUR, EGP, and most others |
| 3 | KWD, BHD, OMR, JOD, TND, LYD, IQD |
| 4 | CLF, UYW |

A `Currency` has four attributes: `code` (`"EGP"`), `numeric` (818, or
None), `minor_units`, and `name` (its English name).

The known currencies are the 165 of ISO 4217's list of active currencies
that have minor units. They are the list as SIX, the maintenance agency
of ISO 4217, published it on 2026-09-17 (`ISO_4217_PUBLISHED`). The data
is in `domainkit/money/iso4217.csv`. Codes whose minor units ISO 4217
gives as "N.A." (gold, silver, the SDR, `XTS`, `XXX`) are not known. A
code is upper case. Any other code raises `UnknownCurrency`.
`currencies()` lists every known currency, ISO 4217's and yours.

### Your own currencies

A token, or a unit of your own, is a currency that you register. Register
it in a domain's `ready()`, before any amount uses it:

```python
from domainkit.money import Money, register_currency

print(repr(register_currency("USDC", minor_units=6, name="USD Coin")))
register_currency("PTS", minor_units=0)  # loyalty points
print(repr(Money.of("1.5", "USDC")))
```

```text
Currency('USDC', None, 6)
Money('1.500000', 'USDC')
```

- A code has 3 to 8 capitals or digits, and starts with a letter. It has
  0 to 18 minor units.
- You cannot register an ISO 4217 code. You cannot register a code again
  with other minor units: the stored amounts would change their meaning.
  Both raise `ImproperlyConfigured`. Registering the same currency again
  returns it.
- A stored code must name a known currency when a row is read back.
  Register your currencies before you read their rows.
- `currencies_kept()` is a context manager. When its block ends, it
  removes what the block registered. The pytest plugin uses it, so each
  test keeps its registrations to itself.

## Amounts

`Money(amount, currency)` and `Money.of(amount, currency)` make an
amount. The amount is a `Decimal`, an `int`, or text: digits, a point,
and a sign, with no exponent (`"10.50"`). The currency is a `Currency`
or its code.

```python
from decimal import Decimal

from domainkit.money import Money, Rounding, currency

Money.of(Decimal("12.30"), currency("EUR"))  # Money('12.30', 'EUR')
Money.of("2.675", "USD", rounding=Rounding.HALF_UP)  # Money('2.68', 'USD')
Money.zero("USD")  # Money('0.00', 'USD')
Money.from_minor(1050, "USD")  # Money('10.50', 'USD')
Money.of("10.50", "USD").minor  # 1050
```

- A float is a `TypeError`: floats are not exact.
- The amount keeps its currency's places. Zero has no sign:
  `Money.of("-0.00", "USD")` is `Money('0.00', 'USD')`.
- An amount with more places than its currency is refused with
  `InexactAmount`, unless you name a `rounding`.
- An amount has at most `MAX_DIGITS` (38) digits before its point. That
  bounds money in memory and in JSON. A table column holds fewer (see
  [In tables](#in-tables)).

### Arithmetic and comparison

```python
from domainkit.money import Money

price = Money.of("10.50", "USD")
-price  # Money('-10.50', 'USD')
abs(Money.of("-3", "USD"))  # Money('3.00', 'USD')
price > Money.of("10", "USD")  # True
price == Money.of("10.50", "EUR")  # False
Money.sum([], "USD")  # Money('0.00', 'USD')
price.is_zero()  # False
str(price)  # '10.50 USD'
```

- `+`, `-`, unary `-`, `abs()`, `<`, `<=`, `>`, and `>=` take amounts of
  one currency. They are exact at any size. Two currencies raise
  `CurrencyMismatch`.
- `==` between two currencies is `False`, never an error.
- `sum(amounts)` works when there is at least one amount.
  `Money.sum(amounts, "USD")` is also zero for no amounts, and checks the
  currency of each one.
- Money is immutable and hashable.

### Scaling and rounding

`price.multiply(factor)` is the only way to scale an amount. The factor
is a `Decimal`, an `int`, or text. The product is exact. When it has
more places than the currency, it is refused, unless `rounding` says how
to round it:

| `Rounding` | 2.675 | 2.665 | -2.675 | 0.005 |
| --- | --- | --- | --- | --- |
| `HALF_EVEN` (banker's rounding) | 2.68 | 2.66 | -2.68 | 0.00 |
| `HALF_UP` (away from zero at a half) | 2.68 | 2.67 | -2.68 | 0.01 |
| `DOWN` (toward zero) | 2.67 | 2.66 | -2.67 | 0.00 |
| `UP` (away from zero) | 2.68 | 2.67 | -2.68 | 0.01 |
| `CEILING` | 2.68 | 2.67 | -2.67 | 0.01 |
| `FLOOR` | 2.67 | 2.66 | -2.68 | 0.00 |

Each value is the `decimal` module's constant of the same name:
`Rounding.HALF_EVEN == decimal.ROUND_HALF_EVEN`.

### Allocation

`amount.allocate(ratios)` splits an amount in proportion. The parts
always sum to the amount: no minor unit is lost or made.

```python
from decimal import Decimal

from domainkit.money import Money

Money.of("100", "USD").allocate([1, 1, 1])  # 33.34, 33.33, 33.33
Money.of("0.05", "USD").allocate([3, 7])  # 0.02, 0.03
Money.of("100", "USD").allocate([Decimal("0.7"), Decimal("0.3")])  # 70.00, 30.00
```

1. Each part gets its share of the minor units, rounded toward zero.
2. The units left over go one each to the parts whose shares lost most
   (the largest remainders). Among equal remainders, the first part
   comes first.

A part of ratio 0 gets nothing. A negative amount is split as its
opposite, and each part is negated. Ratios are `int` or `Decimal`. None
may be negative, and at least one must be above zero.

## In the domain

A domain layer may import `domainkit.money`: the layer rules allow it. A
field of an [aggregate](learning.md#words) holds `Money` like any other
value:

```python
from typing import Annotated, NewType
from uuid import UUID

from domainkit.ddd import AggregateRoot, BusinessRuleViolation, Digits, DomainEvent
from domainkit.money import Money

WalletId = NewType("WalletId", UUID)


class InsufficientFunds(BusinessRuleViolation):
    """The wallet holds less than the amount."""


class WalletPaid(DomainEvent):
    wallet_id: WalletId
    to: WalletId
    amount: Money


class Wallet(AggregateRoot[WalletId]):
    balance: Annotated[Money, Digits(12, 2)] = Money.zero("USD")
    limit: Money | None = None

    def pay(self, *, to: WalletId, amount: Money) -> None:
        if amount > self.balance:  # Another currency: CurrencyMismatch.
            raise InsufficientFunds(f"Wallet {self.id} holds less than {amount}.")
        self.balance -= amount
        self.record(WalletPaid(wallet_id=self.id, to=to, amount=amount))
```

Events, commands, and task arguments carry money as any pydantic field.
The outbox, audit details, and exports write it as
`{"amount": "10.50", "currency": "USD"}`.

## In tables

`table_for` stores a money field in two columns. The database can then
compare, order, and sum the amount, and the code reads as one value:

| Field | Columns |
| --- | --- |
| `balance: Money` | `balance` `ExactDecimal(18, 4)`, and `balance_currency` (`CurrencyCode`, 8 characters) |
| `balance: Annotated[Money, Digits(12, 2)]` | `balance` `ExactDecimal(12, 2)`, and the code |
| `limit: Money \| None` | Both columns nullable, and a check that both are missing or neither is |

For the `Wallet` above, `table_for(Wallet, "wallets_wallets")` makes
this table on PostgreSQL:

```text
CREATE TABLE wallets_wallets (
	id UUID NOT NULL,
	version INTEGER NOT NULL,
	balance NUMERIC(12, 2) NOT NULL,
	balance_currency VARCHAR(8) NOT NULL,
	"limit" NUMERIC(18, 4),
	limit_currency VARCHAR(8),
	CONSTRAINT pk_wallets_wallets PRIMARY KEY (id),
	CONSTRAINT ck_wallets_wallets_limit_money CHECK ("limit" IS NULL AND limit_currency IS NULL OR "limit" IS NOT NULL AND limit_currency IS NOT NULL)
)
```

**The check of a nullable pair.** `makemigrations` writes the check when
it creates the table. It also writes it when it adds a nullable money
field to a table that exists (in batch mode on SQLite). `table_for`
derives it when you give only the amount column. A row that holds half a
pair all the same (written around the check) is refused when it loads,
with `IncompleteMoney` (from `domainkit.db.money`), whose message names
no amount. `MoneyColumns(..., nullable=True)`, below, adds the check to
a table you write by hand. `money_check(name, amount, currency)`, in the
same module, builds it from two columns of your own.

**What a column holds.** An `ExactDecimal` has at most 18 digits in all
(`STORED_DIGITS`, what SQLite's 64-bit integers hold). The default
`(18, 4)` keeps 4 places, which hold every ISO 4217 currency, and 14
digits before the point. `Digits(12, 2)` holds dollars and euros, but not
dinars of 3 places: a write schema refuses them (`money_digits`). A
mapped money attribute refuses, as it is set, an amount its column
cannot hold:

```python
wallet.balance = Money.of("12345678901", "USD")
```

```text
AmountOutOfRange: wallets_wallets.balance holds 2 decimal places, 10 digits before the point: this USD amount does not fit.
```

`AmountOutOfRange` is a `MoneyError` (422 `amount_out_of_range`). So a
balance stops where the use case computed it, not later, when the
session writes it. On SQLite the amount is stored as whole minor units
of the column's scale, so it stays exact
([exact amounts](databases.md#exact-amounts)). `map_imperatively` maps
the pair to one attribute. `makemigrations` writes plain `NUMERIC` and
`VARCHAR(8)` columns.

**A table you write by hand.** `MoneyColumns` gives the same two
columns:

```python
from sqlalchemy import Column, Index, Integer, Table, Uuid

from domainkit.db import MoneyColumns, metadata

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
amount, and its ordering, on both databases. A field of type `Currency`
alone is one `CurrencyCode` column.

**Totals per currency** are one query:

```python
from sqlalchemy import func, select

totals = select(wallets.c.balance_currency, func.sum(wallets.c.balance)).group_by(
    wallets.c.balance_currency
)
```

Each total is an exact `Decimal` with the column's places, on SQLite too.

### In queries

Money compares only with money of its own currency, in memory and in SQL
alike ([queries](queries.md)):

```python
from decimal import Decimal

from domainkit.ddd import F
from domainkit.money import Money

F("balance") >= Money.of("10", "USD")  # balances in USD of at least 10.00
F("balance").range(Money.of("0", "EUR"), Money.of("5", "EUR"))
F("balance.currency") == "USD"  # its parts are fields too
F("balance.amount") > Decimal("9.995")  # of any places, compared exactly
```

- A condition on money is a condition on its currency and its amount. So
  `~(F("balance") > Money.of("0", "USD"))` holds for every balance in
  another currency.
- Through child entities, the amount and its currency belong to one
  child: `F("lines.price") >= Money.of("100", "USD")` holds for an order
  with a line priced in USD at 100 or more. It never compares the amount
  of one line with the currency of another (one `EXISTS` in SQL).
- A range of two currencies is an `InvalidQuery`.
- Money orders by currency code, then amount (`ordering=["-balance"]`).
  Keyset pages follow that order.
- `update_rows(criteria, balance=Money.of("1", "EUR"))` sets both
  columns.
- The mapped attribute compares as money in SQLAlchemy's own queries
  too: `select(Wallet).where(Wallet.balance < Money.of("100", "USD"))`
  gives the balances in USD under 100. `!=` holds for no money, and
  `Wallet.limit == None` is a missing limit.

## Over HTTP

In JSON, money is `{"amount": "10.50", "currency": "USD"}`. The amount is
text with the currency's places. OpenAPI has one `Money` schema that each
field refers to. A `Currency` field is its code.

```python
from domainkit.resources import schema_for

WalletOut = schema_for(Wallet)
WalletIn = schema_for(Wallet, write=True)
print(WalletOut.from_domain(wallet).model_dump_json())
```

```text
{"id":"00000000-0000-0000-0000-000000000007","version":0,"balance":{"amount":"10.50","currency":"USD"},"limit":null}
```

A request with bad money gets 422 `validation_error`. Each error in
`errors` has one of these types:

| Type | Why | The message |
| --- | --- | --- |
| `money_type` | Not an object of exactly `amount` and `currency`. | Give money as an object of an amount and a currency, and nothing else. |
| `money_amount` | The amount is not text of digits: a JSON number, an exponent. | Give the amount as text, such as 10.50, with at most 38 digits before the point. |
| `inexact_amount` | More places than the currency has. | The amount has more decimal places than its currency has. |
| `unknown_currency` | The code names no known currency. | The currency is not one this service knows. |
| `money_digits` | A write schema's field cannot hold the amount (its `Digits`). | The amount has more digits than this field holds. |

A `MoneyError` that the domain or a service raises is 422 too, with its
own code (`currency_mismatch`, `inexact_amount`, `unknown_currency`,
`amount_out_of_range`). Its message names no amount.

**Filters.** A `FilterSet` filter of a money field compares amounts
within one currency, so it adds a `<name>_currency` parameter:
`?balance__gte=10.00&balance_currency=USD`. An amount without the
currency is a 422 `invalid_filter`. The currency alone filters by
currency ([filters over HTTP](queries.md#filters-over-http)). Through
child entities (`Filter("gte", "lte", field="lines.price")`), the
amounts and the currency are of one line.

**Cursors and exports.** A cursor of a money ordering holds its amount
and code, at the currency's places. A column of fewer places may still
hold such a value (1.500 KWD in a column of 2 places), so
`cursor_length` counts the most places a currency has. A CSV export
writes money as its JSON in one cell. The admin lists, filters, and
describes it like any field.

## In tests and fixtures

`MoneyFactory`, from `domainkit.testing`, makes amounts: 1, 2, 3, and so
on, in USD, or in the currency you give:

```python
from domainkit.testing import Factory, MoneyFactory, SelfAttribute, SubFactory


class InvoiceFactory(Factory[Invoice]):
    class Meta:
        model = Invoice

    total = SubFactory(MoneyFactory, currency="EUR")
    tip = SubFactory(MoneyFactory, currency=SelfAttribute("..total.currency"))


InvoiceFactory.build(total__amount="9.99", total__currency="KWD")
```

`dumpdata` writes the two columns, the amount as text:
`{"balance": "10.5000", "balance_currency": "USD"}`. `loaddata` refuses
an amount with more places than its currency, and one column of a pair
without the other.

## Generators

`--field balance:money` (or `balance:money:12:2`) gives an aggregate a
money field. A money field that may not be None also gets
[the transfer](databases.md#a-transfer-of-money):

- methods that move the amount and refuse an overdraft;
- a command whose use case locks both rows in id order, and runs again
  when it loses a race;
- the route `POST .../transfer-balance`;
- their tests ([resources.md](resources.md#generators)).

`addcommand ... --field amount:money` gives a command an amount.

## Names and limits

| Name | What it is |
| --- | --- |
| `Money` | An amount in one currency: `Money(amount, currency)`, `Money.of(amount, currency, rounding=None)`, `zero`, `from_minor`, `sum`; `.amount`, `.currency`, `.minor`, `multiply`, `allocate`, `is_zero`. |
| `Currency` | `code`, `numeric`, `minor_units`, `name`. |
| `Rounding` | `HALF_EVEN`, `HALF_UP`, `DOWN`, `UP`, `CEILING`, `FLOOR`. |
| `currency(code)` | The `Currency` of a code, or `UnknownCurrency`. |
| `currencies()` | Every known currency. |
| `register_currency(code, minor_units, name="")` | Make a currency of your own. |
| `currencies_kept()` | Undo registrations when the block ends. |
| `MAX_DIGITS` | 38: the most digits before the point. |
| `ISO_4217_PUBLISHED` | 2026-09-17: the date of the ISO 4217 list domainkit knows. |

Money has no settings.

## Errors and refusals

Every money error is a `MoneyError`, a `ValueError`. Its `code` names it
for clients. Its `summary` says what is wrong in words safe to show
(translated). The message itself is for developers.

| Error | When | Over HTTP |
| --- | --- | --- |
| `InexactAmount` | An amount, or a product, has more places than its currency, and no rounding is named. | 422 `inexact_amount` |
| `UnknownCurrency` | A code is neither in ISO 4217 nor registered. | 422 `unknown_currency` |
| `CurrencyMismatch` | Two currencies were added, subtracted, or compared with `<`. | 422 `currency_mismatch` |
| `AmountOutOfRange` | A mapped column cannot hold the amount. | 422 `amount_out_of_range` |
| `IncompleteMoney` | A stored row holds one column of a pair. | 500 (a bug in the data) |
| `TypeError` | A float, or a `bool`, as an amount, a factor, or a ratio. | A bug in the code |

```text
InexactAmount: USD has 2 decimal places; an amount with more is refused rather than rounded: name a Rounding.
CurrencyMismatch: Cannot add EUR and USD amounts: convert one first, at a rate the project trusts.
UnknownCurrency: No currency has 'XYZ': use an ISO 4217 code, in capitals, or register the currency with register_currency.
```

## Common mistakes

- **Making money from a float.** `Money.of(10.5, "USD")` is a
  `TypeError`. Pass text or a `Decimal`: `Money.of("10.5", "USD")`.
- **Expecting `multiply` to round.** A product with more places than the
  currency is refused. Name the rounding your rules use:
  `price.multiply("0.15", rounding=Rounding.HALF_EVEN)`.
- **Splitting with division.** Three parts of 100.00 made with `/` lose
  a cent. Use `allocate([1, 1, 1])`.
- **A column too small for the currency.** `Digits(12, 2)` cannot hold
  KWD's 3 places. Keep the default `(18, 4)` when amounts may be in any
  currency.
- **Reading rows of a currency you have not registered.** Register your
  own currencies in a domain's `ready()`, before any query.

## See also

- [Databases: exact amounts](databases.md#exact-amounts): how
  `ExactDecimal` keeps amounts exact on SQLite and PostgreSQL.
- [Databases: a transfer of money](databases.md#a-transfer-of-money):
  moving an amount between two rows, with locks and retries.
- [Queries](queries.md): conditions, orderings, and pages over money.
- [Accounting](contrib/accounting.md): ledgers, postings, and balances
  built on `Money`.
- [The payment recipe](recipes/payments.md): a payment that runs once.
