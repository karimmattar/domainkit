# Databases

This page is about where your aggregates are stored, and how they get
there. A [unit of work](learning.md#words) opens one transaction on a SQL
database, and its repositories load and save whole aggregates. The tables
come from your domain classes, so you declare each field once. A project
can use several databases, read from replicas, split data across shards,
partition tables by time, and keep evidence in a ledger that proves it
never changed.

On this page you learn how to:

- write a SQL unit of work, and its in-memory twin for tests;
- make tables from your classes, with child entities, unique values,
  checks, and exact amounts;
- keep concurrent changes from overwriting each other (versions,
  isolation levels, locks, work run again);
- route domains to databases, read from replicas, and shard by key;
- write and apply migrations, partition tables, and load fixtures;
- keep evidence in an immudb ledger.

[Queries](queries.md) covers what repositories find: conditions, pages,
filters, bulk changes, soft delete, row locks, and savepoints.

## A first example

A product, its table, and a SQL unit of work on SQLite:

```python
import asyncio
from abc import ABC
from decimal import Decimal
from typing import Annotated, NewType
from uuid import UUID, uuid4

from domainkit.db import (
    SqlAlchemyUnitOfWork,
    connections,
    map_imperatively,
    metadata,
    table_for,
)
from domainkit.ddd import (
    AggregateRoot,
    Digits,
    F,
    MaxLength,
    Repository,
    Unique,
    UnitOfWork,
)

ProductId = NewType("ProductId", UUID)


class Product(AggregateRoot[ProductId]):
    name: Annotated[str, MaxLength(100)]
    sku: Annotated[str, MaxLength(40), Unique()]
    price: Annotated[Decimal, Digits(12, 2)]

    class Meta:
        checks = {"price_not_negative": F("price") >= 0}


class CatalogUnitOfWork(UnitOfWork, ABC):  # domain.py: the port
    products: Repository[Product, ProductId]


class SqlAlchemyCatalogUnitOfWork(SqlAlchemyUnitOfWork, CatalogUnitOfWork):
    """models.py: the catalog's repositories, in its database."""


products = table_for(Product, "catalog_products", domain="catalog")
map_imperatively(Product, products)


async def main() -> None:
    async with connections.engine().begin() as connection:
        await connection.run_sync(metadata.create_all)  # migrate does this for you
    async with SqlAlchemyCatalogUnitOfWork() as uow:
        await uow.products.add(
            Product(id=ProductId(uuid4()), name="Lamp", sku="L-1", price=Decimal("25"))
        )
        await uow.commit()
    async with SqlAlchemyCatalogUnitOfWork() as uow:
        print(await uow.products.first(F("sku") == "L-1"))
    try:
        async with SqlAlchemyCatalogUnitOfWork() as uow:
            await uow.products.add(
                Product(
                    id=ProductId(uuid4()), name="Lamp", sku="L-1", price=Decimal("25")
                )
            )
            await uow.commit()
    except Exception as error:
        print(f"{type(error).__name__}: {error} ({error.code})")
    await connections.close_all()


asyncio.run(main())
```

With `DATABASES = {"default": {"URL": "sqlite+aiosqlite:///shop.db"}}`,
it prints:

```text
Product(id=UUID('512a4ccc-...'), version=1, name='Lamp', sku='L-1', price=Decimal('25.00'))
DuplicateValue: The sku is already taken. (duplicate_value)
```

The database made the version 1 and gave the price its 2 places. The
second product broke the unique constraint, and the unit of work turned
that into a `DuplicateValue`, which answers 409 over HTTP.

## Two kinds of database

A project can use several databases, of two kinds. Each kind has its own
setting:

| Kind | Setting | URL | For | Migrated | In units of work |
| --- | --- | --- | --- | --- | --- |
| **SQL** | `DATABASES` | `postgresql+asyncpg://…` (`sqlite+aiosqlite://…` for development and tests) | Business data: read, write, transactions | Yes | Yes |
| **Ledger** | `LEDGERS` | `immudb://…` (`memory://` for tests) | Evidence: written once, read many times, verified | No | No |

**PostgreSQL 16 to 18** are supported. CI runs the suite on 18 and on 16,
as two required checks ([versioning.md](versioning.md#supported-python-and-dependencies)).
In production the application connects as a role with row-level rights,
and `migrate` connects as the schema owner (`MIGRATION_URL`). The test
plugin grants that role what it needs in the test databases
([testing.md](testing.md#test-databases)).

The decisions behind this page are ADRs
[0008](adr/0008-multiple-databases-and-routing.md) (several databases and
routing), [0009](adr/0009-sharding-with-hash-buckets.md) (sharding),
[0010](adr/0010-postgresql-range-partitioning.md) (partitioning),
[0011](adr/0011-immudb-ledger-for-evidence.md) (ledgers),
[0024](adr/0024-replicas-and-derived-tables.md) (replicas and derived
tables), and
[0092](adr/0092-retry-units-of-work-choose-isolation-and-lock-in-order.md)
(isolation, work run again, and locks).

## Units of work and their repositories

A domain's unit of work names its repositories once, as annotations, in
`domain.py`. The SQL unit of work, and the in-memory one for tests, are
made from them:

```python
from abc import ABC

from domainkit.db import SqlAlchemyUnitOfWork
from domainkit.ddd import Repository, UnitOfWork, in_memory


# domain.py: the port, plain Python
class ProductRepository(Repository[Product, ProductId]):
    def not_found(self, id_: ProductId) -> ProductNotFound:
        return ProductNotFound(f"Product {id_} does not exist.")


class CatalogUnitOfWork(UnitOfWork, ABC):
    products: ProductRepository
    brands: Repository[Brand, BrandId]  # no port of its own


# models.py: in SQL
class SqlAlchemyCatalogUnitOfWork(SqlAlchemyUnitOfWork, CatalogUnitOfWork):
    """The catalog's repositories, in its database."""


# tests: in memory
uow = in_memory(CatalogUnitOfWork)
await controllers.create_product(uow, name="Tea")
assert uow.committed == 1
```

- `SqlAlchemyUnitOfWork.create_repositories(session)` makes, for each
  annotation, a `SqlAlchemyRepository` that implements the port, bound to
  the transaction's session. Override it only to build them another way.
- A port that declares a query of its own (an abstract method) needs a
  class that writes it. Annotate that class on the SQL unit of work
  (`products: SqlAlchemyProductRepository`). Pass the in-memory one to
  `in_memory(CatalogUnitOfWork, products=InMemoryProducts())`. Without
  one, both name the method that is missing.
- A `SqlAlchemyRepository` subclass sets `model` to the aggregate class,
  unless its type argument names it.
- `in_memory` keeps each repository in a dictionary
  (`InMemoryRepository`) that answers queries as SQL does. A rollback puts
  the repositories back as they were. Committed events go to a bus of its
  own (`events=` to listen). `uow.committed` counts the commits.

### What a unit of work does

`UnitOfWork` is the base of both. You use it as `async with uow:`, and
call `commit()` inside the block. Leaving the block rolls back what was
not committed.

| Member | What it does |
| --- | --- |
| `commit()` | Runs `before_save()`, `clean()`, and `Meta.checks` on what the repositories saw, stages the outbox, commits, then publishes the events. |
| `rollback()` | Drops changes and deferred tasks that were not committed. |
| `defer(function, *, delay=None, dedupe_key=None, ordering_key=None, **kwargs)` | Runs `function(**kwargs)` in the background once this work commits (a `DeferredTask`, run by the worker). |
| `savepoint()` | Undoes part of the work when it fails ([savepoints](queries.md#savepoints)). |
| `run(work, *, retry=None)` | Runs the whole work again when it loses a race ([running work again](#running-work-again)). |
| `lock(name, *, nowait=False)` | Holds a lock by name until the transaction ends ([locks](#locks)). |
| `expect(model, id_, versions)` | Holds an aggregate to the versions an `If-Match` names ([resources](resources.md#versions-and-conditional-requests)). |
| `readonly` | Refuses to commit (`ReadOnlyError`). |
| `committed` | How many times it has committed. |
| `events` | The `EventBus` it publishes to (the global `event_bus` by default). |

```python
async with uow:
    order = await uow.orders.get(order_id)
    order.ship()
    uow.defer(send_shipping_mail, order_id=str(order.id), dedupe_key=f"ship:{order.id}")
    await uow.commit()
```

`dedupe_key` drops the task when a message with the same key is still in
the outbox. Messages with the same `ordering_key` (1 to 255 characters,
no NUL) run one at a time, in the order of their commits
([messaging](contrib/messaging.md)).

A `SqlAlchemyUnitOfWork` also takes these arguments:

| Argument | Class attribute | What it does |
| --- | --- | --- |
| `database=` | `database` | The target: a SQL alias or a shard group. Default: the domain's routing. |
| `shard_key=` | | The key that picks a database of a shard group. |
| `readonly=` | `readonly` | Read only, from a replica if there is one. |
| `reads=` | `reads` | How a read-only unit of work reads (`Reads`), and makes it read-only ([queries](queries.md#how-read-only-work-reads)). |
| `isolation=` | `isolation` | The isolation level of its transactions (`Isolation`). |
| `events=` | | The `EventBus` to publish to. |

Inside the block, `uow.session` is the SQLAlchemy `AsyncSession`, and
`uow.alias` the SQL alias in use.

## Tables from domain classes

`table_for` derives a table from an entity's dataclass fields and type
hints, so a field is declared once:

```python
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from sqlalchemy import Index

from domainkit.db import map_imperatively, table_for
from domainkit.ddd import AggregateRoot, Digits, MaxLength, ValueObject


class Status(StrEnum):
    DRAFT = "draft"
    LIVE = "live"


class ProductName(ValueObject):
    value: Annotated[str, MaxLength(100)]


class Product(AggregateRoot[ProductId]):
    name: ProductName  # a value object of one field
    sku: Annotated[str, MaxLength(40)]
    price: Annotated[Decimal, Digits(12, 2)]
    status: Status = Status.DRAFT  # an Enum
    published_at: datetime | None = None


products = table_for(
    Product,
    "catalog_products",
    Index("ix_catalog_products_sku", "sku", unique=True),
    domain="catalog",
)


def start_mappers() -> None:
    map_imperatively(Product, products)
```

On PostgreSQL, the table is:

```text
CREATE TABLE catalog_products (
	id UUID NOT NULL,
	version INTEGER NOT NULL,
	name VARCHAR(100) NOT NULL,
	sku VARCHAR(40) NOT NULL,
	price NUMERIC(12, 2) NOT NULL,
	status VARCHAR(5) NOT NULL,
	published_at TIMESTAMP WITH TIME ZONE,
	CONSTRAINT pk_catalog_products PRIMARY KEY (id)
)
```

| Type hint | Column |
| --- | --- |
| `int` | `BigInteger` (`INTEGER` on SQLite); `version` is `Integer` |
| `str` | `String(n)` with `MaxLength(n)`, else `Text` |
| `bool`, `float` | `Boolean`, `Double` |
| `Decimal` | `ExactDecimal(p, s)`; it needs `Digits(p, s)` |
| `datetime`, `date`, `time` | `UTCDateTime`, `Date`, `Time` |
| `UUID` (or a `NewType` of one) | `Uuid` |
| `Reference[XId]` | The id's column, indexed |
| `Money`, `Annotated[Money, Digits(p, s)]` | Two columns: `<name>` (`ExactDecimal(p, s)`, default 18 and 4) and `<name>_currency` ([money.md](money.md#in-tables)) |
| `Currency` | `CurrencyCode`: the code |
| `Point` | `PointType`: PostGIS geography, or JSON ([places](search.md#places)) |
| An `Enum` | `EnumValue`: the members' values, text or integers |
| A value object of one field | `ValueObjectType`: that field's column type |
| An `Encrypted()` field | `EncryptedColumns`: sealed text, and `<name>_index` with `blind_index` ([encrypted fields](#encrypted-fields)) |
| A class you registered | `register_column_type(Point, PointType())` |
| `X \| None` | The same, nullable. Anything else is `NOT NULL`. |

- `id` is the primary key. A `version` field (every aggregate has one)
  turns on optimistic locking ([versions](#versions)). `deleted_at` comes
  with `SoftDeletable`.
- `domain=` tags the table with its domain, as `info={"domain": ...}`
  does on a table you write by hand. Migrations and routing follow the
  tag.
- Child entities (`list[OrderLine]`, `Address | None`) are not columns.
  Map them as relationships ([child entities](#child-entities)).
- A `Column` in the arguments replaces the derived column of the same
  name: `Column("sku", String(40, collation="C"), nullable=False)`.
  Indexes, constraints, and other columns are added.
- A type the table does not cover is a `TypeError` that names the field.
  Pass a `Column` for it, or register a column type.
- Enums are stored by value, so `ORDER BY` follows the values, and you
  may rename a member. Changing a value needs a data migration.
- Value objects come back through their constructor, so their rules run
  when they load.
- The result is an ordinary `Table` on `metadata`. `makemigrations`
  writes plain SQL types into revisions (`sa.String(length=5)` for an
  enum). Tables you write by hand keep working beside derived ones.
- `MaxLength` and `Digits` say what storage needs. The domain checks
  them too ([validation](validation.md)).

### Column types

These SQLAlchemy types, from `domainkit.db`, are what `table_for` uses.
Use them in tables you write by hand too:

| Type | Stores |
| --- | --- |
| `UTCDateTime` | Datetimes with a time zone, in UTC, also on SQLite (which has no time zones). A naive datetime is refused. |
| `ExactDecimal(precision, scale)` | Fixed-point amounts that come back exactly, also on SQLite ([exact amounts](#exact-amounts)). |
| `EnumValue(Status)` | An `Enum` by its values: `String` as long as the longest text value, or an integer. |
| `ValueObjectType(ProductName, String(100))` | A value object of one field, as that field. |
| `CurrencyCode` | A `Currency` as its code (`"USD"`). |
| `MoneyColumns(name, precision, scale, *, nullable=False)` | The two columns of a money field ([money.md](money.md#in-tables)). |

`register_column_type(cls, column_type)` stores every field of `cls`
(and its subclasses) in `column_type`. `column_type` is a type, or a
function that makes one. Use it for a value object of several fields, or
any class the table above does not cover.

### Choices

`TextChoices` and `IntegerChoices` are enums whose members carry a label
to show. A field typed with one is stored by value, as any `Enum`. The
label is for people:

```python
from domainkit.ddd import IntegerChoices, TextChoices


class Status(TextChoices):
    DRAFT = "draft"  # the label "Draft" comes from the name
    PAID = "paid", "Paid in full"


class Priority(IntegerChoices):
    LOW = 1
    HIGH = 3, "High"


print(Status.PAID.label, Status.choices(), Priority.labels())
```

```text
Paid in full [('draft', 'Draft'), ('paid', 'Paid in full')] {1: 'Low', 3: 'High'}
```

A label may be a translation (`gettext_lazy("Paid in full")`). It is then
read in the request's language.

## Child entities

An aggregate owns its child entities: they are loaded, saved, and deleted
with it. Each lives in a table of its own, with a foreign key to the
aggregate's table, and is mapped as a relationship:

```python
from dataclasses import field

from sqlalchemy import Column, ForeignKey

from domainkit.db import child, children, map_imperatively, table_for
from domainkit.ddd import Entity


class OrderLine(Entity[LineId]):
    sku: Annotated[str, MaxLength(40)]
    quantity: int


class Order(AggregateRoot[OrderId]):
    lines: list[OrderLine] = field(default_factory=list)
    address: Address | None = None  # Address is an Entity too


order_lines = table_for(
    OrderLine,
    "sales_order_lines",
    Column(
        "order_id", ForeignKey("sales_orders.id", ondelete="CASCADE"), nullable=False
    ),
    domain="sales",
)


def start_mappers() -> None:
    map_imperatively(OrderLine, order_lines)
    map_imperatively(Address, addresses)
    map_imperatively(
        Order,
        orders,
        properties={
            "lines": children(OrderLine, order_by=order_lines.c.sku),
            "address": child(Address),
        },
    )
```

- `children` loads the collection with the aggregate (`lazy="selectin"`:
  one more query per page of aggregates). It never loads lazily, which
  async sessions cannot do. It saves new lines with the aggregate, and
  deletes a line removed from the list (`cascade="all, delete-orphan"`).
  `child` does the same for one entity. Pass `order_by` for a stable
  order, `collection_class=set` for a `set[...]` field, or any other
  `relationship()` option.
- Refer to another aggregate by its id, not by a relationship. Then each
  aggregate loads and locks on its own.
- Any other relationship given to `map_imperatively` that leaves `lazy`
  at SQLAlchemy's default raises on an unloaded access (`lazy="raise"`),
  and so does the reverse side its `backref` makes:
  `'Order.customer' is not available due to lazy='raise'`. A lazy load
  needs I/O, which an attribute access in async code cannot do. Without
  this, it would fail later with `MissingGreenlet`. Load it in the query
  (`selectinload(Order.customer)`), or pass `lazy="selectin"`.
  `lazy=True` asks for lazy loading on purpose, for
  `AsyncAttrs.awaitable_attrs` or code run with `session.run_sync`.
- `ON DELETE CASCADE` matters to bulk deletes (`delete_rows`) on
  PostgreSQL. SQLite enforces foreign keys only with
  `PRAGMA foreign_keys`.
- Queries reach into children with dotted names:
  `F("lines.sku") == "A-1"`.

### Many-to-many

An aggregate refers to another by id only. So Django's `ManyToManyField`
is a list of child entities, each holding a `Reference`. Their table is
the link table, and a link can carry its own data:

```python
from domainkit.ddd import Reference


class Tagging(Entity[TaggingId]):
    tag_id: Reference[TagId]
    added_at: datetime


class Article(AggregateRoot[ArticleId]):
    title: Annotated[str, MaxLength(200)]
    taggings: list[Tagging] = field(default_factory=list)

    def tag(self, tag_id: TagId) -> None:
        if all(each.tag_id != tag_id for each in self.taggings):
            self.taggings.append(
                Tagging(id=TaggingId(uuid4()), tag_id=tag_id, added_at=clock.now())
            )


await uow.articles.filter(F("taggings.tag_id") == tag_id)  # articles with a tag
```

Its table keeps each pair once with a `UniqueConstraint("article_id",
"tag_id")` among `table_for`'s items. `article_id` is the foreign key: a
column, but not a field. The `tag_id` column is indexed, as every
`Reference` is.

## Versions

An aggregate's table has a `version` column. `map_imperatively` makes
every `UPDATE` and `DELETE` of the aggregate check and bump it. A copy
loaded before someone else's change then fails with `ConcurrentUpdate`
(409 `concurrent_update`), instead of overwriting that change:

```text
ConcurrentUpdate: The data changed while this request was running; reload it and try again.
```

That holds wherever the stale write is flushed: at the commit, by a query
or a `flush()` before it, in a savepoint, or by a domain's `on_stage`
hook. Its `model` names the aggregate's class, for logs.

Without it, 10 requests that each read a balance of 1000.00, take 10.00
off, and commit, all succeed, and leave 990.00.

`table_for` always derives the column. A table you write by hand must
declare it, and `map_imperatively` refuses an aggregate whose table has
none:

```text
ImproperlyConfigured: billing.domain.model.Account is an aggregate, but its
table 'billing_accounts' has no 'version' column, so concurrent changes to
one row would overwrite each other without an error. ...
```

Add the column. Give it `server_default="0"` in the migration that adds it
to a table that already has rows:

```python
from sqlalchemy import Column, Integer, Table, Uuid

from domainkit.db import ExactDecimal, metadata

accounts = Table(
    "billing_accounts",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("balance", ExactDecimal(18, 2), nullable=False),
    Column("version", Integer, nullable=False),
    info={"domain": "billing"},
)
```

```python
op.add_column(
    "billing_accounts",
    sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
)
```

Or map it without optimistic locking, on purpose, for an aggregate only
one writer changes, or one that never changes after it is added:

```python
map_imperatively(Reading, readings, versioned=False)
```

A mapping that passes `version_id_col` (a column of another name) has
chosen already. A subclass mapped with `inherits` shares its base's
version. Optimistic locking makes all but one of the concurrent writers
fail. To make them wait their turn instead, load the aggregate with
`get_for_update` ([row locks](queries.md#row-locks)).

In memory, the version stays where you set it: the database bumps it, not
the aggregate.

## Exact amounts

Hold money in the domain as `Money` ([money.md](money.md)): an exact
amount and its currency, stored in an `ExactDecimal` column and a code
column beside it. Store any other amount that must not drift in an
`ExactDecimal(precision, scale)` column, not in `Numeric`:

```python
from sqlalchemy import Column

from domainkit.db import ExactDecimal

Column("total", ExactDecimal(14, 2), nullable=False)  # up to 999,999,999,999.99
```

An aggregate's table also needs its [`version`](#versions) column, or
concurrent changes to an amount overwrite each other.

**Why it matters.** SQLite has no decimal type. It keeps `NUMERIC` values
as floats, which hold 15 to 17 significant digits. So
`12345678901234567.89` comes back as `12345678901234568.00`, with no
error. SQLite is the default database for development and tests. A
project could pass its tests with amounts that PostgreSQL would store
differently. `ExactDecimal` closes that gap:

- **Storage.** On PostgreSQL the column is `NUMERIC(precision, scale)`.
  On SQLite it holds whole minor units (cents, for a scale of 2) as
  integers, which are exact. Revisions declare `NUMERIC` on both.
- **Values** are `Decimal` (or `int`), and come back as `Decimal` with
  the column's places: `Decimal("5")` is read as `Decimal("5.00")`.
  Floats are refused. So are values with more places or more digits than
  the column holds (`Decimal("1.005")` in a scale of 2). Round them
  yourself, with the rule your domain needs, before you store them.
  Comparisons take values of any places, and compare them exactly:
  `where(price > Decimal("9.995"))`, `price.in_(...)`,
  `price.between(...)`.
- **Precision** is at most 18 digits. Any 18 digits fit SQLite's 64-bit
  integers: `ExactDecimal(18, 2)` holds up to 9,999,999,999,999,999.99.

**Queries give the same results on both databases:** comparisons,
`ORDER BY`, `in_`, `between`, `+` and `-`, `sum`, `min`, `max`, and
multiplying or dividing by a number.

- `price * 2`, `price * Decimal("1.1")`, `price * bindparam("rate")`,
  `price * quantity` for an integer column, `price / 4`, and
  `price / quantity` all work.
- A number, given as a value or as an untyped bound parameter, is a
  factor, never an amount. It has at most 9 places: more is refused, not
  rounded.
- On SQLite, the amount (minor units) and the factor (billionths) are
  multiplied as exact integers, then divided back as a real number. So a
  result of 15 significant digits or fewer comes back exactly, as a
  `Decimal`. A quotient is computed in floating point.
- Put the amount first: `bindparam("rate") * price` is not typed, and
  SQLite refuses its `Decimal`.
- Amounts of different scales, and integer columns, are added,
  subtracted, and compared in the same units (`price + fee`,
  `price > rate`, `price > quantity`). On SQLite, the side with fewer
  places is scaled up. Put the amount first, or both amounts:
  `quantity + price` starts from the integer column, and SQLite would add
  a count to cents.
- For `avg`, name the type: `func.avg(price, type_=price.type)`.

**Do these in Python instead:**

- floor division (`price // 2` is refused);
- products and quotients of two amounts. `price * cost`, or
  `price * bindparam("x", type_=price.type)`, raise `TypeError` as they
  are built (on SQLite they would multiply cents by cents). Give the
  factor as a number instead;
- new amounts with more places than the column, such as
  `update(...).values(price=prices.c.price * Decimal("1.1"))`.
  PostgreSQL rounds them to the column's scale, and SQLite keeps the
  fraction of a cent. Compute them with your own rounding rule;
- results past SQLite's 64-bit integers (9,223,372,036,854,775,807 minor
  units). There a product or a scaled-up amount becomes a float, and
  `sum` fails with an overflow, where PostgreSQL computes exactly;
- casts to text, which give minor units on SQLite.

**Checks in SQL.** A check written as SQL (`CheckConstraint("refunded <=
paid")`) is rewritten on SQLite. Each comparison that names an amount is
counted in whole minor units of the largest scale it names: `fee * 100`
beside a column of 4 places, `cap <= 100` as `cap <= 10000`. So it
compares exactly, as integers. A number with more places than that unit,
another column, or `*`, `/`, `%` beside an amount is refused when the
table is made. Write such a rule as an expression of the columns, or in
Python. A plain-number server default of an amount is written in minor
units.

**As text.** Wherever domainkit writes an amount as text, it writes it in
fixed point (`domainkit.utils.decimals.decimal_text`):

| Value | Text |
| --- | --- |
| `Decimal("1E+3")` | `"1000"` |
| `Decimal("0E-8")` | `"0.00000000"` |
| `Decimal("-0.00")` | `"0.00"` |

The text keeps the value's own places, and zero has no sign. That holds
in the responses of `schema_for` schemas, CSV and NDJSON exports, outbox
events and task arguments, webhook payloads, audit details, evidence
facts, cursors, `dumpdata`, dynamic settings, privacy exports, and the
admin API's user fields. So the same amount has one text in every
record. The audit trail's pseudonyms of personal values hash that text
too, so equal amounts have one pseudonym. JSON strings stay strings. A
value with more than 100 digits before or after the point is no amount,
and is written as `str()` writes it (`1E+999999999`), so its text stays
short. Your own response models can write amounts the same way:

```python
from decimal import Decimal
from typing import Annotated

from pydantic import PlainSerializer

from domainkit.utils.decimals import decimal_text

Amount = Annotated[
    Decimal, PlainSerializer(decimal_text, return_type=str, when_used="json")
]
```

## Unique values

A field declares that its values are unique with `Unique()`, or that
queries look it up often with `Indexed()`. Several fields together go in
the aggregate's `Meta` (Django's `unique=True`, `db_index=True`,
`Meta.constraints`, and `Meta.indexes`):

```python
from domainkit.ddd import Indexed, Unique


class Member(AggregateRoot[MemberId]):
    email: Annotated[str, MaxLength(254), Unique()]
    status: Annotated[Status, Indexed()]
    region: Annotated[str, MaxLength(4)]
    number: int

    class Meta:
        unique = (("region", "number"),)
        indexes = (("status", "region"),)
```

`table_for` makes each a constraint or an index named after its table and
fields (`uq_members_region_number`). A name longer than PostgreSQL's 63
characters is cut, with a digest. So migrations can alter and drop them.

- A money field counts as its currency and amount columns.
- A child entity cannot be unique or indexed. Neither can an encrypted
  field: look it up by its `blind_index`.
- A one-to-one reference is `Annotated[Reference[UserId], Unique()]`.

**When a value is taken.** When a unique constraint or a primary key
refuses a value, the unit of work raises `DuplicateValue`, a `Conflict`
that answers 409 with the code `duplicate_value`. It does so at
`commit()`, and also when an earlier flush inside the `async with` block
hits the constraint, explicit or a query's autoflush. `constraint` and
`fields` say which constraint refused what. The message names the
fields, never the values, which may be personal data. Other integrity
errors, such as a missing required value or a broken foreign key, are
left as they are.

To give a constraint its own code, name a `DuplicateValue` subclass in
its `info`, or in its column's `info` when it covers one column:

```python
from sqlalchemy import Column, String, UniqueConstraint

from domainkit.ddd import DuplicateValue


class EmailTaken(DuplicateValue):  # code: email_taken
    """Another account has that email."""


Column("email", String(254), unique=True, info={"duplicate": EmailTaken})
UniqueConstraint("tenant_id", "handle", info={"duplicate": HandleTaken})
```

The constraint is found in `domainkit.db.metadata`, by the name
PostgreSQL reports, or by the table and columns SQLite reports. Checking
first, as `create_user` does for usernames, still gives a friendlier
message; the constraint settles two requests that both pass the check.
Auth's username and group name columns name `UsernameTaken` and
`GroupNameTaken`.

## References to any aggregate

A `Reference[InvoiceId]` names one kind of aggregate. A comment that may
be about an invoice or a payment holds an `ObjectRef` instead (Django's
`GenericForeignKey`). An `ObjectRef` holds the model's key, as content
types name it (`billing.invoice`), and the aggregate's id:

```python
from domainkit.ddd.refs import ObjectRef


class Comment(AggregateRoot[CommentId]):
    about: ObjectRef
    text: Annotated[str, MaxLength(2000)]
```

```python
from domainkit.db.refs import load, model_of, ref_to

comment = Comment(id=CommentId(uuid4()), about=ref_to(invoice), text="Paid late")
on_it = await uow.comments.filter(F("about") == ref_to(invoice))
invoice = await load(uow, comment.about)  # None once it is gone
model_of(comment.about)  # the Invoice class
```

- The field is one text column, `billing.invoice:<id>` (`ObjectRefType`).
- The database keeps no foreign key on it, because the target may be in
  any table. So a reference may outlive what it names: `load` then
  returns None.
- `ref_to` raises `LookupError` for a class that is not mapped in an
  installed domain.

Prefer a `Reference` when you know the kind. An `ObjectRef` is for the
few things that may point at anything: comments, tags, audit notes.

## Columns the database fills

Two markers leave a column's value to the database, as Django's
`db_default` and `GeneratedField` do:

```python
from domainkit.ddd import DatabaseDefault, Generated


class Line(AggregateRoot[LineId]):
    price_cents: int
    quantity: int
    total_cents: Annotated[int, Generated("price_cents * quantity")] = 0
    state: Annotated[str, MaxLength(10), DatabaseDefault("'open'")] = "open"
```

- `DatabaseDefault(sql)` gives the column a server default. The
  aggregate's own default still fills a new aggregate. The database's
  default fills rows written without the column: by another writer, and
  the rows a migration adds the column to. So `makemigrations` writes a
  migration that needs no hand to fill the rows that exist.
- `Generated(sql, stored=True)` makes the column the database's to work
  out from the row's other columns (`GENERATED ALWAYS AS (...) STORED`).
  Rows are written without it, and it is read back as they are written
  (`RETURNING`). So the aggregate holds the database's value after `add`,
  after a change, and once loaded. `stored=False` works it out on each
  read, where the database can (PostgreSQL before 18 cannot).

The SQL is the database's own. Keep to what both SQLite and PostgreSQL
read, when the tests run on SQLite.

## Checks

A rule that every stored aggregate keeps, on its own fields, goes in
`Meta.checks` (Django's `CheckConstraint`). Each check has a name, and is
written as criteria:

```python
class Item(AggregateRoot[ItemId]):
    stock: int
    cap: int | None = None
    status: Status = Status.OPEN

    class Meta:
        checks = {
            "stock_not_negative": F("stock") >= 0,
            "cap_under_1000": F("cap").isnull() | (F("cap") < 1000),
            "status_known": F("status").in_([Status.OPEN, Status.SHUT]),
        }
```

Each check holds twice:

1. A unit of work refuses to commit an aggregate that breaks one, after
   its `clean()`, in memory as in SQL. It raises `CheckFailed`, a
   `BusinessRuleViolation` that names the `check` (422
   `check_violation`):

   ```text
   CheckFailed: Product breaks its check 'price_not_negative'.
   ```

2. `table_for` writes it as a check constraint, `ck_<table>_<name>`. So a
   row written past the domain (a script, another service, raw SQL) is
   refused too, with the same code.

`makemigrations` adds a check declared on a table that exists. Drop a
check you remove with `op.drop_constraint` in the revision.

A condition holds on a present value only, as in a query. A field that
may be missing breaks `F("cap") < 1000` when it is missing, unless the
check says `isnull()`. A check compares fields stored in a column of
their own (not a child's, nor the parts of money) with numbers, text,
booleans, and enum members. It uses `==`, `!=`, `<`, `<=`, `>`, `>=`,
`in_`, `range`, and `isnull`, joined by `&`, `|`, and `~`. Those are
written the same on PostgreSQL and SQLite. Anything else (a text
pattern, an amount, a date, a rule across fields) is refused when the
table is made. Keep it in `clean()` ([validation](validation.md)).

## Encrypted fields

A field marked `Encrypted()` is stored sealed, never in clear, under the
keys of `domainkit.contrib.keys`. `table_for` stores it in
`EncryptedColumns`: an `EncryptedType` column of sealed text, and, with
`blind_index=True`, a `<name>_index` column to look it up by equality:

```python
from typing import Annotated

from domainkit.ddd import Encrypted


class Card(AggregateRoot[CardId]):
    holder_id: UUID
    number: Annotated[str | None, Encrypted(subject="holder_id", blind_index=True)] = (
        None
    )
```

`domainkit.db.encrypted` holds the pieces:

| Name | What it is |
| --- | --- |
| `EncryptedType` | The column type: text sealed under the key ring. |
| `EncryptedColumns` | The column of an encrypted field, and its blind index if it has one. |
| `Ciphertext` | A sealed value, as the column holds it. A plain string bound to the column is refused, so a query cannot compare ciphertext with a value by mistake. |
| `Plaintext` | A value a fixture gives in clear (`{"plaintext": ...}`), sealed as it loads. Its `repr` never shows the value. |
| `lookup(column, values)` | The rows whose column equals one of the values, through its blind index, under every data key of the ring. |

```python
from sqlalchemy import select

from domainkit.db.encrypted import lookup

select(cards).where(lookup(cards.c.number, ["4111111111111111"]))
```

A derived schema leaves an encrypted field out unless `include` names it
([resources](resources.md#schemas-from-the-domain)). [Keys](contrib/keys.md)
explains the keys, queries on encrypted fields, rotation, and
crypto-shredding.

## Transactions

A SQL unit of work is one transaction. By default it runs at the
database's own isolation level, `READ COMMITTED` on PostgreSQL. Each
statement sees what was committed before it began. `version` columns
refuse lost updates ([versions](#versions)), and nothing is run again.

Work that keeps a rule across rows needs more than that. Take two
transfers that each check "at least one account keeps a balance", and
each change a different account. Both commit: no row was changed twice,
so no version check fails, and the rule is broken. This is called write
skew.

### Isolation levels

A unit of work that writes chooses its level on the class, or when it is
made:

```python
from domainkit.db import Isolation, SqlAlchemyUnitOfWork


class LedgerUnitOfWork(SqlAlchemyUnitOfWork):
    isolation = Isolation.SERIALIZABLE


uow = OrdersUnitOfWork(isolation="repeatable read")
```

| `Isolation` | PostgreSQL | SQLite |
| --- | --- | --- |
| None (the default) | The server's default, `READ COMMITTED`. | As SQLite always does. |
| `READ_COMMITTED` | Each statement sees what was committed before it. | As the default. |
| `REPEATABLE_READ` | One snapshot. A row changed in between cannot be changed (a serialization failure). | As `SERIALIZABLE`. |
| `SERIALIZABLE` | As if the transactions ran one after another. One that could not have fails (a serialization failure). | `BEGIN IMMEDIATE`: they do run one after another. |

A level may be given as text, in any case, with `_` or a space between
its words.

- On PostgreSQL the level goes into the transaction's `BEGIN`: no
  statement or round trip more.
- On SQLite, `REPEATABLE_READ` and `SERIALIZABLE` begin the transaction
  with `BEGIN IMMEDIATE` before its first statement. Such units of work
  take the database's write lock first, and wait for each other (up to
  the driver's busy timeout, then "database is locked"). They never fail
  on each other's changes.
- A read-only unit of work takes no level. It says how it reads with
  `reads=` (`Reads.SNAPSHOT` reads one snapshot). `isolation=` with
  `readonly=True` is a `ValueError`.

### Running work again

At `REPEATABLE_READ` and `SERIALIZABLE`, PostgreSQL ends the losing
transaction with a serialization failure (SQLSTATE `40001`). At any
level, two transactions that wait for each other's locks end one of them
with a deadlock (`40P01`). Either one is answered 503 `database_busy`.
Running the whole work again usually succeeds. `run` does that:

```python
from domainkit.ddd import Retry

await uow.run(lambda: transfer(command, uow), retry=Retry(attempts=5))
```

The work is a function of no arguments that does the whole job. It enters
the unit of work itself, as an application service does. `run` never
runs part of an `async with` block again. What a failed run wrote went
with its transaction: outbox messages, audit entries, and the
`Idempotency-Key`'s mark included. The next run starts again from the
database. Events are published, and deferred tasks queued, only by the
commit that succeeds. In memory, the repositories are put back as they
were before the failed run.

| `Retry` argument | Default | What it does |
| --- | --- | --- |
| `attempts` | 3 | The most runs, 1 to 10. |
| `backoff` | 0.02 | Before each next run, a random wait of up to this many seconds, doubled for each run so far. |
| `max_backoff` | 1.0 | The longest wait, in seconds (at most 60). |
| `retry_on` | `()` | Errors of your own to run again on, such as `ConcurrentUpdate` for a lost optimistic race. |

`run(work)` without `retry` uses `Retry()`.

- Only serialization failures, deadlocks, and the `retry_on` errors run
  the work again. SQLite reports neither of the first two. Any other
  error is raised at once.
- After the last run, the last failure is raised as it was, with a note
  that says how many runs there were. It is also raised, without another
  run, when this unit of work committed during the run (running it again
  would commit twice), and when the wait would pass the request's
  deadline.
- Under a frozen clock (`freeze_time`), the wait takes no time.

!!! warning
    Work that changes something outside its unit of work must not run
    again: a call to a payment provider, or another unit of work. Leave
    such a change to a deferred task, which the outbox runs once.

`ResourceRouter.command_route(..., retry=Retry())` runs a command's
service this way ([resources.md](resources.md#commands)). The check that
the caller may reach the aggregate, and `If-Match`, are made again in
each run's transaction, so a run after another one committed sees its
version. `batch_route(..., retry=Retry())` does the same for each item,
or, for an atomic batch, runs the whole batch again. Routes do not retry
unless they say so.

### Locks

- `get_for_update(id)` locks one row, and `get_many_for_update(ids)`
  several, in one statement, by ascending id, whatever the order of
  `ids` ([row locks](queries.md#row-locks)). Two transfers that lock both
  their accounts that way take the locks in the same order. One waits for
  the other, and they cannot deadlock, where locking source then target
  can.
- `await uow.lock("payout:" + str(account_id))` holds a lock by name
  until the transaction ends, for work that has no row to lock. Take the
  lock before you read what it guards.

| Where | What `uow.lock(name)` takes |
| --- | --- |
| PostgreSQL | `pg_advisory_xact_lock` on `lock_key(name)`, a stable 64-bit hash of the name. `nowait=True` raises `LockNotAvailable` (409) instead of waiting. A wait past `LOCK_TIMEOUT` is a 503 `database_busy`. |
| SQLite | The database's write lock (`BEGIN IMMEDIATE`, unless the transaction wrote already). |
| In memory | A lock on the name for the whole process. |

`lock_key(name)` is a keyed BLAKE2b hash of the name's UTF-8, the same in
every process and release. `pg_locks` shows it as `classid` (the high 32
bits) and `objid` (the low ones), with `objsubid` 1:

```python
from domainkit.db import lock_key

lock_key("payout:1")  # 8824147027588705687
```

### A transfer of money

Here is everything together, with `Money` ([money.md](money.md)) as the
amount. The use case locks the two rows in one statement, by ascending
id, changes both, and lets `run` do it all again when the transaction
loses a race. A balance in another currency refuses the amount
(`CurrencyMismatch`, 422):

```python
from domainkit.ddd import ConcurrentUpdate, Retry

RACES = Retry(attempts=5, retry_on=(ConcurrentUpdate,))


async def transfer_wallet_balance(
    command: TransferWalletBalance, uow: WalletsUnitOfWork
) -> Wallet:
    """Move the amount from one wallet to another, whole or not at all."""
    source, target = WalletId(command.wallet_id), WalletId(command.to)

    async def work() -> Wallet:
        async with uow:
            wallets = await uow.wallets.get_many_for_update([source, target])
            wallets[source].transfer_balance(to=target, amount=command.amount)
            wallets[target].receive_balance(amount=command.amount)
            await uow.commit()
        return wallets[source]

    return await uow.run(work, retry=RACES)
```

`startaggregate wallets Wallet --field balance:money` writes:

- this use case and its command;
- the two methods (`transfer_balance` refuses an overdraft);
- `POST /wallets/{wallet_id}/transfer-balance`;
- their tests, one of them a lost race
  ([resources.md](resources.md#generators)).

Both rows are locked, so the rule on each holds at `READ COMMITTED`. A
rule across rows that are not both locked needs `SERIALIZABLE`
([the payment recipe](recipes/payments.md)). Two things are yours to
add:

- the idempotency middleware, so a request retried with its
  `Idempotency-Key` moves the amount once
  ([contrib/idempotency.md](contrib/idempotency.md));
- a rule that the caller may move money out of that wallet
  ([resources.md](resources.md#who-may-call-it)).

## Connections

`connections`, from `domainkit.db`, is the one `ConnectionHandler` of the
process (Django's `django.db.connections`). It makes engines and sessions
on first use, one set per event loop. So importing a module never opens
a connection, and a command and a test client in one process never share
a pool.

| Member | What it does |
| --- | --- |
| `engine(alias="default")` | This event loop's `AsyncEngine` for the alias. |
| `session(alias="default", *, readonly=False, reads=Reads.TRANSACTION)` | A new `AsyncSession`. Objects stay readable after the commit. |
| `autocommit(alias="default")` | A connection whose statements each commit as they run (through PgBouncer: one transaction). |
| `config(alias="default")` | The alias's `DATABASES` entry. |
| `check(alias="default")` | Runs `SELECT 1`; raises when the database cannot be reached. |
| `check_all(seconds)` | Checks every alias; returns `database:<alias>` for each failure. |
| `pool_stats()` | Pool use per alias, for metrics: `size`, `checked_out`, `overflow`. |
| `refuse(reason)`, `allow()` | Refuse every engine and session (`DatabaseAccessRefused`) until `allow()`. |
| `close_all()` | Dispose the running loop's engines; the next use makes fresh ones. |

```python
from sqlalchemy import text

from domainkit.db import connections

async with connections.engine("billing").connect() as connection:
    print((await connection.execute(text("SELECT 1"))).scalar())
```

The test plugin refuses the databases to a test that did not ask for
them. Such a test gets `DatabaseAccessRefused`, whose message names the
fixture to request ([testing](testing.md#database-access)). The
application's lifespan calls `close_all()` at shutdown.

On PostgreSQL, each transaction starts by setting a `statement_timeout`
no longer than the time left before the request's deadline, and, under
PgBouncer, every timeout.

## Connection pools

Each alias has a pool of connections per event loop (`POOL_SIZE`,
`MAX_OVERFLOW`, `POOL_TIMEOUT`, `POOL_RECYCLE`), made on first use.

- **Testing connections.** With `POOL_PRE_PING`, on by default, a
  connection is tested as it leaves the pool. One the server closed (a
  restart, a failover) is replaced before a request uses it. On asyncpg
  the test is one round trip: a prepared `;` outside any transaction.
  SQLAlchemy wraps it in `BEGIN` and `ROLLBACK`, three round trips, which
  only PgBouncer needs, and keeps. Keep the test on: without it, the
  first request on each stale connection fails.
- **Reads on their own.** The first `Reads.STATEMENTS` unit of work on an
  alias opens a second pool for that alias, sized like the first. PostgreSQL holds its
  connections read-only from the start
  ([queries.md](queries.md#how-read-only-work-reads)).
  - Each new connection is checked once. If its transactions are not
    read-only, because a pooler between you and PostgreSQL dropped the
    startup settings, it is refused with `ImproperlyConfigured`.
  - Through asyncpg the pool does not test connections: asyncpg notices
    when the server closes one, and it is replaced before a statement is
    sent on it. psycopg notices only when it next sends one, so through
    psycopg the pool pings.
  - A connection is reset before it goes to the next caller, unless it
    ran only plain `SELECT` statements.
  - A process may then hold up to twice the alias's connections. Count
    them when you size the server's `max_connections`.
  - `pool_stats()` and the `db_pool_connections` metric name the pool
    `<alias>:reads`. Readiness checks it as `database:<alias>:reads`
    once it is open. A replica read that way gets one too.

ADR [0061](adr/0061-read-without-a-transaction-and-ping-once.md) explains
the choices. [Backends](reference/backends.md#sizing-pools) helps you
size pools.

## Read replicas

An alias may name read replicas. Each replica is an alias of its own:

```python
DATABASES = {
    "default": {"URL": env.str("DATABASE_URL"), "REPLICAS": ["replica"]},
    "replica": {"URL": env.str("DATABASE_REPLICA_URL"), "SSLMODE": "verify-full"},
}
```

```python
async with OrdersUnitOfWork(readonly=True) as uow:  # on the replica
    page = await uow.orders.paginate(query, limit=50)


class ReportsUnitOfWork(SqlAlchemyUnitOfWork):
    readonly = True  # always on a replica
```

- **Reads.** A read-only unit of work opens its session on a replica of
  the alias its routing gives (a shard's alias works too). With several
  replicas, units of work take turns. Without `REPLICAS`, it reads the
  alias itself.
- **Writes** always go to the primary. A read-only unit of work refuses
  to commit (`ReadOnlyError`). On PostgreSQL its transaction is
  `READ ONLY`, so a write that goes around the unit of work is refused as
  well.
- **A replica is never a target.** Routing a domain to one, naming one as
  a unit of work's `database`, or migrating one is refused. Replication
  keeps it migrated.
- **Lag.** A replica may be behind. A request that writes, then reads
  what it wrote, should read in its writable unit of work.
- **Settings.** A replica is another alias than `default`, of one
  primary, outside every shard group, with no replicas of its own. Each
  is a `DATABASES` entry, so the deployment checks (TLS, `ECHO`,
  `LOG_PARAMETERS`) and `/ready` cover it, and it has its own pool.
- **Tests.** Point the replica at the primary's URL, or leave `REPLICAS`
  out of the test settings: two SQLite files do not replicate.

[The read replica recipe](recipes/read-replica.md) sets one up step by
step.

## Routing domains to databases

`DATABASE_ROUTING` maps a domain label to a **target**: a SQL alias, or a
shard group. Domains not listed use `default`:

```python
DATABASES = {
    "default": {"URL": env.str("DATABASE_URL")},
    "billing": {"URL": env.str("BILLING_DATABASE_URL")},
}
DATABASE_ROUTING = {"invoices": "billing"}
```

The same routing decides two things, so they can never disagree:

- **Where the tables are.** A table tagged `info={"domain": "invoices"}`
  (the `startdomain` template tags them, and so does `table_for(...,
  domain=...)`) is created in `billing`. Untagged tables belong to
  `default`. Tag the tables of a domain generated before 0.3 before you
  route it.
- **Where units of work open sessions.** A `SqlAlchemyUnitOfWork` defined
  in the `invoices` package uses `billing`. A subclass may still set
  `database = "<target>"`, and `SomeUnitOfWork(database=...)` overrides
  it for one instance.

The outbox and the audit trail commit with the data they describe. So
`contrib.messaging` and `contrib.audit` create their tables in every
target that hosts a domain. `contrib.idempotency` keeps its table in its
own `DATABASE`. An app decides this in `DomainConfig.databases(router)`.

There are no transactions across databases. Keep each use case on one
target, and let other targets react to its events through the outbox.

`router`, from `domainkit.db.routing`, answers where data lives:
`router.target_for("invoices")` is `"billing"`,
`router.alias_for(target, shard_key)` is the alias of a target, and
`router.read_alias(alias)` is where read-only work on it goes.

## Sharding

A shard group is several SQL databases with the same schema. A **shard
key** (a tenant id, a customer id; a `str`, `int`, or `UUID`) picks one
of them:

```python
DATABASES = {
    "default": {"URL": env.str("DATABASE_URL")},
    "accounts_0": {"URL": env.str("ACCOUNTS_0_URL")},
    "accounts_1": {"URL": env.str("ACCOUNTS_1_URL")},
}
SHARDS = {"accounts": {"DATABASES": ["accounts_0", "accounts_1"]}}
DATABASE_ROUTING = {"accounts": "accounts"}
```

```python
async with AccountsUnitOfWork(shard_key=tenant_id) as uow:
    ...  # one database, chosen by tenant_id
```

A unit of work on a group without a shard key fails at once, and so does
a shard key on a plain alias. With [tenancy](tenancy.md), a unit of work
given no `shard_key` takes the current tenant's. Choose a key that keeps
every use case on one shard.

**A key is placed by its text**, `str(key)`, and that placement is frozen
for 1.x ([ADR 0125](adr/0125-freeze-the-public-api-for-1-0-renaming-what-was-never-published.md)).
`5` and `"5"` land together. So do a `UUID` and its canonical text (lower
case, with hyphens), but not its upper-case or unhyphenated text. Shard
on the typed id the domain holds, not on text a caller sent.

**How keys map.** A key hashes (BLAKE2b, so every process agrees) to one
of `BUCKETS` buckets, 4,096 by default. Each bucket belongs to one alias:
equal ranges, in list order, unless `RANGES` says otherwise. `PINNED`
sends specific keys to an alias, such as a large tenant to a database of
its own. `BUCKETS` must never change once the group holds data.

```python
from domainkit.db.routing import bucket_for, router

bucket_for("acme", 4096)  # 641
router.shards("accounts").alias_for("acme")  # 'accounts_0'
router.shards("accounts").owner_of(4095)  # 'accounts_1'
```

| `SHARDS` group key | Default | What it does |
| --- | --- | --- |
| `DATABASES` | (required) | The group's SQL aliases. `makemigrations` compares against the first. |
| `BUCKETS` | 4096 | How many hash buckets, 1 to 1,048,576. Never change it once the group holds data. |
| `RANGES` | `{}` | Inclusive bucket ranges per alias. Default: equal ranges, in list order. They must cover every bucket once. |
| `PINNED` | `{}` | Shard keys (as text) always routed to an alias of the group. |

**Growing a group.** Add a database, and give it some buckets:

1. Add the alias to `DATABASES` and to the group, then `migrate` it.
2. Pick bucket ranges to move. `bucket_for(key, buckets)` and
   `router.shards("accounts")` show where each key lives.
3. Copy those buckets' rows to the new database. Write to both while you
   copy, then compare.
4. Change `RANGES` to give the buckets to the new alias, deploy, and
   delete the copies from the old one.

**Across shards.** `await for_each_shard("accounts", fn)`, from
`domainkit.db.routing`, runs `fn(alias)` on every database of the group
at once, for reports and maintenance. Each shard has its own outbox and
audit trail. `runworker` runs them all, and `runworker --database
accounts` only the group's.

To shard inside the database instead, use a group of one alias that
points at a Citus coordinator. It works without framework changes.

## Migrations per target

A migration is a change to the tables, generated from your classes
(`makemigrations`) and applied (`migrate`). Each target has its own
history of revisions:

```text
migrations/              default
migrations/billing/      the billing alias
migrations/accounts/     a shard group: one history for all its databases
```

```bash
python manage.py makemigrations -m "add invoices"   # a revision per changed target
python manage.py migrate                            # every database of every target
python manage.py migrate --database billing         # one target (or one shard alias)
python manage.py showmigrations --database accounts # each shard of a group
python manage.py migrate 0003_ab12 --database billing --downgrade
```

- A revision id belongs to one target. So `migrate <revision>` needs
  `--database` when there are several targets. `head`, `heads`, and
  `base` work everywhere.
- `check` refuses several heads in any target.
- The pytest plugin migrates every target, and turns each in-memory
  SQLite database into a file of its own.
- `MIGRATIONS_DIR` holds the revisions of `default`. Any other target
  uses `MIGRATIONS_DIR/<target>`.

Moving a domain to another target is a settings change plus a migration.
Its existing rows are yours to copy.

| Command | What it does |
| --- | --- |
| `makemigrations --check` | Fails if a revision is missing (for CI). |
| `makemigrations --merge` | Joins two heads. |
| `makemigrations --empty` | Writes an empty revision, for a data migration (below). |
| `migrate --plan` | Lists the revisions that would run. |
| `migrate --sql` | Prints their SQL, rendered offline. |
| `migrate --fake` | Records a revision without running it, for a schema made another way. |
| `migrate --downgrade` | Reverts down to a revision. Staging and production also need `--allow-data-loss`. |
| `migrate --tenant NAME` | Migrates a tenant's schema ([tenancy](tenancy.md#a-schema-per-tenant-postgresql)). |
| `squashmigrations` | Replaces a run of revisions with one. |
| `inspectdb` | Prints aggregates for the tables of an existing database, to start from. |

[Commands](reference/commands.md) has every option.

### Data migrations

A change to rows, not to tables (Django's `RunPython` and `RunSQL`), is a
revision of its own. `makemigrations --empty` writes one for you to fill
in, after the schema change it follows:

```bash
python manage.py makemigrations --empty -m "split full names"
```

Its `upgrade()` runs inside the migration's transaction. SQL goes through
`op.execute`. Work in Python reads and writes through `op.get_bind()`,
with a table described in the revision itself, as it is at that point in
history. Never use the domain's classes there: they keep changing after
the revision.

```python
import sqlalchemy as sa
from alembic import op

people = sa.table(
    "crm_people", sa.column("id"), sa.column("name"), sa.column("given_name")
)


def upgrade() -> None:
    """Fill given_name from the first word of name."""
    connection = op.get_bind()
    for row in connection.execute(sa.select(people.c.id, people.c.name)):
        connection.execute(
            people.update()
            .where(people.c.id == row.id)
            .values(given_name=row.name.split(" ", 1)[0])
        )


def downgrade() -> None:
    """Nothing to undo: name is kept."""
```

Change a large table in batches, with a command run after the deploy,
not in one migration that holds its lock.

## Partitioning

Tables that only grow (events, readings, logs) can be split by time on
PostgreSQL. Queries that filter on the time column skip the other
partitions. Old data leaves by dropping a partition, not by deleting
rows.

```python
from sqlalchemy import Column, Integer, Table, Uuid

from domainkit.db import ExactDecimal, UTCDateTime, metadata
from domainkit.db.partitioning import Interval, partition_by_range

readings = Table(
    "metering_readings",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("taken_at", UTCDateTime, primary_key=True),
    Column("value", ExactDecimal(12, 3), nullable=False),
    Column("version", Integer, nullable=False),  # an aggregate's table needs it
    info={"domain": "metering"},
)
partition_by_range(readings, "taken_at", Interval.MONTH, premake=3, retain=24)
```

| `partition_by_range` argument | Default | What it does |
| --- | --- | --- |
| `column` | (required) | A date or timestamp column of the primary key. |
| `interval` | (required) | `Interval.DAY`, `WEEK` (from Monday), `MONTH`, or `YEAR`, in UTC; or its text. |
| `premake` | 3 | Future partitions kept ready, beyond the current one. |
| `retain` | `None` | Past partitions that `partitions --prune` keeps. None keeps everything. |

- PostgreSQL needs the partition column in the primary key (and in every
  unique constraint), so the key above is `(id, taken_at)`. The
  declaration fails when it is not.
- Partitions are named `<table>_p<YYYYMMDD>`, and each covers
  `[start, next start)`. There is no default partition: a row outside
  every partition fails.
- On SQLite the table is a plain table, so tests need nothing extra.

`migrate` creates the current partition and `premake` future ones. Run
`python manage.py partitions` on a schedule (a cron job, or a Kubernetes
CronJob), at least once per period, so they keep coming. Run
`partitions --prune` to drop partitions older than `retain` periods.
`--dry-run` prints the SQL.

- Only partitions named by the framework are ever dropped.
- `makemigrations` ignores partitions.
- Maintenance holds a PostgreSQL advisory lock, so replicas of the
  service that migrate at the same time take turns.

An existing table cannot be partitioned in place. Create the partitioned
table, copy the rows, and swap the names in a migration.

## Fixtures

A fixture is a JSON file of rows. `dumpdata` writes one, and `loaddata`
loads one:

```bash
python manage.py dumpdata catalog --output catalog.json --indent 2
python manage.py loaddata catalog.json --dry-run
python manage.py loaddata catalog.json
```

- `loaddata` says first what each table gets, and how many of its rows
  are stored already. A row whose primary key is stored is refused
  without `--replace`.
- Guarded tables (users, permissions, grants) need `--include-guarded`.
- Staging and production refuse without `--force` and a named
  `--operator`. Each load is audited.
- Encrypted columns are written sealed, unless `dumpdata --decrypt`.
  Amounts are written as text ([money](money.md#in-tests-and-fixtures)).
- `flush` deletes every row but the audit trail's and the guarded
  tables', then runs the `migrated` hooks again.

[Commands](reference/commands.md) has every option of `dumpdata`,
`loaddata`, `flush`, and `seed`.

## Ledgers (immudb)

A ledger keeps every revision of every key. The client checks a
cryptographic proof with every read and every write: if the server's
history changes, the next proof fails. Install the extra, and add the
ledger to `LEDGERS`:

```bash
pip install "domainkit[immudb]"
```

```python
LEDGERS = {
    "evidence": {
        # immudb://writer:pw@immudb:3322/evidence
        "URL": env.str("EVIDENCE_LEDGER_URL"),
        "TLS_CA": BASE_DIR / "certs" / "immudb-ca.pem",
        "SERVER_SIGNING_KEY": BASE_DIR / "certs" / "immudb-signing.pub",
    },
}
# Evidence keys hide subjects with this secret; required in deployments.
EVIDENCE = {"SUBJECT_KEY": env.str("EVIDENCE_SUBJECT_KEY")}
```

| `LEDGERS` option | Default | What it does |
| --- | --- | --- |
| `URL` | (required) | `immudb://user:password@host:port/database`, or `memory://` for tests. |
| `TLS_CA` | `None` | The CA certificate of the server. Required in staging and production. |
| `SERVER_SIGNING_KEY` | `None` | The server's public signing key, to verify its signed states. Required in staging and production. |
| `TIMEOUT` | `10.0` | Seconds a call may take before `LedgerUnavailable`. |

Prepare immudb once:

```bash
immudb --certificate server.crt --pkey server.key --signingKey signing.key
immuadmin login immudb
immuadmin database create evidence
immuadmin user create writer readwrite evidence
openssl ec -in signing.key -pubout -out immudb-signing.pub
```

### Using a ledger

Use ledgers through `domainkit.contrib.evidence`
([evidence](contrib/evidence.md#evidence-records-that-provably-never-change)),
or directly through the `Ledger` port. `ledgers` (a `LedgerHandler`)
makes each configured ledger on first use, and keeps it for the process:

```python
from domainkit.ledger import ledgers

evidence = ledgers["evidence"]
print(await evidence.append(b"key", b"value"))
print(await evidence.read(b"key", revision=1))
latest = await evidence.read(b"key")
print(await evidence.append(b"key", b"next", unchanged_since=latest.transaction))
await evidence.create(b"key", b"again")
```

```text
LedgerWrite(key=b'key', transaction=1)
LedgerEntry(key=b'key', value=b'value', transaction=1, revision=1)
LedgerWrite(key=b'key', transaction=2)
LedgerKeyExists: b'key' is already written.
```

| `Ledger` method | What it does |
| --- | --- |
| `append(key, value, *, unchanged_since=None)` | Adds a revision. With `unchanged_since`, the transaction of the revision read (0 for a key found unwritten), it writes only if no revision came after it; else `LedgerChanged`. |
| `create(key, value)` | Writes only a new key; else `LedgerKeyExists`. |
| `read(key, revision=None)` | The latest (or the given) revision, a `LedgerEntry`; else `LedgerKeyNotFound`. |
| `revision_count(key, *, limit)` | How many revisions the key has, counting up to `limit`. |
| `keys(prefix, *, limit)` | Keys that start with `prefix`, in key order. |
| `check()` | Raises when the ledger cannot serve (the readiness probe). |
| `close()` | Releases clients and threads; the ledger reconnects on next use. |

`create`, and `append` with `unchanged_since`, are one step in the
ledger. So of several writers racing for a key, exactly one succeeds, and
a retried or late write lands at most once. Audit anchors are written
this way. A `LedgerWrite` has `key` and `transaction`. A `LedgerEntry`
also has `value` and `revision` (1 for the first write of the key).

Tests use `LEDGERS = {"evidence": {"URL": "memory://"}}`: a
`MemoryLedger`, the same port in memory, without proofs.

### Trust in the ledger

- **Deployments.** In staging and production, ledgers need `TLS_CA` and
  `SERVER_SIGNING_KEY`, and `memory://` is refused. Evidence also needs
  `EVIDENCE.SUBJECT_KEY`
  ([evidence](contrib/evidence.md#evidence-records-that-provably-never-change)).
- **Readiness.** `/ready` checks every ledger (`ledger:<alias>`), on a
  thread and a client of its own, so a probe never waits behind a slow
  write.
- **One state.** A ledger's clients verify every step from one state, the
  ledger's. The work and the probe share it, a reconnect keeps it, and
  each advance is saved. So a process starts from where the ledger was
  last proved. With `domainkit.contrib.audit` installed, the state is
  saved in `AUDIT.DATABASE` (`domainkit_ledger_states`, one row per
  server and database; run `makemigrations` and `migrate`), which every
  process and host shares. Without it, the state is kept in memory, until
  the process ends. Only the very first contact trusts the server's state
  as it is.
- **Replicas** of the service share that row. `/ready` reads it again, so
  a replica starts from a later state another one proved. A save
  replaces only the state the replica last read or saved there. When
  another replica saved one since, the two are first proven to lie on one
  chain (an immudb dual proof). So a replica that kept writing on a
  rolled-back or forked server is refused there, and never saves the fork
  over what the others proved (SEC-10b-5).
- **Rollbacks and forks.** A server whose state is behind the saved one
  (rolled back to an older copy) is refused with `LedgerRolledBack`. One
  with another digest at the same transaction (forked) is refused with
  `LedgerTampered`. Every call and `/ready` refuse it, whatever its
  signature says. If you restored the ledger from a backup on purpose,
  delete its row from `domainkit_ledger_states`, so the next contact
  trusts it again ([ADR
  0011](adr/0011-immudb-ledger-for-evidence.md#amendment-2026-10-26-010-trust-that-outlives-a-client)).
- **Timeouts.** A call that takes longer than `TIMEOUT` is given up on
  with `LedgerUnavailable`. But the SDK cannot interrupt it: a write may
  still land afterwards. `create`, and `append(..., unchanged_since=...)`,
  make a retried or late write land at most once. Shutdown waits up to
  `TIMEOUT` for a busy client, which then closes when its call returns.
- **Speed.** Calls run on one thread per ledger, in order. On a laptop
  (immudb in Docker, synced, with TLS and signed states) that was about
  70 verified writes and 900 verified reads per second per process.
  immudb's synced commits set the write rate. Keep records small
  (`EVIDENCE.MAX_BYTES`). Store documents elsewhere, and record their
  SHA-256 digests.

`domainkit.ledger.trust` holds where the states are kept: `StateStore`,
`MemoryStateStore` (the default, this process only), `TrustedState`, and
`use_states(store)` for an installed app to register its store, in its
`DomainConfig.ready()`. `registered_states()` returns the store an
installed app registered, or None.

## Hooks, models, and the database clock

Three smaller modules serve the apps that build on the database.

**Hooks** (`domainkit.db.hooks`). A domain registers them in
`DomainConfig.ready()`:

```python
from domainkit.apps import DomainConfig
from domainkit.db.hooks import on_stage


class LedgerConfig(DomainConfig):
    def ready(self) -> None:
        on_stage(write_postings)
```

| Hook | Runs |
| --- | --- |
| `on_stage` | In a committing `SqlAlchemyUnitOfWork`, in its transaction, to add rows (the outbox, the audit trail). Raising rolls it back. |
| `on_committed` | After it committed. A failure is logged and reported, and the unit of work carries on. |
| `on_stage_transaction`, `on_transaction_committed` | The same, for a transaction the framework commits outside a unit of work. |
| `on_migrated` | After `migrate` upgraded a database, in one transaction on the schema owner's connection. |

[Extension points](reference/extension-points.md) has their signatures.

**Installed models** (`domainkit.db.models`). Every class mapped onto a
table of an installed domain is a model, named by its key
`(domain label, model)`: `("billing", "invoice")`. The keys name
permissions (`billing.change_invoice`).

| Name | What it gives |
| --- | --- |
| `installed_models()` | Every `InstalledModel` (`domain`, `cls`, and its options). |
| `model_key(cls)` | The `(domain, model)` of a mapped class, or `LookupError`. |
| `model_options(cls)` | A `ModelOptions` read from `cls.Meta`: `verbose_name`, `default_permissions`, `permissions`. |
| `DEFAULT_PERMISSIONS` | `("add", "change", "delete", "view")`: the permissions every model gets unless `Meta` says otherwise. |

```python
class Invoice(AggregateRoot[InvoiceId]):
    class Meta:
        verbose_name = "invoice"
        permissions = (("approve_invoice", "Can approve invoice"),)
```

**The database clock** (`domainkit.db.dbclock`). Hosts that share a
database share its rows' times. On PostgreSQL the database server
decides when shared leases and keys expire: `now(dialect)` and
`at(dialect, seconds)` give `statement_timestamp()` in SQL. On SQLite,
and under a frozen clock, they give `domainkit.clock`'s time instead.
`uses_database(dialect)` says which one decides. A `Moment` is either
kind of time. The outbox and idempotency keys use it.

## Testing

- The pytest plugin's `database` fixture migrates every target. Its `db`
  fixture (which `client` uses) puts every database back as `migrate`
  left it, before each test. Point the test settings at PostgreSQL to
  test partitions and locking for real.
- In-memory repositories answer queries as the database does
  ([queries.md](queries.md#what-a-query-means-everywhere)). So use cases
  can be tested without a database: `in_memory(CatalogUnitOfWork)`
  ([units of work](#units-of-work-and-their-repositories)).
- domainkit's own suite runs its database tests on SQLite, and on
  PostgreSQL when `DOMAINKIT_TEST_POSTGRES_URL` is set. Its immudb tests
  run when `DOMAINKIT_TEST_IMMUDB_URL` is set. CI sets both.

## Options

| Setting | Default | What it does |
| --- | --- | --- |
| `DATABASES` | `{}` | SQL databases by alias. |
| `DATABASE_ROUTING` | `{}` | Domain label to target (an alias or a shard group). |
| `SHARDS` | `{}` | Shard groups by name. |
| `LEDGERS` | `{}` | Ledgers by alias. |
| `MIGRATIONS_DIR` | `<project>/migrations` | Revisions of `default`; other targets use a folder inside it. |

The options of each `DATABASES` entry:

| Option | Default | What it does |
| --- | --- | --- |
| `URL` | (required) | `dialect+driver://user:password@host/name`. Hidden in reprs and errors. |
| `MIGRATION_URL` | `None` | The schema owner's URL, for `migrate`. Default: `URL`. |
| `ECHO`, `LOG_PARAMETERS` | `False` | Log every statement; show bound values in logs (development only). |
| `SSLMODE`, `SSLROOTCERT`, `SSLCERT`, `SSLKEY` | `None` | TLS to the server. |
| `STATEMENT_TIMEOUT`, `LOCK_TIMEOUT`, `IDLE_IN_TRANSACTION_TIMEOUT` | `30.0`, `10.0`, `60.0` | PostgreSQL's timeouts, in seconds. |
| `CONNECT_TIMEOUT`, `COMMAND_TIMEOUT` | `10.0`, `None` | The driver's timeouts, in seconds. |
| `PGBOUNCER` | `False` | The URL points at PgBouncer in transaction mode. |
| `POOL_SIZE`, `MAX_OVERFLOW`, `POOL_TIMEOUT`, `POOL_RECYCLE` | `None` | The pool's size and clocks (SQLAlchemy's defaults when None). |
| `POOL_PRE_PING` | `True` | Test a connection as it leaves the pool. |
| `CONNECT_ARGS` | `{}` | Extra driver arguments. |
| `REPLICAS` | `()` | Aliases of read replicas. |
| `TEST_NAME` | `None` | The name of the test database. |

[Backends](reference/backends.md#databases-and-migrations) has the full
rows, with their bounds.

## Errors and refusals

| Error | When | Over HTTP |
| --- | --- | --- |
| `DuplicateValue` | A unique constraint or primary key refuses a value. | 409 `duplicate_value` (or the subclass's code) |
| `ConcurrentUpdate` | A stale copy is written over a newer version. | 409 `concurrent_update` |
| `CheckFailed` | An aggregate breaks one of its `Meta.checks`. | 422 `check_violation` |
| `LockNotAvailable` | `nowait=True`, and the lock is held. | 409 `lock_not_available` |
| (database) | A serialization failure, a deadlock, or a lock past `LOCK_TIMEOUT`. | 503 `database_busy` |
| `ReadOnlyError` | A read-only unit of work commits or writes. | 500 (a bug) |
| `ImproperlyConfigured` | An aggregate's table without `version`; a missing alias; a pooler that drops read-only settings. | (at startup) |
| `ValueError` | `isolation=` on a read-only unit of work; `reads=` on one that writes; a bad `Retry`. | |
| `TypeError` | `table_for` meets a type it does not cover; an amount times an amount. | (at import) |
| `DatabaseAccessRefused` | A test reaches the databases without asking. | |
| `LedgerKeyExists`, `LedgerChanged`, `LedgerKeyNotFound` | A ledger write or read that the ledger refuses. | |
| `LedgerTampered`, `LedgerRolledBack`, `LedgerUnavailable` | A proof fails, the ledger went back, or it cannot be reached. | 503 from `/ready` |

Every ledger error is a `LedgerError`.

## Common mistakes

- **A hand-written table without `version`.** `map_imperatively` refuses
  it. Add the column, or pass `versioned=False` on purpose.
- **Floats or `Numeric` for amounts.** SQLite loses digits. Use `Money`,
  or `ExactDecimal` with `Digits`.
- **One use case across two databases.** There are no transactions
  across databases. Keep the use case on one target, and react to its
  events on the other.
- **Locking source, then target.** Two transfers in opposite directions
  can deadlock. Use `get_many_for_update`.
- **`run` around work that calls a payment provider.** The call would
  happen again. Defer it to a task.
- **Changing `BUCKETS`.** Every key moves. Grow a group with `RANGES`.

## See also

- [Queries](queries.md): what repositories find, and how.
- [Validation](validation.md): the markers that size columns.
- [Money](money.md): amounts and their two columns.
- [Tenancy](tenancy.md): a database or a schema per tenant.
- [Backends](reference/backends.md): every database setting.
- [Commands](reference/commands.md): `makemigrations`, `migrate`,
  `partitions`, `dumpdata`, `loaddata`.
- [Testing](testing.md): test databases and their fixtures.
