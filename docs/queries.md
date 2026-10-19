# Queries: criteria, pages, filters, and locks

Repositories answer queries without hand-written SQL, and their in-memory
fakes answer them the same way, so a use case tested against a fake behaves
the same in production. This page covers querying, paging, filtering over
HTTP, bulk changes, soft delete, row locks, and savepoints. The decisions are
ADRs [0022](adr/0022-query-repositories-with-criteria.md) and
[0023](adr/0023-keyset-pages-and-typed-cursors.md).

## Querying a repository

Conditions are built from fields (`F`), as in Django:

```python
from domainkit.ddd import F
from domainkit.ddd.query import Query

open_ = F("status") == Status.OPEN
large = F("total") >= Decimal("1000")

async with uow:
    orders = await uow.orders.filter(
        Query(open_ & large, ordering=["-placed_at"], limit=20)
    )
    first = await uow.orders.first(Query(open_, ordering=["placed_at"]))
    how_many = await uow.orders.count(open_)
    any_left = await uow.orders.exists(~open_)
```

`filter`, `first`, `count`, and `exists` are on every repository: the port
(`Repository`), `InMemoryRepository`, and `SqlAlchemyRepository`. `filter`
and `first` also take bare criteria or a specification. Every aggregate they
return is tracked, so the events it records are published on commit.

| Expression | Lookup | Means |
| --- | --- | --- |
| `F("x") == v`, `F("x") != v` | `exact` | equal, not equal; `== None` is `isnull()` |
| `<`, `<=`, `>`, `>=` | `lt`, `lte`, `gt`, `gte` | compared |
| `F("x").in_([a, b])` | `in` | one of the values (none: never) |
| `F("x").range(low, high)`, `.between(...)` | `range` | both ends included |
| `F("x").isnull()`, `.isnull(False)` | `isnull` | missing, present |
| `.iexact(t)` | `iexact` | equal, ignoring case |
| `.contains(t)`, `.icontains(t)` | `contains`, `icontains` | holds the text |
| `.startswith(t)`, `.istartswith(t)` | `startswith`, `istartswith` | starts with it |
| `.endswith(t)`, `.iendswith(t)` | `endswith`, `iendswith` | ends with it |
| `F("x").lookup("gte", v)` | any | by name, as `x__gte` |

Combine criteria with `&`, `|`, and `~` (never `and`, `or`, `not`: criteria
refuse to be true or false, so the mistake fails loudly). An ordering is
`F("x").asc()`, `F("x").desc()`, or Django's `"x"` and `"-x"`.

`Query(criteria, ordering=..., limit=..., offset=..., after=...,
lock=..., include_deleted=...)` is a frozen value; `where(...)` adds
criteria and `replace(...)` changes any part. (`domainkit.ddd.Query` is a
different thing: the base class of CQRS query messages.)

### What a query means everywhere

The in-memory repository evaluates a query in Python and the SQL repository
compiles it, with these rules on both:

- **Names are checked.** A name must be a field of the aggregate (in SQL,
  also a mapped column). An unknown one raises `UnknownField` before any
  SQL runs. A dotted name reaches into child entities: `F("lines.sku") ==
  "A-1"` holds when some line has that sku (`EXISTS` in SQL). Each
  condition on a collection is checked on its own, as Django's chained
  `filter()` calls are.
- **Values are checked** against the field's type hint and raise
  `InvalidQuery` when they do not fit: an `int` field refuses `"3"` and
  `True`, an amount refuses a float, a datetime must be timezone-aware. An
  enum field takes a member or its value (`F("status") == "open"`). A
  single-field value object (`ProductName(value: str)`) takes the object or
  what it wraps, and is compared, ordered, and searched by what it wraps,
  as its column stores it. Amounts compare exactly, also on SQLite
  (`ExactDecimal`).
- **Missing values never match** a condition, and `~` negates exactly:
  `~(F("stock") > 5)` includes the rows without stock. A condition through
  a child entity that is not there is false.
- **The order is total.** Results come in the query's ordering, then by
  `id`. Missing values sort after every value ascending, and first
  descending (`NULLS LAST` and `NULLS FIRST` on every database).
- **Text is matched as characters.** `%` and `_` in a value are matched as
  themselves. The `i` lookups fold case with `str.lower()` in Python and
  `lower()` in SQL.

Two things depend on the database, and the fake cannot follow both:

- **Case outside ASCII.** SQLite's `lower()` folds only ASCII letters, and
  so does PostgreSQL's under the `C` collation; with a UTF-8 collation
  PostgreSQL folds the others too, as Python does. `F("name").icontains("é")`
  can therefore miss `"É"` on SQLite.
- **The order of text.** Python and SQLite order text by code point;
  PostgreSQL orders by the column's collation. A database created with
  `C` or `C.UTF-8` (`initdb --locale=C.UTF-8`, or
  `CREATE DATABASE ... TEMPLATE template0 LC_COLLATE 'C.UTF-8'`) orders as
  Python does; so does a column declared `String(200, collation="C")`. Keyset
  pages are right under any collation; only the order differs from the fake's.

### Specifications

A specification names a business rule, so the domain can test one aggregate
against it, and a repository can find every aggregate that meets it:

```python
from domainkit.ddd import Specification
from domainkit.ddd.query import Criteria


class Overdue(Specification[Invoice]):
    def __init__(self, today: date) -> None:
        self.today = today

    def to_criteria(self) -> Criteria:
        return (F("status") == Status.OPEN) & (F("due") < self.today)


if Overdue(today).is_satisfied_by(invoice):
    ...
late = await uow.invoices.filter(Overdue(today) & ~Disputed())
```

`is_satisfied_by` evaluates the criteria in Python by default. A subclass may
decide it in Python instead, but must keep its criteria in step.

## Pages

`paginate` returns a `Page`: up to `limit` aggregates, whether more follow,
and the position of the last one (its values of the ordering's fields, the
id last), which the next page continues after:

```python
query = Query(F("status") == "open", ordering=["-placed_at"])
first = await uow.orders.paginate(query, limit=20)
second = await uow.orders.paginate(query, limit=20, after=first.next_position)
skipped = await uow.orders.paginate(query, limit=20, offset=40)
counted = await uow.orders.paginate(query, limit=20, count=True)  # .total
```

Keyset pages stay fast on deep pages, and never skip or repeat a row when
others are added meanwhile. `offset` suits pages a person jumps to; `count`
costs one more query.

Over HTTP, `Pagination` reads the page from the query string and
`PageRequest` fetches it and answers:

```python
from typing import Annotated

from fastapi import Depends
from domainkit.http import PageOut, PageRequest, Pagination

pages = Pagination(default_limit=20, max_limit=100)  # offset=True, count=True


@router.get("/orders", dependencies=[allowed("view")])
async def list_orders(
    page: Annotated[PageRequest, Depends(pages)], uow: UnitOfWorkDep
) -> PageOut[OrderOut]:
    query = Query(ordering=["-placed_at"])
    async with uow:
        found = await page.fetch(uow.orders, query)
    return page.respond(found, [OrderOut.from_domain(o) for o in found.items], query)
```

The answer is `{"items": [...], "next_cursor": "...", "next_offset": null,
"total": null}`; clients pass `next_cursor` back as `cursor`. `limit` above
`max_limit`, and `offset` above `max_offset` (10,000), are 422
`validation_error`.

A cursor is base64url JSON: a version, the ordering it belongs to, and the
position's values ([ADR 0023](adr/0023-keyset-pages-and-typed-cursors.md)).
It is opaque but not secret: it shows the sort values of the last item the
client received, so order only by fields the client may see. Decoding
checks the version, the ordering, the number of values, and each value
against its field's type; anything else is a 422 `invalid_cursor`, never a
500, and the values reach SQL only as bound parameters. A cursor from
another ordering is refused, so changing `?ordering=` starts again.

## Filters over HTTP

A `FilterSet` declares, per field, which lookups clients may use, and turns
the query string into a query:

```python
from domainkit.http import Filter, FilterSet


class OrderFilters(FilterSet):
    model = Order
    status = Filter("exact", "in")
    total = Filter("gte", "lte")
    customer = Filter("icontains", field="customer_name")
    sku = Filter(field="lines.sku")
    ordering_fields = ("placed_at", "total")
    default_ordering = ("-placed_at",)
    search_fields = ("customer_name", "reference")


@router.get("/orders", dependencies=[allowed("view")])
async def list_orders(
    filters: Annotated[OrderFilters, Depends()],
    page: Annotated[PageRequest, Depends(pages)],
    uow: UnitOfWorkDep,
) -> PageOut[OrderOut]:
    query = filters.query()
    ...
```

`?status=open&total__gte=10&ordering=-placed_at&search=acme` then asks for
open orders of at least 10, newest first, whose customer name or reference
contains `acme`. `exact` is the bare name; other lookups are
`<name>__<lookup>`. `__in` and `ordering` repeat:
`?status__in=open&status__in=paid&ordering=-total&ordering=placed_at`.
Every word of `search` must be found in one of the search fields, ignoring
case. `range` is not offered: use `gte` and `lte`.

The parameters are typed from the model's fields, so OpenAPI documents each
with its type, an enum's values, the ordering choices, and the bounds, and
FastAPI answers a wrong type with 422 `validation_error`. The class is
checked when it is declared, so a mistake fails at import: a lookup that
does not fit its field (`gt` on a list, `icontains` on an amount) is a
`TypeError`, and a field the model lacks an `UnknownField`.

What FastAPI cannot see is a 422 `invalid_filter`: a lookup the set does
not offer on a field it filters, or on a field of the model
(`?total__regex=`), a filter given twice, an ordering field named twice,
more conditions than `max_conditions` (10) or search words than
`max_search_words` (5), and values a value object refuses. Other
parameters (`limit`, `cursor`, the route's own) are left alone. The other
bounds are `max_values` (50 values in an `__in`), `max_length` per filter
(200 characters), `max_search_length` (100), and `max_ordering` (3 fields).

## Several aggregates at once

`add_all` and `remove_all` add or remove each aggregate as `add` and
`remove` do, so their events are published on commit.

`SqlAlchemyRepository.update_rows(criteria, **values)` and
`delete_rows(criteria)` change rows in one statement instead. They bypass
the aggregates: no method runs, no invariant is checked, and no event is
recorded, so neither the outbox nor the audit trail hears of them. Use them
in an adapter for data fixes and housekeeping, behind a port method whose
name says what it does; they are not on the port, so a fake implements that
method itself. `update_rows` bumps the `version` column, so a copy loaded
earlier cannot overwrite the change, and updates the aggregates the session
holds. `delete_rows` deletes for good, soft-deletable rows too; child rows
go only where their foreign key says `ON DELETE CASCADE`. `None` as the
criteria means every row; deleted rows only with `include_deleted=True`.

## Soft delete

An aggregate that inherits `SoftDeletable` is marked deleted instead of
removed:

```python
from domainkit.ddd import AggregateRoot, SoftDeletable


class Invoice(SoftDeletable, AggregateRoot[InvoiceId]): ...


await uow.invoices.remove(invoice)  # sets deleted_at
await uow.invoices.remove(invoice, hard=True)  # deletes the row
await uow.invoices.restore(invoice_id)  # clears deleted_at
await uow.invoices.find(invoice_id, include_deleted=True)
await uow.invoices.filter(Query(F("deleted_at").isnull(False), include_deleted=True))
```

Every read leaves deleted aggregates out unless asked: `get`, `find`,
`filter`, `first`, `count`, `exists`, `paginate`, and `get_for_update`.
The table needs a nullable `deleted_at` column (`table_for` derives it).

A unique constraint still counts deleted rows: a deleted customer's email
stays taken. To let a value be used again, make the constraint a partial
unique index over live rows; restoring a row whose value was taken
meanwhile then fails with `DuplicateValue` (409):

```python
Index(
    "uq_billing_customers_email_live",
    "email",
    unique=True,
    postgresql_where=text("deleted_at IS NULL"),
    sqlite_where=text("deleted_at IS NULL"),
)
```

## Row locks

`get_for_update` loads an aggregate and locks its row until the transaction
ends; another transaction that locks it waits, then sees what the first
committed:

```python
async with uow:
    account = await uow.accounts.get_for_update(account_id)
    account.withdraw(amount)
    await uow.commit()
```

`nowait=True` raises `LockNotAvailable` (a `Conflict`, so 409
`lock_not_available`) instead of waiting; the transaction can go on.
`skip_locked=True` returns None for a locked row, as for a missing one. A
query locks every row it returns with `Query(..., lock=RowLock(...))`, in
its order, which keeps two transactions from locking the same rows in
opposite orders. A lock that cannot be had within `LOCK_TIMEOUT` is a 503
`database_busy`.

- **PostgreSQL** runs `SELECT ... FOR UPDATE OF <table>` (`NOWAIT`, `SKIP
  LOCKED`).
- **SQLite** has no row locks: the row is read without one. SQLite lets one
  transaction write at a time, so a lost update shows as `ConcurrentUpdate`
  (with a `version` column) or as "database is locked"; test locking on
  PostgreSQL.
- **In memory**, repositories that share their `items` and a `RowLocks`
  behave like two transactions: the second waits until the first's unit of
  work commits or rolls back, `nowait` raises, `skip_locked` skips, and a
  wait past `RowLocks(timeout=10.0)` raises `LockNotAvailable`.

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

When the block raises, its database changes are rolled back (`ROLLBACK TO
SAVEPOINT`), the aggregates it changed are read again, those it added are
forgotten with their events, events recorded inside are dropped, and tasks
it deferred are discarded; then the error propagates. A unique violation
inside becomes `DuplicateValue`, as at commit. Savepoints nest; `commit()`
inside one raises. The in-memory repositories restore their aggregates'
fields too.

## Read-only work

`SomeUnitOfWork(readonly=True)`, or a subclass with `readonly = True`,
refuses to commit (`ReadOnlyError`). The SQL one reads from a replica when
the database names one ([databases.md](databases.md#read-replicas)), and
its PostgreSQL transaction is `READ ONLY`, so the server refuses writes
too. Row locks need a writable unit of work.
