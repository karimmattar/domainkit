# Databases: several kinds, routing, replicas, shards, partitions, ledgers

A project can use several databases of two kinds, each in its own setting:

| Kind | Setting | URL | For | Migrated | In units of work |
| --- | --- | --- | --- | --- | --- |
| **SQL** | `DATABASES` | `postgresql+asyncpg://…` (`sqlite+aiosqlite://…` for development and tests) | Business data: read, write, transactions | Yes | Yes |
| **Ledger** | `LEDGERS` | `immudb://…` (`memory://` for tests) | Evidence: write once, read many, verified | No | No |

The decisions behind this page are ADRs
[0008](adr/0008-multiple-databases-and-routing.md),
[0009](adr/0009-sharding-with-hash-buckets.md),
[0010](adr/0010-postgresql-range-partitioning.md), and
[0011](adr/0011-immudb-ledger-for-evidence.md). Settings are listed in
[reference/backends.md](reference/backends.md#databases-and-migrations).

Querying repositories, pages, filters, bulk changes, soft delete, row
locks, and savepoints are in [queries.md](queries.md). Read replicas and
tables derived from domain classes follow ADR
[0024](adr/0024-replicas-and-derived-tables.md). Isolation levels, work
run again after a lost race, and locks are under
[Transactions](#transactions) (ADR
[0092](adr/0092-retry-units-of-work-choose-isolation-and-lock-in-order.md)).

## Routing domains to databases

`DATABASE_ROUTING` maps a domain label to a **target**: a SQL alias, or a
shard group. Domains not listed use `default`.

```python
DATABASES = {
    "default": {"URL": env.str("DATABASE_URL")},
    "billing": {"URL": env.str("BILLING_DATABASE_URL")},
}
DATABASE_ROUTING = {"invoices": "billing"}
```

The same routing decides two things, so they can never disagree:

- **Where the tables are.** A table tagged `info={"domain": "invoices"}`
  (the `startdomain` template tags them) is created in `billing`. Untagged
  tables belong to `default`: tag the tables of a domain generated before
  0.3 before routing it.
- **Where units of work open sessions.** A `SqlAlchemyUnitOfWork` defined in
  the `invoices` package uses `billing`. A subclass may still set
  `database = "<target>"`, and `SomeUnitOfWork(database=...)` overrides it
  per instance.

The outbox and the audit trail commit with the data they describe, so
`contrib.messaging` and `contrib.audit` create their tables in every target
that hosts a domain. `contrib.idempotency` keeps its table in its own
`DATABASE`. An app decides this in `DomainConfig.databases(router)`.

There are no transactions across databases. Keep each use case on one
target, and let other targets react to its events through the outbox.

## Migrations per target

Each target has its own revision history:

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

A revision id belongs to one target, so `migrate <revision>` needs
`--database` when there are several targets; `head`, `heads`, and `base`
work everywhere. `check` refuses multiple heads in any target. The pytest
plugin migrates every target, and turns each in-memory SQLite database into
its own file.

Moving a domain to another target is a settings change plus a migration, but
its existing rows are yours to copy.

## Sharding

A shard group is several SQL databases with the same schema. A **shard key**
(a tenant id, a customer id; `str`, `int`, or `UUID`) picks one of them:

```python
DATABASES = {
    "default": {...},
    "accounts_0": {"URL": ...},
    "accounts_1": {"URL": ...},
}
SHARDS = {"accounts": {"DATABASES": ["accounts_0", "accounts_1"]}}
DATABASE_ROUTING = {"accounts": "accounts"}
```

```python
async with AccountsUnitOfWork(shard_key=tenant_id) as uow:
    ...  # one database, chosen by tenant_id
```

A unit of work on a group without a shard key fails at once, and so does a
shard key on a plain alias. Choose a key that keeps every use case on one
shard.

**How keys map.** A key hashes (BLAKE2b, so every process agrees) to one of
`BUCKETS` buckets (4096 by default), and each bucket belongs to one alias:
equal ranges in list order unless `RANGES` says otherwise. `PINNED` sends
specific keys to an alias, such as a large tenant on its own database.
`BUCKETS` must never change once the group holds data.

**Growing a group.** Add a database and give it some buckets:

1. Add the alias to `DATABASES` and the group, then `migrate` it.
2. Pick bucket ranges to move. `domainkit.db.routing.bucket_for(key,
   buckets)` and `router.shards("accounts")` show where each key lives.
3. Copy those buckets' rows to the new database. Write to both while
   copying, then compare.
4. Change `RANGES` to give the buckets to the new alias, deploy, and delete
   the copies from the old one.

**Across shards.** `await for_each_shard("accounts", fn)` runs `fn(alias)`
on every database of the group at once, for reports and maintenance. Each
shard has its own outbox and audit trail; `runworker` runs them all, or
`runworker --database accounts` only the group's.

If you would rather shard inside the database, a group of one alias pointing
at a Citus coordinator works without framework changes.

## Read replicas

An alias may name read replicas, each an alias of its own:

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

- **Reads.** A read-only unit of work opens its session on a replica of the
  alias its routing gives (a shard's alias works too); with several, units
  of work take turns. Without `REPLICAS` it reads the alias itself.
- **Writes** always go to the primary: a read-only unit of work refuses to
  commit (`ReadOnlyError`), and on PostgreSQL its transaction is `READ
  ONLY`, so a write that skips the unit of work is refused as well. A
  replica is never a target: routing a domain to one, naming one as a
  unit of work's `database`, or migrating one is refused. Replication
  keeps it migrated.
- **Lag.** A replica may be behind: a request that writes and then reads
  what it wrote should read in its writable unit of work.
- **Settings.** A replica is another alias than `default`, of one primary,
  outside every shard group, with no replicas of its own. Each is a
  `DATABASES` entry, so the deployment checks (TLS, `ECHO`,
  `LOG_PARAMETERS`) and `/ready` cover it, and it has its own pool.
- **Tests.** Point the replica at the primary's URL, or leave `REPLICAS`
  out of the test settings: two SQLite files do not replicate.

## Connection pools

Each alias has a pool of connections per event loop (`POOL_SIZE`,
`MAX_OVERFLOW`, `POOL_TIMEOUT`, `POOL_RECYCLE`), made on first use.

- **Testing connections.** With `POOL_PRE_PING`, on by default, a connection
  is tested as it leaves the pool, so one the server closed (a restart, a
  failover) is replaced before a request uses it. On asyncpg the test is
  one round trip, a prepared `;` outside any transaction; SQLAlchemy wraps
  it in `BEGIN` and `ROLLBACK`, three round trips, which only PgBouncer
  needs, and keeps. Keep it on: without it, the first request on each
  stale connection fails.
- **Reads on their own.** The first `Reads.STATEMENTS` unit of work on an
  alias opens a second pool for that alias, sized like the first, whose
  connections PostgreSQL holds read-only from the start
  ([queries.md](queries.md#how-read-only-work-reads)). Each new connection
  is checked once: if its transactions are not read-only, because a
  pooler between you and PostgreSQL dropped the startup settings, it is
  refused with `ImproperlyConfigured`. Through asyncpg the pool does not
  test connections: asyncpg notices when the server closes one, and it is
  replaced before a statement is sent on it. psycopg notices only when it
  next sends one, so through psycopg the pool pings. A connection is
  reset before it goes to the next caller, unless it ran only plain
  `SELECT` statements. A process may then hold up to twice the alias's
  connections, so count them when you size the server's
  `max_connections`. `pool_stats()` and the `db_pool_connections` metric
  name it `<alias>:reads`, and readiness checks it as
  `database:<alias>:reads` once it is open. A replica read that way gets
  one too.

ADR [0061](adr/0061-read-without-a-transaction-and-ping-once.md) explains
the choices.

## Partitioning

Tables that only grow (events, readings, logs) can be split by time on
PostgreSQL. Queries that filter on the time column skip other partitions,
and old data leaves by dropping a partition instead of deleting rows.

```python
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

- PostgreSQL needs the partition column in the primary key (and in every
  unique constraint), so the key above is `(id, taken_at)`. The declaration
  fails when it is not.
- The column must be a date or a timestamp. Intervals are `day`, `week`
  (from Monday), `month`, and `year`, in UTC.
- Partitions are named `<table>_p<YYYYMMDD>` and cover `[start, next start)`.
  There is no default partition: a row outside every partition fails.
- On SQLite the table is a plain table, so tests need nothing extra.

`migrate` creates the current partition and `premake` future ones. Run
`python manage.py partitions` on a schedule (a cron job or a Kubernetes
CronJob, at least once per period) so they keep coming, and
`partitions --prune` to drop partitions older than `retain` periods.
`--dry-run` prints the SQL. Only partitions named by the framework are ever
dropped. `makemigrations` ignores partitions. Maintenance holds a PostgreSQL
advisory lock, so replicas that migrate at the same time take turns.

An existing table cannot be partitioned in place. Create the partitioned
table, copy the rows, and swap the names in a migration.

## Ledgers (immudb)

A ledger keeps every revision of every key, and the client checks a
cryptographic proof with every read and write: if the server's history
changes, the next proof fails. Install the extra and add the ledger to
`LEDGERS`:

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

Prepare immudb once:

```bash
immudb --certificate server.crt --pkey server.key --signingKey signing.key
immuadmin login immudb
immuadmin database create evidence
immuadmin user create writer readwrite evidence
openssl ec -in signing.key -pubout -out immudb-signing.pub
```

- In staging and production, ledgers need `TLS_CA` and
  `SERVER_SIGNING_KEY`, and `memory://` is refused. Evidence also needs
  `EVIDENCE.SUBJECT_KEY`
  ([contrib.md](contrib/evidence.md#evidence-records-that-provably-never-change)).
- `/ready` checks every ledger (`ledger:<alias>`), on a thread and a
  client of its own, so a probe never waits behind a slow write.
- A ledger's clients verify every step from one state, the ledger's: the
  work and the probe share it, a reconnect keeps it, and each advance is
  saved, so a process starts from where the ledger was last proved. With
  `domainkit.contrib.audit` installed it is saved in `AUDIT.DATABASE`
  (`domainkit_ledger_states`, one row per server and database, run
  `makemigrations` and `migrate`), which every process and host shares;
  without it, in memory, until the process ends. Only the very first
  contact trusts the server's state as it is.
- Replicas share that row. `/ready` reads it again, so a replica starts
  from a later state another proved; and a save replaces only the state
  the replica last read or saved there. When another replica saved one
  since, the two are proven to lie on one chain first (an immudb dual
  proof), so a replica that kept writing on a rolled-back or forked
  server is refused there, and never saves the fork over what the others
  proved (SEC-10b-5).
- A server whose state is behind the saved one (rolled back to an older
  copy) is refused with `LedgerRolledBack`, and one with another digest at
  the same transaction (forked) with `LedgerTampered`, by every call and
  by `/ready`, whatever its signature says. If you restored the ledger
  from a backup on purpose, delete its row from `domainkit_ledger_states`
  so the next contact trusts it again ([ADR
  0011](adr/0011-immudb-ledger-for-evidence.md#amendment-2026-10-26-010-trust-that-outlives-a-client)).
- A call that takes longer than `TIMEOUT` is given up on with
  `LedgerUnavailable`, but the SDK cannot interrupt it: a write may still
  land afterwards. `create`, and `append(..., unchanged_since=...)`, make a
  retried or late write land at most once. Shutdown waits up to `TIMEOUT`
  for a busy client, which then closes when its call returns.
- Calls run on one thread per ledger, in order. On a laptop (immudb in
  Docker, synced, TLS and signed states) that was about 70 verified writes
  and 900 verified reads per second per process; immudb's synced commits set
  the write rate. Keep records small (`EVIDENCE.MAX_BYTES`); store documents
  elsewhere and record their SHA-256 digests.

Use ledgers through `domainkit.contrib.evidence`
([contrib.md](contrib/evidence.md#evidence-records-that-provably-never-change)), or
directly through the `Ledger` port:

```python
from domainkit.ledger import ledgers

written = await ledgers["evidence"].append(b"key", b"value")
entry = await ledgers["evidence"].read(b"key", revision=1)
first = await ledgers["evidence"].create(b"new", b"value")  # LedgerKeyExists if written
latest = await ledgers["evidence"].read(b"key")
await ledgers["evidence"].append(
    b"key", b"next", unchanged_since=latest.transaction
)  # LedgerChanged if another revision came first
```

`append` adds a revision; `create` writes only a new key, in one step in
the ledger, so of several writers racing for a key exactly one succeeds.
`append` with `unchanged_since`, the transaction of the revision read (0 for
a key found unwritten), writes only if no revision came after it, also in
one step: audit anchors are written this way.

Tests use `LEDGERS = {"evidence": {"URL": "memory://"}}`: the same port, in
memory, without proofs.

## Tables from domain classes

`table_for` derives a table from an entity's dataclass fields and type
hints, so a field is declared once:

```python
from domainkit.db import map_imperatively, table_for
from domainkit.ddd import AggregateRoot, Digits, MaxLength


class Product(AggregateRoot[ProductId]):
    name: ProductName  # value object: ProductName(value: str)
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

| Hint | Column |
| --- | --- |
| `int` | `BigInteger` (`INTEGER` on SQLite); `version` is `Integer` |
| `str` | `String(n)` with `MaxLength(n)`, else `Text` |
| `bool`, `float` | `Boolean`, `Double` |
| `Decimal` | `ExactDecimal(p, s)`; it needs `Digits(p, s)` |
| `datetime`, `date`, `time` | `UTCDateTime`, `Date`, `Time` |
| `UUID` (or a `NewType` of one) | `Uuid` |
| `Money`, `Annotated[Money, Digits(p, s)]` | two columns, `<name>` (`ExactDecimal(p, s)`, default 18 and 4) and `<name>_currency` ([money.md](money.md#in-tables)) |
| `Currency` | `CurrencyCode`: the code |
| an `Enum` | `EnumValue`: the members' values, text or integers |
| a value object of one field | `ValueObjectType`: that field's column type |
| a class you registered | `register_column_type(Point, PointType())` |
| `X \| None` | the same, nullable; anything else is `NOT NULL` |

- `id` is the primary key. A `version` field (every aggregate has one)
  turns on optimistic locking. `deleted_at` comes with `SoftDeletable`.
  A hand-written table for an aggregate needs that column too (see
  [Versions](#versions)).
- Child entities (`list[OrderLine]`, `Address | None`) are not columns;
  map them as relationships (below).
- Columns in the arguments replace the derived one of the same name
  (`Column("sku", String(40, collation="C"), nullable=False)`); indexes,
  constraints, and other columns are added. A type the table does not cover
  is a `TypeError` naming the field.
- Enums are stored by value, so `ORDER BY` follows the values and a member
  may be renamed; changing a value needs a data migration. Value objects
  come back through their constructor, so their rules run on load.
- The result is an ordinary `Table` on `metadata`: `makemigrations` writes
  plain SQL types into revisions (`sa.String(length=5)` for an enum), and
  hand-written tables keep working beside derived ones.
- `MaxLength` and `Digits` say what storage needs; they do not check values.
  Check them in the domain, as value objects do.

## Child entities

An aggregate owns its child entities: they are loaded, saved, and deleted
with it. Each lives in a table of its own, with a foreign key to the
aggregate's, and is mapped as a relationship:

```python
from domainkit.db import child, children, map_imperatively, table_for


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
  one more query per page of aggregates, never a lazy load, which async
  sessions cannot do), saves new lines with it, and deletes a line removed
  from the list (`cascade="all, delete-orphan"`). `child` does the same for
  one entity. Pass `order_by` for a stable order, `collection_class=set`
  for a `set[...]` field, or any other `relationship()` option.
- Another aggregate is referenced by its id, not by a relationship, so
  each loads and locks on its own.
- Any other relationship given to `map_imperatively` that leaves `lazy` at
  SQLAlchemy's default, and the reverse side its `backref` makes, raises on
  an unloaded access (`lazy="raise"`): `'Order.customer' is not available
  due to lazy='raise'`. A lazy load needs I/O, which an attribute access in
  async code cannot do, so it would otherwise fail later with
  `MissingGreenlet`. Load it in the query (`selectinload(Order.customer)`),
  or pass `lazy="selectin"`. `lazy=True` asks for lazy loading on purpose,
  for `AsyncAttrs.awaitable_attrs` or code run with `session.run_sync`.
- `ON DELETE CASCADE` matters to bulk deletes (`delete_rows`) on
  PostgreSQL; SQLite enforces foreign keys only with `PRAGMA foreign_keys`.
- Queries reach into children with dotted names: `F("lines.sku") == "A-1"`.

## Versions

An aggregate's table has a `version` column. `map_imperatively` makes every
`UPDATE` and `DELETE` of the aggregate check and bump it, so a copy loaded
before someone else's change fails with `ConcurrentUpdate` (409
`concurrent_update`) instead of overwriting that change. Without it, ten
requests that each read a balance of 1000.00, take 10.00 off, and commit
all succeed and leave 990.00.

`table_for` always derives the column. A hand-written table must declare
it, and `map_imperatively` refuses an aggregate whose table has none:

```text
ImproperlyConfigured: billing.domain.model.Account is an aggregate, but its
table 'billing_accounts' has no 'version' column, so concurrent changes to
one row would overwrite each other without an error. ...
```

Add the column, with `server_default="0"` in the migration that adds it to
a table that already has rows:

```python
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

Or map it without optimistic locking on purpose, for an aggregate only one
writer changes, or one that is never changed after it is added:

```python
map_imperatively(Reading, readings, versioned=False)
```

A mapping that passes `version_id_col` (a column of another name) has
chosen already, and a subclass mapped with `inherits` shares its base's
version. Optimistic locking makes all but one of the concurrent writers
fail; to make them wait their turn instead, load the aggregate with
`get_for_update` (see [queries](queries.md)).

## Exact amounts

Hold money in the domain as `Money` ([money.md](money.md)): an exact
amount and its currency, stored in an `ExactDecimal` column and a code
column beside it. Store any other amount that must not drift in
`ExactDecimal(precision, scale)` columns rather than `Numeric`:

```python
from domainkit.db import ExactDecimal

Column("total", ExactDecimal(14, 2), nullable=False)  # up to 999,999,999,999.99
```

An aggregate's table also needs its [`version`](#versions) column, or
concurrent changes to an amount overwrite each other.

**As text.** Wherever domainkit writes an amount as text, it writes it in
fixed point (`domainkit.utils.decimals.decimal_text`): `Decimal("1E+3")`
is `"1000"`, `Decimal("0E-8")` is `"0.00000000"`, and `Decimal("-0.00")`
is `"0.00"`, with the value's own places and no sign on zero. That holds
in responses of `schema_for` schemas, CSV and NDJSON exports, outbox
events and task arguments, webhook payloads, audit details, evidence
facts, cursors, `dumpdata`, dynamic settings, privacy exports, and the
admin API's user fields, so the same amount has one text in every record.
The audit trail's pseudonyms of personal values hash that text too, so
equal amounts have one pseudonym. JSON strings stay strings. A value with more than 100 digits before or
after the point is no amount, and is written as `str()` does
(`1E+999999999`), so its text stays short. Your own response models can
write amounts the same way:
`Annotated[Decimal, PlainSerializer(decimal_text, return_type=str, when_used="json")]`.

SQLite has no decimal type. It keeps `NUMERIC` values as floats, which
hold 15 to 17 significant digits, so `12345678901234567.89` comes back as
`12345678901234568.00`, silently. Since SQLite is the default development
and test database, a project could pass its tests with amounts that
PostgreSQL would store differently.

- **Storage.** On PostgreSQL the column is `NUMERIC(precision, scale)`. On
  SQLite it holds whole minor units (cents for a scale of 2) as integers,
  which are exact. Revisions declare `NUMERIC` on both.
- **Values** are `Decimal` (or `int`), and come back as `Decimal` with the
  column's places: `Decimal("5")` is read as `Decimal("5.00")`. Floats are
  refused, and so are values with more places or more digits than the
  column holds (`Decimal("1.005")` in a scale of 2). Round them yourself,
  with the rule your domain needs, before storing them. Comparisons take
  values of any places and compare them exactly:
  `where(price > Decimal("9.995"))`, `price.in_(...)`, `price.between(...)`.
- **Precision** is at most 18 digits, any 18 of which fit SQLite's 64-bit
  integers: `ExactDecimal(18, 2)` holds up to 9,999,999,999,999,999.99.
- **Queries give the same results on both databases**: comparisons,
  `ORDER BY`, `in_`, `between`, `+` and `-`, `sum`, `min`, `max`, and
  multiplying or dividing by a number (`price * 2`, `price * Decimal("1.1")`,
  `price * quantity` for an integer column, `price / 4`). On SQLite, a
  product with a fraction and a quotient are computed in floating point
  before they come back as `Decimal`.
- **Amounts of different scales, and integer columns,** are added,
  subtracted, and compared in the same units (`price + fee`,
  `price > rate`, `price > quantity`): on SQLite the side with fewer places
  is scaled up. Put the amount first, or both amounts: `quantity + price`
  starts from the integer column, and SQLite would add a count to cents.
- **In Python instead:**
  - floor division (`price // 2` is refused);
  - arithmetic between two amount columns other than `+` and `-` (on SQLite
    `price * cost` multiplies cents by cents);
  - new amounts with more places than the column, such as
    `update(...).values(price=prices.c.price * Decimal("1.1"))`.
    PostgreSQL rounds them to the column's scale, and SQLite keeps the
    fraction of a cent, so compute them with your own rounding rule.

  - results past SQLite's 64-bit integers (9,223,372,036,854,775,807
    minor units): there a product or a scaled-up amount becomes a float,
    and `sum` fails with an overflow, where PostgreSQL computes exactly;
  - casts to text, which give minor units on SQLite.

  For `avg`, name the type: `func.avg(price, type_=price.type)`.

## Unique values

When a unique constraint or a primary key refuses a value, the unit of work
raises `DuplicateValue`, a `Conflict` that answers 409 with the code
`duplicate_value`. It does so at `commit()`, and also when an earlier flush
inside the `async with` block hits the constraint, whether explicit or a
query's autoflush. `constraint` and `fields` say which constraint refused
what. The message names the fields but never the values, which may be
personal data. Other integrity errors, such as a missing required value or
a broken foreign key, are left as they are.

To give a constraint its own code, name a `DuplicateValue` subclass in its
`info`, or in its column's when it covers one column:

```python
class EmailTaken(DuplicateValue):  # code: email_taken
    """Another account has that email."""


Column("email", String(254), unique=True, info={"duplicate": EmailTaken})
UniqueConstraint("tenant_id", "handle", info={"duplicate": HandleTaken})
```

The constraint is looked up in `domainkit.db.metadata`, by the name
PostgreSQL reports or by the table and columns SQLite reports. Checking
first, as `create_user` does for usernames, still gives a friendlier
message; the constraint settles two requests that both pass the check.
Auth's username and group name columns name `UsernameTaken` and
`GroupNameTaken`.

## Transactions

A SQL unit of work is one transaction. By default it runs at the
database's own isolation level, `READ COMMITTED` on PostgreSQL: each
statement sees what was committed before it began, `version` columns
refuse lost updates ([Versions](#versions)), and nothing is run again.
Work that keeps a rule across rows needs more than that. Two transfers
that each check "at least one account keeps a balance" and each change a
different account both commit: no row was changed twice, so no version
check fails, and the rule is broken (write skew).

### Isolation levels

A unit of work that writes chooses its level on the class, or when it is
made:

```python
from domainkit.db import Isolation, SqlAlchemyUnitOfWork


class LedgerUnitOfWork(SqlAlchemyUnitOfWork):
    isolation = Isolation.SERIALIZABLE


uow = OrdersUnitOfWork(isolation="repeatable read")
```

| Level | PostgreSQL | SQLite |
| --- | --- | --- |
| None (default) | the server's default, `READ COMMITTED` | as SQLite always does |
| `READ_COMMITTED` | each statement sees what was committed before it | as the default |
| `REPEATABLE_READ` | one snapshot; a row changed meanwhile cannot be changed (serialization failure) | as `SERIALIZABLE` |
| `SERIALIZABLE` | as if run one after another; one that could not have been fails (serialization failure) | `BEGIN IMMEDIATE`: they do run one after another |

On PostgreSQL the level goes into the transaction's `BEGIN`: no statement
or round trip more. On SQLite, `REPEATABLE_READ` and `SERIALIZABLE` begin
the transaction with `BEGIN IMMEDIATE` before its first statement, so such
units of work take the database's write lock first and wait for each
other (up to the driver's busy timeout, then "database is locked"); they
never fail on each other's changes. A read-only unit of work takes no
level: it says how it reads with `reads=` (`Reads.SNAPSHOT` reads one
snapshot), and `isolation=` with `readonly=True` is a `ValueError`.

### Running work again

At `REPEATABLE_READ` and `SERIALIZABLE`, PostgreSQL ends the losing
transaction with a serialization failure (SQLSTATE `40001`); at any level,
two transactions that wait for each other's locks end one of them with a
deadlock (`40P01`). Either is answered 503 `database_busy`. Running the
whole work again usually succeeds: `run` does that.

```python
from domainkit.ddd import Retry

await uow.run(lambda: transfer(command, uow), retry=Retry(attempts=5))
```

The work is a function of no arguments that does the whole job, entering
the unit of work itself, as an application service does. `run` never
re-runs part of an `async with` block: what a failed run wrote went with
its transaction, outbox messages, audit entries, and the
`Idempotency-Key`'s mark included, and the next run starts again from the
database. Events are published, and deferred tasks queued, only by the
commit that succeeds. In memory, the repositories are put back as they
were before the failed run.

- `Retry(attempts=3, backoff=0.02, max_backoff=1.0, retry_on=())`: at most
  `attempts` runs, 1 to 10. Before each next run, a random wait up to
  `backoff` seconds, doubled for each run so far, at most `max_backoff`
  (at most 60). `retry_on` adds errors of your own, such as
  `ConcurrentUpdate` to run again a lost optimistic race. `run(work)`
  without `retry` uses `Retry()`.
- Only serialization failures and deadlocks are run again (SQLite reports
  neither), and the `retry_on` errors. Any other error is raised at once.
- After the last run, the last failure is raised as it was, with a note
  saying how many runs there were. It is also raised, without another
  run, when this unit of work committed during the run (running it again
  would commit twice), and when the wait would pass the request's
  deadline.
- Under a frozen clock (`freeze_time`) the wait takes no time.
- Work that changes something outside its unit of work, such as a call to
  a payment provider, or another unit of work, must not be run again:
  leave it to a deferred task, which the outbox runs once.

`ResourceRouter.command_route(..., retry=Retry())` runs a command's service
that way ([resources.md](resources.md)): the check that the caller may
reach the aggregate, and `If-Match`, are made again in each run's
transaction, so a run after another committed sees its version.
`batch_route(..., retry=Retry())` does the same for each item, or, for an
atomic batch, runs the whole batch again. Routes do not retry unless they
say so.

### Locks

- `get_for_update(id)` locks one row, and `get_many_for_update(ids)`
  several, in one statement, by ascending id whatever the order of `ids`
  ([queries.md](queries.md#row-locks)). Two transfers that lock both their
  accounts that way take the locks in the same order: one waits for the
  other, and they cannot deadlock, where locking source then target can.
- `await uow.lock("payout:" + str(account_id))` holds a lock by name until
  the transaction ends, for work that has no row to lock. On PostgreSQL it
  is `pg_advisory_xact_lock` on `lock_key(name)`, a stable 64-bit hash of
  the name; `nowait=True` raises `LockNotAvailable` (409) instead of
  waiting, and a wait past `LOCK_TIMEOUT` is a 503 `database_busy`. On
  SQLite it is the database's write lock (`BEGIN IMMEDIATE`, unless the
  transaction wrote already). In memory, names are locked for the whole
  process. Take a lock before reading what it guards.

### A transfer of money

Both together, with `Money` ([money.md](money.md)) as the amount: lock the
two rows in one statement, by ascending id, change both, and let `run` do
it all again when the transaction loses a race. A balance of another
currency refuses the amount (`CurrencyMismatch`, 422):

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

`startaggregate wallets Wallet --field balance:money` writes this use
case, the two methods (`transfer_balance` refuses an overdraft), the
command, `POST /wallets/{wallet_id}/transfer-balance`, and their tests,
one of them a lost race ([resources.md](resources.md#generators)). Both
rows are locked, so the rule on each holds at `READ COMMITTED`; a rule
across rows that are not both locked needs `SERIALIZABLE`
([the payment recipe](recipes/payments.md)).

## Testing

- The pytest plugin's `database` fixture migrates every target, and its
  `db` fixture (which `client` uses) puts every database back as `migrate`
  left it before each test. Point the test settings at PostgreSQL to test
  partitions and locking for real.
- In-memory repositories answer queries as the database does
  ([queries.md](queries.md#what-a-query-means-everywhere)), so use cases
  can be tested without one.
- domainkit's own suite runs its database tests on SQLite, and on PostgreSQL
  when `DOMAINKIT_TEST_POSTGRES_URL` is set. Its immudb tests run when
  `DOMAINKIT_TEST_IMMUDB_URL` is set. CI sets both.
