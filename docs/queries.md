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

`Selection(criteria, ordering=..., limit=..., offset=..., after=...,
lock=..., include_deleted=...)` is a frozen value; `where(...)` adds
criteria and `replace(...)` changes any part. Before 0.9.2 it was
`Query` from `domainkit.ddd.query`, a name 0.11 removed:
`domainkit.ddd.Query` is a different thing, the base class of CQRS query
messages, and the two met in the same files.

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
query = Selection(F("status") == "open", ordering=["-placed_at"])
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
    query = Selection(ordering=["-placed_at"])
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

Cursors that come in are at most `MAX_CURSOR_LENGTH` (4,096) characters.
Cursors that go out are never refused, so a page always answers; what an
ordering can put in one is bounded by its fields instead.
`cursor_length(ordering, Order)` gives the longest cursor an ordering can
make: text counts 6 bytes a character (JSON writes control characters as
`\u001f`) up to its `MaxLength`, amounts their `Digits`, enums their
longest value, and the id ends every ordering. A `FilterSet` checks its
orderings when declared (below); an ordering written by hand is yours to
keep short. A stored value longer than its field declares (SQLite does
not enforce `MaxLength`) still gets a cursor: it is logged, and refused
when it comes back.

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
FastAPI answers a wrong type with 422 `validation_error`. So is a value
the field cannot hold: an integer past 64 bits, or an amount past its
`Digits` (`Digits(10, 2)` takes 8 digits, then 2; without `Digits`, 40
either side of the point), so `1e999999999` never reaches the database.
The class is checked when it is declared, so a mistake fails at import: a
lookup that does not fit its field (`gt` on a list, `icontains` on an
amount) is a `TypeError`, and a field the model lacks an `UnknownField`.

Its orderings must fit a cursor, so a page can always say where the next
one starts. Each field of `ordering_fields` and `default_ordering` needs a
bound (`Annotated[str, MaxLength(n)]` on text, `Digits` on amounts), and
the default ordering, and the longest one clients may ask for (the
`max_ordering` fields that take most room, descending), must fit in
`MAX_CURSOR_LENGTH`. Otherwise declaring the class is a `TypeError` that
says what to change:

```text
OrderFilters: Order.notes holds text of any length, which no cursor can bound:
declare its MaxLength(n), as Annotated[str, MaxLength(n)], or do not order by it.
```

Three text fields of `MaxLength(150)` fit together, in any script, with a
UUID id; for longer ones, lower `max_ordering`.

What FastAPI cannot see is a 422 `invalid_filter`: a lookup the set does
not offer on a field it filters (`?total__regex=`), a filter given twice,
an ordering field named twice, more conditions than `max_conditions` (10)
or search words than `max_search_words` (5), and values a value object
refuses. Other parameters (`limit`, `cursor`, the route's own, and
`<name>__<lookup>` for a name the set does not filter, a field of the
model or not) are left alone, so answers tell clients nothing of the
model's other fields. The other bounds are `max_values` (50 values in an
`__in`), `max_length` per filter (200 characters), `max_search_length`
(100), and `max_ordering` (3 fields).

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
await uow.invoices.filter(
    Selection(F("deleted_at").isnull(False), include_deleted=True)
)
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
query locks every row it returns with `Selection(..., lock=RowLock(...))`, in
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
fields too, and those of the entities they hold, in place: a child entity
is the same object after the rewind, so one also mapped to a table keeps
its mapping.

## Read-only work

`SomeUnitOfWork(readonly=True)`, or a subclass with `readonly = True`,
refuses to commit (`ReadOnlyError`). The SQL one reads from a replica when
the database names one ([databases.md](databases.md#read-replicas)), and
its PostgreSQL transaction is `READ ONLY`, so the server refuses writes
too. Row locks need a writable unit of work.

### How read-only work reads

`reads=` picks how a SQL unit of work reads, and makes it read-only:

```python
from domainkit.db import Reads

async with OrdersUnitOfWork(reads=Reads.STATEMENTS) as uow:  # one round trip
    order = await uow.orders.get(order_id)

async with OrdersUnitOfWork(reads=Reads.SNAPSHOT) as uow:  # one snapshot
    order = await uow.orders.get(order_id)
    lines = await uow.lines.filter(F("order_id") == order_id)


class ReportsUnitOfWork(SqlAlchemyUnitOfWork):
    reads = Reads.SNAPSHOT  # always
```

| `reads` | On PostgreSQL | Statements see |
| --- | --- | --- |
| `TRANSACTION` (`readonly=True` alone) | A `READ ONLY` transaction: `BEGIN`, the statements, `ROLLBACK` | What was committed when each started |
| `STATEMENTS` | Each statement on its own: no `BEGIN`, no `ROLLBACK` | What was committed when each started |
| `SNAPSHOT` | A `REPEATABLE READ`, `READ ONLY` transaction | What was committed when the first started |

- **Snapshots.** Two statements of a `STATEMENTS` or `TRANSACTION` unit of
  work may see different data: a write that commits between them shows in
  the second. When reads must agree with each other, such as an order and
  its lines, use `SNAPSHOT`.
- **Writes are refused.** With `STATEMENTS` or `SNAPSHOT`, a flush, or an
  `INSERT`, `UPDATE`, `DELETE` or DDL statement, raises `ReadOnlyError`
  before anything is sent. On PostgreSQL the server refuses writes in SQL
  text too (SQLSTATE 25006), whichever way the unit of work reads, and
  `commit()` always raises `ReadOnlyError`. Changes left unflushed at the
  end are dropped. With `STATEMENTS`, the server's refusal rests on the
  session's `default_transaction_read_only`: SQL text in the unit of work
  that turns it off could write until the unit of work ends. On SQLite,
  every read-only unit of work runs in a transaction that is rolled back,
  so whatever SQL text writes there is undone.
- **One statement, one transaction.** In `STATEMENTS`, `SET LOCAL` and
  transaction-scoped advisory locks (`pg_advisory_xact_lock`) end with the
  statement that takes them: they do nothing for the next one. What you
  set for the session (`SET`, `set_config(..., false)`, `SET ROLE`,
  `LISTEN`, session advisory locks) lasts until the unit of work ends;
  then the connection is reset before anyone else gets it. The reset
  costs one round trip, unless all the unit of work ran was plain
  `SELECT` statements of columns, which change nothing: SQL text, a
  function call or a statement run many times always count as changes.
  Use `TRANSACTION` or `SNAPSHOT` when statements must share a setting
  scoped to a transaction.
- **One engine for each way of reading.** `TRANSACTION` and `SNAPSHOT`
  sessions (and `STATEMENTS` where it reads in a transaction) bind to the
  alias's engine with their execution options, made once per event loop
  and shared by every such session on it; it shares the alias's pool, and
  `connections.close_all()` drops it with the engine. A new one for each
  session cost 100 to 200 Python calls.
- **Connections the server closed.** After a restart or failover, a
  `STATEMENTS` read gets a fresh connection. A statement whose connection
  closes while it runs fails, and is never run again: it may have had
  effects outside its transaction, through `dblink` or a foreign table.
- **Deadlines.** A `STATEMENTS` read is cut off at the request's deadline,
  like everything the request awaits, and the driver cancels it on the
  server. Work that shares a deadline without enforcing it, such as a task
  the request starts, bounds each statement by the time left.
  `STATEMENT_TIMEOUT` still applies to every statement
  ([runtime.md](runtime.md#what-keeps-to-it)).
- **Where it applies.** `STATEMENTS` runs without a transaction on
  PostgreSQL through asyncpg or psycopg, on a pool of its own (see
  [databases.md](databases.md#connection-pools)). Through PgBouncer
  (`PGBOUNCER`), which keeps no timeouts in the session, and on SQLite, it
  reads in the `READ ONLY` transaction. SQLite's driver begins a
  transaction only to write; there every read-only unit of work begins its
  own before it reads, so its reads agree, and holds SQLite's read lock
  until it ends.
- **Streaming** (`session.stream()`) needs a transaction: use
  `TRANSACTION` or `SNAPSHOT` for it.

ADR [0061](adr/0061-read-without-a-transaction-and-ping-once.md) explains
the choices.
