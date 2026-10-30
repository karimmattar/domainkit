# 0115. Store money as an exact amount and a currency code

- Status: Accepted
- Date: 2026-10-30

## Context

Until 0.14 domainkit had exact amounts (`ExactDecimal`, ADR 0066's one
text form) but no money: no currency, no minor units, no rounding rule, no
way to split an amount without losing a cent (the fintech re-audit's
FIN-01, FIN-02, FIN-03). Every project wrote its own `Money` value object,
stored it through `register_column_type`, and could not filter or order by
it. The ledger of 0.14 builds on the type this record settles, so its
names and meanings are a contract.

**The value.** A frozen dataclass value object (`ValueObject`) would make
every multi-field rule of `schema_for` and `table_for` apply to it, nested
schemas and no columns, where money needs its own JSON form, its own
columns, and comparisons that refuse two currencies. Holding the amount as
an integer of minor units would make every `amount` a computation and every
log line a puzzle. An amount `Decimal` with exactly the currency's places,
refused (never rounded) past them, reads as it is, and keeps arithmetic
exact under a context that traps `Inexact`.

**The currencies.** ISO 4217's list one, as its maintenance agency (SIX)
publishes it, has 165 active codes with minor units (0 to 4); 13 more
(gold, the SDR, `XTS`, `XXX`) have none and are no currency an amount can
be counted in. A project also needs units of its own (tokens, points).
A setting would validate them at startup, but the domain declares them
best, beside the aggregates that use them; a function a domain calls in
`ready()` does, and a setting can be added on top later.

**The storage**, measured by a PoC (50,000 rows in five currencies with
0, 2, 3, and 4 minor units, plus the extremes) on SQLite and on
PostgreSQL 16:

- **A: the amount in an `ExactDecimal(18, 4)` column, the code beside it.**
- **B: the amount in minor units (`BIGINT`), the code beside it.**
- C: one text or JSON column: neither database compares or sums it as a
  number, so it was not measured.

| Measured | A | B |
| --- | --- | --- |
| Every extreme back exactly, both databases | yes | yes |
| `currency = 'USD' AND amount >= 10.00 ORDER BY amount`, as Python sorts | yes | yes, with the bound turned into minor units by hand |
| A bound with more places than the currency (`> 9.995`) | exact | must be rounded per currency first |
| `SUM` per currency in one query | exact decimals | integers, scaled in Python by each currency's units |
| Index on `(currency, amount)` used | yes, both (covering on SQLite) | yes, both |
| A page of 50 by that index | 0.32 ms SQLite, 0.54 ms PostgreSQL | 0.44 ms, 0.48 ms |
| What `psql` and a report read | `99999999999999.9900`, `USD` | `9999999999999999`, `USD` |

B's integers mean nothing without the currency registry of the process
reading them: a registered unit whose places were wrong, or an ISO
amendment of a currency's minor units, would change every stored amount
silently. A holds the amount itself; its cost is a scale fixed per column
(4 holds every ISO currency and 14 digits before the point).

## Decision

- `domainkit.money` (a domain layer may import it): `Currency` (code,
  numeric code, minor units, name), `currency()`, `currencies()`,
  `register_currency()` with `currencies_kept()` for tests, the ISO list
  as data (`iso4217.csv`, published 2026-09-17, `ISO_4217_PUBLISHED`);
  `Money` with `of`, `zero`, `from_minor`, `minor`, `sum`, `multiply`,
  `allocate` (largest remainder, ties in order); `Rounding`, the six
  `decimal` policies by name; `MoneyError` and its `UnknownCurrency`,
  `InexactAmount`, `CurrencyMismatch`, which are `ValueError`s and
  `DomainkitError`s. Floats are a `TypeError` everywhere; amounts have at
  most 38 digits before the point.
- The errors are not `DomainError`s: `domainkit.money` imports nothing of
  `domainkit.ddd`, which imports it to query money, so no import cycle
  can form. The HTTP layer answers a `MoneyError` with 422, its code, and a
  fixed, translated summary that names no amount.
- Design A. A money field `total` is two columns, `total` (the amount,
  `ExactDecimal(p, s)`, 18 and 4 by default, `Digits` to size it) and
  `total_currency` (`CurrencyCode`, eight characters), plus a check that
  both or neither are NULL when it may be missing. The amount column keeps
  the field's name, so a `Decimal` field becomes money by adding one
  column. `MoneyColumns` makes them for hand-written tables; `table_for`
  derives them; `map_imperatively` maps each pair to the one attribute (a
  composite, its columns under private names).
- Queries compare money only within its currency: a condition on money is
  bound into conditions on `total.currency` and `total.amount`, which
  queries may also name, so memory and SQL share one meaning. Money orders
  by currency code, then amount, and keyset pages follow that order.
- JSON is `{"amount": "10.50", "currency": "USD"}`, the amount as ADR
  0066's text, with one `Money` schema in OpenAPI. A `FilterSet` filter of
  a money field takes amounts with a `<name>_currency` parameter, which
  an amount filter requires. Cursors hold `[amount, code]`.

## Consequences

- Projects stop writing money, and a table's money is readable and
  summable in SQL. Moving from a `Decimal` balance to `Money` is one
  column and a data migration that fills it.
- A column's scale must cover the currencies stored in it; a write schema
  refuses an amount it cannot hold (`money_digits`) before the database
  does. A registered currency of more than 4 places needs `Digits`.
- A stored code must be known when its row is read: a project registers
  its currencies before reading them, and never changes their units.
- Comparing amounts of two currencies is an error in Python and false in
  a query; conversion stays the project's, with rates it trusts.
