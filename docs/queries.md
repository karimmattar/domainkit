# Queries

A [repository](learning.md#words) answers questions about its
aggregates without SQL that you write by hand. You build a condition from
fields with `F`, and the repository finds, counts, pages, or locks what
it selects. The in-memory repository of your tests answers every query
the way the SQL one does. So a use case tested against a fake behaves the
same in production. The decisions behind this page are ADRs
[0022](adr/0022-query-repositories-with-criteria.md) and
[0023](adr/0023-keyset-pages-and-typed-cursors.md).

On this page you learn how to:

- find aggregates with conditions, orderings, and limits;
- name a business rule as a `Specification`;
- count and total without loading anything;
- read pages, over HTTP too, with cursors that never skip a row;
- let clients filter, order, and search a listing with a `FilterSet`;
- read many aggregates, or a few of their fields;
- change many rows in one statement;
- soft-delete, lock rows, use savepoints, and read without writing.

## A first example

An order has a customer, a status, a total, and lines:

```python
import asyncio
from abc import ABC
from dataclasses import field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, NewType
from uuid import UUID, uuid4

from domainkit.ddd import (
    AggregateRoot,
    Digits,
    Entity,
    F,
    MaxLength,
    Repository,
    Selection,
    UnitOfWork,
    in_memory,
)

OrderId = NewType("OrderId", UUID)
LineId = NewType("LineId", UUID)


class Status(StrEnum):
    OPEN = "open"
    PAID = "paid"


class OrderLine(Entity[LineId]):
    sku: Annotated[str, MaxLength(40)]
    quantity: int


class Order(AggregateRoot[OrderId]):
    customer: Annotated[str, MaxLength(100)]
    status: Status
    total: Annotated[Decimal, Digits(12, 2)]
    placed_at: datetime
    lines: list[OrderLine] = field(default_factory=list)


class ShopUnitOfWork(UnitOfWork, ABC):
    orders: Repository[Order, OrderId]


def order(customer: str, status: str, total: str, day: int, *skus: str) -> Order:
    return Order(
        id=OrderId(uuid4()),
        customer=customer,
        status=Status(status),
        total=Decimal(total),
        placed_at=datetime(2026, 3, day, tzinfo=UTC),
        lines=[OrderLine(id=LineId(uuid4()), sku=sku, quantity=1) for sku in skus],
    )


async def main() -> None:
    uow = in_memory(ShopUnitOfWork)
    async with uow:
        await uow.orders.add_all([
            order("Acme", "open", "1200.00", 1, "A-1"),
            order("Globex", "open", "80.00", 2, "B-2"),
            order("Initech", "paid", "2500.00", 3, "A-1", "C-3"),
        ])
        await uow.commit()

    open_ = F("status") == Status.OPEN
    large = F("total") >= Decimal("1000")
    async with uow:
        found = await uow.orders.filter(Selection(open_ | large, ordering=["placed_at"]))
        print([each.customer for each in found])
        print(await uow.orders.count(open_), await uow.orders.exists(~open_))
        print([each.customer for each in await uow.orders.filter(F("lines.sku") == "A-1")])


asyncio.run(main())
```

```text
['Acme', 'Globex', 'Initech']
2 True
['Initech', 'Acme']
```

The last query has no ordering of its own. The `Order` above declares
none either, so its results come by id, and the order changes from run
to run. [Orderings](#orderings) says how to fix it.

## Querying a repository

A condition names a field with `F`, then compares it, as in Django.
`Selection` adds an ordering and a limit:

```python
from domainkit.ddd import F, Selection

open_ = F("status") == Status.OPEN
large = F("total") >= Decimal("1000")

async with uow:
    orders = await uow.orders.filter(
        Selection(open_ & large, ordering=["-placed_at"], limit=20)
    )
    first = await uow.orders.first(Selection(open_, ordering=["placed_at"]))
    how_many = await uow.orders.count(open_)
    any_left = await uow.orders.exists(~open_)
```

Every repository has these methods: the port (`Repository`),
`InMemoryRepository`, and `SqlAlchemyRepository`.

| Method | Returns |
| --- | --- |
| `filter(query=None)` | A list of the aggregates selected, in the query's ordering. |
| `first(query=None)` | The first one, or None. |
| `count(criteria=None, *, include_deleted=False)` | How many meet the criteria. |
| `exists(criteria=None, *, include_deleted=False)` | Whether any does. |
| `get(id_)`, `find(id_)` | One aggregate by id. `get` raises the repository's `not_found`; `find` returns None. |

`filter` and `first` take a `Selection`, bare criteria, or a
`Specification`. Every aggregate they return is tracked, so the events it
records are published at the commit.

### Lookups

| Expression | Lookup | Means |
| --- | --- | --- |
| `F("x") == v`, `F("x") != v` | `exact` | Equal, not equal. `== None` is `isnull()`. |
| `<`, `<=`, `>`, `>=` | `lt`, `lte`, `gt`, `gte` | Compared. |
| `F("x").in_([a, b])` | `in` | One of the values. An empty list matches nothing. |
| `F("x").range(low, high)`, `.between(low, high)` | `range` | From `low` to `high`, both included. |
| `F("x").isnull()`, `.isnull(False)` | `isnull` | Missing, present. |
| `.iexact(t)` | `iexact` | Equal, in any case. |
| `.contains(t)`, `.icontains(t)` | `contains`, `icontains` | Holds the text. |
| `.startswith(t)`, `.istartswith(t)` | `startswith`, `istartswith` | Starts with the text. |
| `.endswith(t)`, `.iendswith(t)` | `endswith`, `iendswith` | Ends with the text. |
| `.regex(p)`, `.iregex(p)` | `regex`, `iregex` | Matches the regular expression (in code only). |
| `.search("late fee")` | `search` | Holds every word: full-text search on PostgreSQL ([search.md](search.md)). |
| `F("location").within(point, 2_000)` | `within` | A `Point` at most that many meters away ([search.md](search.md#places)). |
| `.year(2026)`, `.month(2026, 3)`, `.on(day)` | `__year`, `__month`, `__date` | In that period, in UTC. An index can answer it, because it is a range. |
| `F("x").lookup("gte", v)` | any | A lookup by its name, as `x__gte`. |

`regex` runs as `~` on PostgreSQL, and as Python's `re.search` on SQLite
and in memory. Write patterns that both read the same way: no lookbehind,
no named groups. A `FilterSet` does not offer `regex`: a pattern from a
caller can take exponential time. Take patterns from your code only.

### Combining conditions

Combine conditions with `&` (and), `|` (or), and `~` (not). Never use
`and`, `or`, or `not`: a condition refuses to be true or false, so the
mistake fails at once:

```text
TypeError: Combine criteria with &, | and ~, not with and, or, not.
```

A condition is a `Criteria`. `Selection` and the methods also take bare
criteria where they take a query.

### Orderings

An ordering is `F("x").asc()`, `F("x").desc()`, or the text `"x"` and
`"-x"`, as in Django. Each is an `Ordering`. A query that names no
ordering follows the aggregate's `Meta.ordering`, then the id. Without
`Meta.ordering`, it follows the id:

```python
class Order(AggregateRoot[OrderId]):
    placed_at: datetime

    class Meta:
        ordering = ("-placed_at",)
```

### `Selection`

`Selection` is a frozen value. It says which aggregates to load, in which
order, and how many:

```python
Selection(
    criteria,
    ordering=["-placed_at"],
    limit=20,
    offset=0,
    after=None,
    lock=None,
    include_deleted=False,
)
```

| Argument | Default | What it does |
| --- | --- | --- |
| `criteria` | `None` | The condition. None selects everything. |
| `ordering` | `()` | Orderings, as `Ordering` values or text. |
| `limit` | `None` | The most aggregates to return. Not negative. |
| `offset` | `0` | How many to skip. Not with `after`. |
| `after` | `None` | Continue after a position: a page's `next_position`. |
| `lock` | `None` | A `RowLock`: lock the rows it returns ([row locks](#row-locks)). |
| `include_deleted` | `False` | Include soft-deleted aggregates ([soft delete](#soft-delete)). |

`where(...)` returns a copy with more criteria, and `replace(...)` a copy
with any part changed. A negative `limit` or `offset`, or both `offset`
and `after`, raise `ValueError`.

`Selection` was called `Query` (in `domainkit.ddd.query`) before 0.9.2.
Release 0.11 removed that name. `domainkit.ddd.Query` is a different
thing: the base class of query messages ([resources](resources.md#queries)).

### What a query means everywhere

The in-memory repository evaluates a query in Python. The SQL repository
compiles it to SQL. Both keep these rules:

- **Names are checked.** A name must be a field of the aggregate (in SQL,
  also a mapped column). An unknown one raises `UnknownField` before any
  SQL runs: `UnknownField: Order has no field 'colour'.` A dotted name
  reaches into child entities: `F("lines.sku") == "A-1"` holds when some
  line has that sku (`EXISTS` in SQL). Each condition on a collection is
  checked on its own, as Django's chained `filter()` calls are.
- **Values are checked** against the field's type hint. A value that does
  not fit raises `InvalidQuery`: `InvalidQuery: Order.total holds Decimal,
  not float.` An `int` field refuses `"3"` and `True`. An amount refuses a
  float. A datetime must have a time zone. An enum field takes a member or
  its value (`F("status") == "open"`). A value object of one field
  (`ProductName(value: str)`) takes the object or what it wraps. It is
  compared, ordered, and searched by what it wraps, as its column stores
  it. Amounts compare exactly, on SQLite too (`ExactDecimal`).
- **Money compares within its currency.** `F("total") >= Money.of("10",
  "USD")` holds for totals in USD of at least 10.00, and for none in
  another currency; `~` of it includes those. Its two parts are fields:
  `F("total.currency") == "USD"`, `F("total.amount") > Decimal("9.995")`.
  Through child entities (`F("lines.price")`), the amount and its
  currency are of one child. Money sorts by currency code, then amount
  ([money.md](money.md#in-queries)).
- **Missing values never match** a condition, and `~` negates exactly:
  `~(F("stock") > 5)` includes the rows without stock. A condition
  through a child entity that is not there is false.
- **The order is total.** Results come in the query's ordering, then by
  `id`. Missing values sort after every value in ascending order, and
  first in descending order (`NULLS LAST` and `NULLS FIRST` on every
  database).
- **Text is matched as characters.** `%` and `_` in a value match
  themselves. The `i` lookups fold case with `str.lower()` in Python and
  `lower()` in SQL.

Two things depend on the database, and the fake cannot follow both:

- **Case outside ASCII.** SQLite's `lower()` folds only ASCII letters, and
  so does PostgreSQL's under the `C` collation. With a UTF-8 collation,
  PostgreSQL folds the other letters too, as Python does. So
  `F("name").icontains("é")` can miss `"É"` on SQLite.
- **The order of text.** Python and SQLite order text by code point.
  PostgreSQL orders it by the column's collation. A database created with
  `C` or `C.UTF-8` (`initdb --locale=C.UTF-8`, or
  `CREATE DATABASE ... TEMPLATE template0 LC_COLLATE 'C.UTF-8'`) orders as
  Python does. So does a column declared `String(200, collation="C")`.
  Keyset pages are right under any collation; only the order differs
  from the fake's.

### Specifications

A specification names a business rule. The domain can then test one
aggregate against it, and a repository can find every aggregate that
meets it:

```python
from datetime import date

from domainkit.ddd import Criteria, F, Specification


class Overdue(Specification[Invoice]):
    def __init__(self, today: date) -> None:
        self.today = today

    def to_criteria(self) -> Criteria:
        return (F("status") == "open") & (F("due") < self.today)


today = date(2026, 11, 4)
print(Overdue(today).is_satisfied_by(invoice))
late = await uow.invoices.filter(Overdue(today) & ~Disputed())
```

```text
True
```

Specifications combine with `&`, `|`, and `~`, with each other and with
criteria. `is_satisfied_by` evaluates the criteria in Python by default.
A subclass may decide it in Python another way, but must keep its
criteria in step.

### Your own criteria in SQL

A `Criteria` subclass of your own needs a compiler for SQL.
`register_compiler(kind, compiler)`, from `domainkit.db.query`, registers
one. The compiler takes the criteria (bound to the model), the model, and
the dialect's name. It returns a condition on the model's table, such as
an `EXISTS` on another table. Every value must be a bound parameter.

## Counting and totals

`aggregate` works out measures over what the criteria select, in one
statement, without loading anything (Django's `aggregate()`):

```python
from domainkit.ddd import Avg, Count, Max, Min, Sum

await uow.orders.aggregate(
    F("status") == "open",
    orders=Count(),
    revenue=Sum("total"),
    mean=Avg("total"),
    first=Min("placed_at"),
    last=Max("placed_at"),
)
```

```text
{'orders': 2, 'revenue': Decimal('1280.00'), 'mean': Decimal('640.00'),
 'first': datetime.datetime(2026, 3, 1, 0, 0, tzinfo=datetime.timezone.utc),
 'last': datetime.datetime(2026, 3, 2, 0, 0, tzinfo=datetime.timezone.utc)}
```

| Measure | Takes | Gives |
| --- | --- | --- |
| `Count()` | Nothing | How many aggregates. |
| `Count("field")` | Any field | How many have the field set. |
| `Sum("field")` | A number field | The total, of the field's type. |
| `Avg("field")` | A number field | The mean. A `Decimal` field gives a `Decimal`. |
| `Min("field")`, `Max("field")` | A field whose values sort | The smallest, the largest. |

Each is a `Measure`. Each leaves out aggregates without a value. Over no
aggregates, a measure is None. Totals and extremes keep the field's type,
and are exact on SQLite too. A money field is counted but not summed:
measure its amounts per currency, with the currency in the criteria. A
measure that does not fit its field is an `InvalidQuery`.

## Pages

`paginate` returns a `Page`. A page holds up to `limit` aggregates,
whether more follow, and the position of the last one: its values of the
ordering's fields, with the id last. The next page continues after that
position:

```python
query = Selection(F("status") == "open", ordering=["-placed_at"])
first = await uow.orders.paginate(query, limit=20)
second = await uow.orders.paginate(query, limit=20, after=first.next_position)
skipped = await uow.orders.paginate(query, limit=20, offset=40)
counted = await uow.orders.paginate(query, limit=20, count=True)  # .total
```

| `Page` attribute | Holds |
| --- | --- |
| `items` | The aggregates of the page. |
| `has_more` | Whether more follow. |
| `next_position` | The position to continue after, if more follow. |
| `total` | How many match in all, with `count=True`; else None. |

Keyset pages stay fast on deep pages. They never skip or repeat a row
when others are added in between. `offset` suits pages a person jumps
to. `count` costs one more query. A use case of your own may fetch
`limit + 1` items and call `Page.from_items(items, limit)`.

### Pages over HTTP

`Pagination` is a FastAPI dependency. It reads the page from the query
string, and gives a `PageRequest`. `PageRequest.fetch` reads the page, and
`PageRequest.respond` makes the answer, a `PageOut`:

```python
from typing import Annotated

from fastapi import Depends

from domainkit.http import PageOut, PageRequest, Pagination

pages = Pagination(default_limit=20, max_limit=100)


@router.get("/orders", dependencies=[allowed("view")])
async def list_orders(
    page: Annotated[PageRequest, Depends(pages)], uow: UnitOfWorkDep
) -> PageOut[OrderOut]:
    query = Selection(ordering=["-placed_at"])
    async with uow:
        found = await page.fetch(uow.orders, query)
    return page.respond(found, [OrderOut.from_domain(o) for o in found.items], query)
```

```json
{
  "items": [
    {"id": "49ef8292-...", "customer": "Initech", "status": "paid", "total": "2500.00"},
    {"id": "6bbd57b8-...", "customer": "Globex", "status": "open", "total": "80.00"}
  ],
  "next_cursor": "WzEsIi1wbGFjZWRfYXQsaWQiLCIyMDI2LTAzLTAy...",
  "next_offset": null,
  "total": null
}
```

Clients pass `next_cursor` back as `cursor`. `next_cursor` is null on the
last page.

| `Pagination` argument | Default | What it does |
| --- | --- | --- |
| `default_limit` | 50 | The `limit` without `?limit=`. |
| `max_limit` | 200 | The largest `limit`. More is 422 `validation_error`. |
| `offset` | `False` | Pages continue with `?offset=` instead of `?cursor=`, and the answer has `next_offset`. |
| `max_offset` | 10,000 | The largest `offset`. More is 422 `validation_error`. |
| `count` | `False` | Count every match into `total` (one more query per page). |

A `ResourceRouter` listing uses `Pagination` for you
([resources](resources.md)).

### Cursors

A cursor is base64url JSON: a version, the ordering it belongs to, and
the position's values ([ADR 0023](adr/0023-keyset-pages-and-typed-cursors.md)).
The cursor above decodes to:

```text
[1,"-placed_at,id","2026-03-02T00:00:00+00:00","6bbd57b8-c618-46c5-b88e-7d0e37e5e948"]
```

- **Opaque, not secret.** A cursor shows the sort values of the last item
  the client received. Order only by fields the client may see.
- **Checked.** Decoding checks the version, the ordering, the number of
  values, and each value against its field's type. Anything else is a
  422 `invalid_cursor` (`InvalidCursor`), never a 500. The values reach
  SQL only as bound parameters. A cursor from another ordering is
  refused, so a client that changes `?ordering=` starts again.
- **Bounded.** Cursors that come in are at most `MAX_CURSOR_LENGTH`
  (4,096) characters. Cursors that go out are never refused, so a page
  always answers. Instead, the ordering's fields bound what a cursor can
  hold.

`cursor_length(ordering, Order)` gives the longest cursor an ordering can
make. Text counts 6 bytes a character (JSON writes control characters as
`\u001f`), up to its `MaxLength`. Amounts count their `Digits`, and
enums their longest value. The id ends every ordering. A `FilterSet`
checks its orderings when you declare it (below). An ordering you write
by hand is yours to keep short. A stored value longer than its field
declares (SQLite does not enforce `MaxLength`) still gets a cursor: the
value is logged, and the cursor is refused when it comes back.

`encode_cursor(values)` and `decode_cursor(cursor, size)` encode and
decode a list of strings in the same opaque form, for pages of your own.

## Reading many

`stream` reads everything a query selects, a batch at a time (Django's
`iterator()`). Each batch is one statement that starts after the last
aggregate of the batch before. So no statement returns more than `batch`
rows, and none stays open between batches:

```python
async with ShopUnitOfWork(readonly=True) as uow:
    async for order in uow.orders.stream(F("status") == "paid", batch=500):
        write_line(order)
```

What `stream` yields is tracked, as what `filter` returns is. So a unit
of work that writes would commit changes made to it: read a large table
through a read-only one. A query with a `limit` or an `offset` is refused
with `ValueError`: a page is `paginate`'s job.

`values` reads a few fields of many aggregates as dicts, in one
statement that names only their columns (Django's `values()`). Use it for
a listing or an export:

```python
await uow.orders.values(Selection(F("status") == "paid", limit=100), "id", "total")
```

```text
[{'id': UUID('...'), 'total': Decimal('2500.00')}]
```

Nothing is loaded or tracked, so what `values` reads cannot be changed
and committed. It reads fields of the aggregate itself only: not a child
entity, a dotted name, or an encrypted field (load the aggregate to read
that). A query without an ordering follows `Meta.ordering`, then the id.

`explain` gives the database's plan for what `filter` would run, on SQL
repositories. It is `EXPLAIN` on PostgreSQL, where `analyze=True` also
runs the query and reports the time. It is `EXPLAIN QUERY PLAN` on
SQLite. The values stay bound parameters, and nothing is loaded:

```python
print(await uow.products.explain(F("sku") == "L-1"))
```

```text
SEARCH catalog_products USING INDEX sqlite_autoindex_catalog_products_2 (sku=?)
```

## Filters over HTTP

A `FilterSet` says, for each field, which lookups clients may use. It
turns the query string into a `Selection`:

```python
from typing import Annotated

from fastapi import Depends

from domainkit.http import Filter, FilterSet


class OrderFilters(FilterSet):
    model = Order
    status = Filter("exact", "in")
    total = Filter("gte", "lte")
    customer = Filter("icontains")
    sku = Filter(field="lines.sku")
    ordering_fields = ("placed_at", "total")
    default_ordering = ("-placed_at",)
    search_fields = ("customer",)


@router.get("/orders", dependencies=[allowed("view")])
async def list_orders(
    filters: Annotated[OrderFilters, Depends()],
    page: Annotated[PageRequest, Depends(pages)],
    uow: UnitOfWorkDep,
) -> PageOut[OrderOut]:
    query = filters.query()
    async with uow:
        found = await page.fetch(uow.orders, query)
    return page.respond(found, [OrderOut.from_domain(o) for o in found.items], query)
```

| Query string | Finds |
| --- | --- |
| `?status=open&total__gte=100&ordering=-total` | Open orders of at least 100, the largest first: `['Acme']`. |
| `?status__in=open&status__in=paid&search=glob` | Orders open or paid whose customer holds "glob": `['Globex']`. |
| `?sku=C-3` | Orders with a line of that sku: `['Initech']`. |

- `exact` is the bare name. Other lookups are `<name>__<lookup>`.
- `__in` and `ordering` may repeat:
  `?status__in=open&status__in=paid&ordering=-total&ordering=placed_at`.
- Every word of `search` must be found in one of the `search_fields`, in
  any case.
- `range` is not offered: use `gte` and `lte`. `within` is not offered
  either (filter by a place in your route), nor `regex` and `iregex`.
- `filters.query(limit=...)` passes other arguments to `Selection`.
  `filters.criteria` holds the criteria alone.

**Typed parameters.** The parameters are typed from the model's fields.
So OpenAPI documents each one with its type, an enum's values, the
ordering choices, and the bounds. FastAPI answers a wrong type with 422
`validation_error`. So is a value the field cannot hold: an integer past
64 bits, or an amount past its `Digits`. `Digits(10, 2)` takes 8 digits,
then 2; without `Digits`, 40 on either side of the point. So
`1e999999999` never reaches the database.

**Checked when declared.** The class is checked when Python reads it, so
a mistake fails at import:

- a lookup that does not fit its field (`gt` on a list, `icontains` on an
  amount) is a `TypeError`: `TypeError: total: icontains needs a text
  field.`;
- a field the model lacks is an `UnknownField`.

**Orderings fit a cursor.** A page must always be able to say where the
next one starts. So each field of `ordering_fields` and
`default_ordering` needs a bound: `Annotated[str, MaxLength(n)]` on text,
`Digits` on amounts. The default ordering, and the longest one clients
may ask for (the `max_ordering` fields that take most room, descending),
must fit in `MAX_CURSOR_LENGTH`. Otherwise declaring the class is a
`TypeError` that says what to change:

```text
OrderFilters: Order.notes holds text of any length, which no cursor can bound:
declare its MaxLength(n), as Annotated[str, MaxLength(n)], or do not order by it.
```

Three text fields of `MaxLength(150)` fit together, in any script, with a
UUID id. For longer ones, lower `max_ordering`.

**Money.** A filter of a money field compares amounts within one
currency. So it comes with a `<name>_currency` parameter, which an amount
needs. `?total__gte=10.00&total_currency=USD` asks for totals in USD of
at least 10.00. `?total__gte=10` alone is a 422 `invalid_filter`.
`?total_currency=USD` alone keeps the totals in USD. The amount may have
more places than the currency (`9.995`), and is compared exactly. A money
field orders by currency, then amount, and its cursor holds both.

**What FastAPI cannot see** is a 422 `invalid_filter` (`InvalidFilter`):

- a lookup the set does not offer, on a field it filters
  (`?total__regex=`): `Unknown lookup on total; it offers total__gte,
  total__lte.`;
- a filter given twice: `Give the status filter once.`;
- an ordering field named twice;
- more conditions than `max_conditions`, or more search words than
  `max_search_words`;
- a value that a value object refuses.

Other parameters are left alone: `limit`, `cursor`, the route's own, and
`<name>__<lookup>` for a name the set does not filter, whether the model
has that field or not. So answers tell clients nothing of the model's
other fields.

| `FilterSet` class variable | Default | What it does |
| --- | --- | --- |
| `model` | (required) | The aggregate class. |
| `ordering_fields` | `()` | Fields clients may order by, each ascending or descending. |
| `default_ordering` | `()` | The ordering without `?ordering=` (`"-placed_at"`). The id ends every ordering. |
| `search_fields` | `()` | Text fields that `?search=` looks in, in any case. |
| `max_conditions` | 10 | The most filters and search words one request may combine. |
| `max_values` | 50 | The most values an `__in` filter may list. |
| `max_search_length` | 100 | The longest `?search=`, in characters. |
| `max_search_words` | 5 | The most words in `?search=`. |
| `max_ordering` | 3 | The most fields one `?ordering=` may name. |

`Filter(*lookups, field=None, max_length=200)` allows `lookups` (default:
`exact`) on `field` (default: the attribute's name; a dotted name reaches
a child entity). `max_length` bounds text values, in characters.
`ordering`, `search`, and `request` cannot name a filter.

## Several aggregates at once

**Loading by ids.** `get_many(ids)` loads the aggregates of `ids` that
exist, in one statement, by ascending id (Django's `in_bulk`). A missing
id is left out. `get_many_for_update` locks them, and refuses a missing
one ([row locks](#row-locks)).

**Get or create.** `get_or_create(criteria, create)` returns the one
aggregate the criteria select, or makes one with `create()` and stores
it. It also says whether it created one. `update_or_create(criteria,
update, create)` reads the one it finds with its row locked, and calls
`update(aggregate)` on it, or creates one:

```python
customer, created = await uow.customers.get_or_create(
    F("email") == email, lambda: Customer.register(email)
)
account, _ = await uow.accounts.update_or_create(
    F("owner_id") == owner_id,
    lambda account: account.rename(name),
    lambda: Account.open(owner_id, name),
)
```

- `create` and `update` go through the aggregate's own factory and
  methods, so its rules and events hold.
- The new aggregate is inserted at once, in a savepoint. When another
  transaction inserted a matching one first, the unique constraint
  refuses this one. The stored one is then read and returned instead.
  That needs a unique constraint on what the criteria name. Without one,
  two transactions at once can each create one.
- More than one match is `MultipleFound`, a 409 `multiple_found`:
  `More than one Invoice meets the criteria.`

**Adding and removing many.** `add_all` and `remove_all` add or remove
each aggregate as `add` and `remove` do. So their events are published at
the commit.

### Changing rows in one statement

`SqlAlchemyRepository.update_rows(criteria, **values)` and
`delete_rows(criteria)` change rows in one statement instead. Each returns
how many rows it changed.

!!! warning
    `update_rows` and `delete_rows` bypass the aggregates. No method runs,
    no rule is checked, and no event is recorded. So neither the outbox
    nor the audit trail hears of them. Use them in an adapter, for data
    fixes and housekeeping, behind a port method whose name says what it
    does.

- They are not on the port. So a fake implements that port method itself.
- `update_rows` bumps the `version` column. A copy loaded earlier cannot
  overwrite the change. It also updates the aggregates the session holds.
- `delete_rows` deletes for good, soft-deletable rows too. Child rows go
  only where their foreign key says `ON DELETE CASCADE`.
- `None` as the criteria means every row. Deleted rows are included only
  with `include_deleted=True`.

A value may be computed from the row's own values (Django's `F()` in an
update). This subtracts in the database, in one statement, so two updates
at once never read and overwrite each other:

```python
await uow.products.update_rows(F("sku") == sku, stock=F("stock") - 1)
```

`F("stock") - 1` is an `Arithmetic`. `+`, `-`, and `*` take an `int` or
a `Decimal`, on fields of whole numbers or decimals. A missing value
stays missing.

## Soft delete

An aggregate that inherits `SoftDeletable` is marked deleted instead of
removed:

```python
from domainkit.ddd import AggregateRoot, F, Selection, SoftDeletable


class Invoice(SoftDeletable, AggregateRoot[InvoiceId]): ...


await uow.invoices.remove(invoice)  # sets deleted_at
await uow.invoices.remove(invoice, hard=True)  # deletes the row
await uow.invoices.restore(invoice_id)  # clears deleted_at
await uow.invoices.find(invoice_id, include_deleted=True)
await uow.invoices.filter(
    Selection(F("deleted_at").isnull(False), include_deleted=True)
)
```

- `SoftDeletable` adds the field `deleted_at: datetime | None`, and the
  methods `is_deleted()`, `mark_deleted(when=None)`, and `restore()`.
- Every read leaves deleted aggregates out unless you ask: `get`, `find`,
  `filter`, `first`, `count`, `exists`, `paginate`, and `get_for_update`.
- `restore` on an aggregate that is not `SoftDeletable` is a `TypeError`.
- The table needs a nullable `deleted_at` column. `table_for` derives it.

A unique constraint still counts deleted rows: a deleted customer's email
stays taken. To let a value be used again, make the constraint a partial
unique index over live rows. Restoring a row whose value was taken in
between then fails with `DuplicateValue` (409):

```python
from sqlalchemy import Index, text

Index(
    "uq_billing_customers_email_live",
    "email",
    unique=True,
    postgresql_where=text("deleted_at IS NULL"),
    sqlite_where=text("deleted_at IS NULL"),
)
```

## Row locks

`get_for_update` loads an aggregate and locks its row until the
transaction ends. Another transaction that locks the row waits, then sees
what the first one committed:

```python
async with uow:
    account = await uow.accounts.get_for_update(account_id)
    account.withdraw(amount)
    await uow.commit()
```

- `nowait=True` raises `LockNotAvailable` (a `Conflict`, so 409
  `lock_not_available`) instead of waiting. The transaction can go on.
- `skip_locked=True` returns None for a locked row, as for a missing one.
- A query locks every row it returns with
  `Selection(..., lock=RowLock(nowait=False, skip_locked=False))`, in its
  order. That keeps two transactions from locking the same rows in
  opposite orders. A `RowLock` takes `nowait` or `skip_locked`, not both.
- A lock that cannot be had within `LOCK_TIMEOUT` is a 503
  `database_busy`.

`get_many_for_update` locks several rows in one statement, always by
ascending id, and returns them by id:

```python
async with uow:
    accounts = await uow.accounts.get_many_for_update([source_id, target_id])
    accounts[source_id].withdraw(amount)
    accounts[target_id].deposit(amount)
    await uow.commit()
```

Two transfers between the same accounts, in opposite directions, lock
them in the same order, so one waits for the other. Locked one by one,
source then target, each could hold the row the other waits for.
PostgreSQL then ends one of them as a deadlock (503 `database_busy`). A
missing id raises the repository's `not_found`. `nowait` and
`skip_locked` work as for one row. With `skip_locked`, locked and missing
rows are left out. For work with no row to lock, `uow.lock(name)` holds a
lock by name until the transaction ends ([databases.md](databases.md#locks)).

| Where | What a row lock does |
| --- | --- |
| PostgreSQL | `SELECT ... FOR UPDATE OF <table>`, with `NOWAIT` or `SKIP LOCKED`. |
| SQLite | Nothing: SQLite has no row locks, and the row is read without one. SQLite lets one transaction write at a time, so a lost update shows as `ConcurrentUpdate` (with a `version` column) or as "database is locked". Test locking on PostgreSQL. |
| In memory | Repositories that share their `items` and a `RowLocks` behave like two transactions. The second waits until the first's unit of work commits or rolls back. `nowait` raises, `skip_locked` skips, and a wait past `RowLocks(timeout=10.0)` raises `LockNotAvailable`. |

## Savepoints

`savepoint()` undoes part of a unit of work when it fails, and keeps the
rest:

```python
async with uow:
    try:
        async with uow.savepoint():
            await reserve_stock(uow, order)
    except OutOfStock:
        order.backorder()
    await uow.commit()
```

When the block raises:

1. its database changes are rolled back (`ROLLBACK TO SAVEPOINT`);
2. the aggregates it changed are read again;
3. those it added are forgotten, with their events;
4. events recorded inside it are dropped, and tasks it deferred are
   discarded;
5. then the error propagates.

A unique violation inside becomes `DuplicateValue`, as at the commit.
Savepoints nest. `commit()` inside one raises `RuntimeError: Leave every
savepoint before committing.` The in-memory repositories restore the
fields of their aggregates, and of the entities they hold, in place. A
child entity is the same object after the rewind, so one also mapped to a
table keeps its mapping.

On SQLite, the driver begins a transaction only before a write. A
savepoint that comes first in a unit of work begins its transaction
before the `SAVEPOINT`. Releasing the savepoint then keeps its changes in
the transaction, for `commit()` to write or for the end of the block to
roll back, as on PostgreSQL. Before 0.12.1 that release committed them.

## Read-only work

`SomeUnitOfWork(readonly=True)`, or a subclass with `readonly = True`,
refuses to commit:

```text
ReadOnlyError: SqlAlchemyCatalogUnitOfWork is read-only; open one without readonly to change data.
```

The SQL unit of work reads from a replica when the database names one
([databases.md](databases.md#read-replicas)). Its PostgreSQL transaction
is `READ ONLY`, so the server refuses writes too. Row locks need a
unit of work that can write.

### How read-only work reads

`reads=` picks how a SQL unit of work reads, and makes it read-only:

```python
from domainkit.db import Reads, SqlAlchemyUnitOfWork

async with OrdersUnitOfWork(reads=Reads.STATEMENTS) as uow:  # one round trip
    order = await uow.orders.get(order_id)

async with OrdersUnitOfWork(reads=Reads.SNAPSHOT) as uow:  # one snapshot
    order = await uow.orders.get(order_id)
    lines = await uow.lines.filter(F("order_id") == order_id)


class ReportsUnitOfWork(SqlAlchemyUnitOfWork):
    reads = Reads.SNAPSHOT  # always
```

| `Reads` | On PostgreSQL | Statements see |
| --- | --- | --- |
| `TRANSACTION` (`readonly=True` alone) | A `READ ONLY` transaction: `BEGIN`, the statements, `ROLLBACK`. | What was committed when each statement started. |
| `STATEMENTS` | Each statement on its own: no `BEGIN`, no `ROLLBACK`. | What was committed when each statement started. |
| `SNAPSHOT` | A `REPEATABLE READ`, `READ ONLY` transaction. | What was committed when the first statement started. |

- **Snapshots.** Two statements of a `STATEMENTS` or `TRANSACTION` unit
  of work may see different data: a write that commits between them shows
  in the second. When reads must agree with each other, such as an order
  and its lines, use `SNAPSHOT`.
- **Writes are refused.** With `STATEMENTS` or `SNAPSHOT`, a flush, or an
  `INSERT`, `UPDATE`, `DELETE`, or DDL statement, raises `ReadOnlyError`
  before anything is sent. On PostgreSQL the server refuses writes in SQL
  text too (SQLSTATE 25006), whichever way the unit of work reads, and
  `commit()` always raises `ReadOnlyError`. Changes left unflushed at the
  end are dropped. With `STATEMENTS`, the server's refusal rests on the
  session's `default_transaction_read_only`: SQL text in the unit of work
  that turns it off could write until the unit of work ends. On SQLite,
  every read-only unit of work runs in a transaction that is rolled back,
  so whatever SQL text writes there is undone.
- **One statement, one transaction.** In `STATEMENTS`, `SET LOCAL` and
  transaction-scoped advisory locks (`pg_advisory_xact_lock`) end with
  the statement that takes them: they do nothing for the next one. What
  you set for the session (`SET`, `set_config(..., false)`, `SET ROLE`,
  `LISTEN`, session advisory locks) lasts until the unit of work ends.
  Then the connection is reset before anyone else gets it. The reset
  costs one round trip, unless all that the unit of work ran was plain
  `SELECT` statements of columns, which change nothing. SQL text, a
  function call, or a statement run many times always count as changes.
  Use `TRANSACTION` or `SNAPSHOT` when statements must share a setting
  scoped to a transaction.
- **One engine for each way of reading.** `TRANSACTION` and `SNAPSHOT`
  sessions (and `STATEMENTS` where it reads in a transaction) bind to the
  alias's engine with their execution options. That engine is made once
  per event loop, and shared by every such session on it. It shares the
  alias's pool, and `connections.close_all()` drops it with the engine.
  A new one for each session cost 100 to 200 Python calls.
- **Connections the server closed.** After a restart or a failover, a
  `STATEMENTS` read gets a fresh connection. A statement whose connection
  closes while it runs fails, and is never run again: it may have had
  effects outside its transaction, through `dblink` or a foreign table.
- **Deadlines.** A `STATEMENTS` read is cut off at the request's
  deadline, like everything the request awaits, and the driver cancels it
  on the server. Work that shares a deadline without enforcing it, such
  as a task the request starts, bounds each statement by the time left.
  `STATEMENT_TIMEOUT` still applies to every statement
  ([runtime.md](runtime.md#what-keeps-to-it)).
- **Where it applies.** `STATEMENTS` runs without a transaction on
  PostgreSQL through asyncpg or psycopg, on a pool of its own
  ([connection pools](databases.md#connection-pools)). Through PgBouncer
  (`PGBOUNCER`), which keeps no timeouts in the session, and on SQLite, it
  reads in the `READ ONLY` transaction. SQLite's driver begins a
  transaction only to write. There, every read-only unit of work begins
  its own before it reads, so its reads agree, and it holds SQLite's read
  lock until it ends.
- **Streaming** (`session.stream()`) needs a transaction. Use
  `TRANSACTION` or `SNAPSHOT` for it.

A read-only unit of work takes no isolation level:
`readonly=True` with `isolation=` is a `ValueError`. ADR
[0061](adr/0061-read-without-a-transaction-and-ping-once.md) explains the
choices.

## Options

Two options of each `DATABASES` entry bound queries:

| `DATABASES` option | Default | What it does |
| --- | --- | --- |
| `LOCK_TIMEOUT` | `10.0` | Seconds a statement may wait for a lock: a row lock or a named lock. Past it, 503 `database_busy`. |
| `STATEMENT_TIMEOUT` | `30.0` | Seconds before PostgreSQL cancels a statement. Migrations are exempt. |

[Backends](reference/backends.md#databases-and-migrations) has the full
rows. `MAX_CURSOR_LENGTH` (4,096) is a constant of `domainkit.http`, not a
setting. The arguments of `Selection`, `Pagination`, `FilterSet`, and
`Filter` are in their sections above.

## Errors and refusals

| Error | When | Over HTTP |
| --- | --- | --- |
| `UnknownField` | A query, a measure, or a `FilterSet` names a field the aggregate does not have. | 500 in code; a `FilterSet` fails at import |
| `InvalidQuery` | A value does not fit its field; a lookup does not fit a field; a measure does not fit; `values` names a child entity. | 422 when it comes from a request |
| `TypeError` | `and`, `or`, or `not` on criteria; a `FilterSet` lookup that does not fit; an ordering without a bound. | (at import) |
| `ValueError` | A negative limit or offset; both `offset` and `after`; `stream` with a limit; a `RowLock` with `nowait` and `skip_locked`. | |
| `MultipleFound` | `get_or_create` or `update_or_create` finds more than one. | 409 `multiple_found` |
| `LockNotAvailable` | `nowait=True`, and the row is locked. | 409 `lock_not_available` |
| (database) | A lock waits past `LOCK_TIMEOUT`, or a deadlock. | 503 `database_busy` |
| `DuplicateValue` | A unique constraint refuses a value, at the commit or in a savepoint. | 409 `duplicate_value` |
| `ReadOnlyError` | A read-only unit of work commits, flushes, or runs a write. | 500 (a bug in the code) |
| `InvalidFilter` | A filter the set does not offer, given twice, or past a bound. | 422 `invalid_filter` |
| `InvalidCursor` | A cursor that is malformed, altered, or of another ordering. | 422 `invalid_cursor` |
| (FastAPI) | A filter value of the wrong type, or `limit` or `offset` out of bounds. | 422 `validation_error` |

## Common mistakes

- **Writing `and` or `or` between conditions.** Use `&` and `|`. Put
  each comparison in parentheses: `(F("a") == 1) & (F("b") == 2)`.
- **Ordering by text without `MaxLength`.** A `FilterSet` refuses it,
  because no cursor could hold it. Give the field a `MaxLength`.
- **Expecting `update_rows` to publish events.** It bypasses the
  aggregates. Load the aggregates and call their methods when events
  matter.
- **Locking rows one at a time.** Two transactions can deadlock. Lock
  them together with `get_many_for_update`.
- **Testing locks on SQLite.** SQLite has no row locks. Test locking on
  PostgreSQL.
- **Reading one table twice for a report.** Two statements may see
  different data. Use `Reads.SNAPSHOT`.

## See also

- [Databases](databases.md): tables, migrations, transactions, and locks
  by name.
- [Resources](resources.md): listings that use `FilterSet` and
  `Pagination` for you.
- [Search and places](search.md): `search` and `within`.
- [Money](money.md#in-queries): conditions and orderings over money.
- [Testing](testing.md): in-memory units of work in tests.
