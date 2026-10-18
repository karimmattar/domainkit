# Databases: several kinds, routing, shards, partitions, ledgers

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
[reference.md](reference.md#databases-and-migrations).

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

## Partitioning

Tables that only grow (events, readings, logs) can be split by time on
PostgreSQL. Queries that filter on the time column skip other partitions,
and old data leaves by dropping a partition instead of deleting rows.

```python
from domainkit.db import UTCDateTime, metadata
from domainkit.db.partitioning import Interval, partition_by_range

readings = Table(
    "metering_readings",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("taken_at", UTCDateTime, primary_key=True),
    Column("value", Numeric(12, 3), nullable=False),
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
  ([contrib.md](contrib.md#evidence-records-that-provably-never-change)).
- `/ready` checks every ledger (`ledger:<alias>`).
- Each process trusts the server's state when it first connects, then
  verifies every later step against it. The signing key is what protects
  across restarts.
- Calls run on one thread per ledger, in order. On a laptop (immudb in
  Docker, synced, TLS and signed states) that was about 70 verified writes
  and 900 verified reads per second per process; immudb's synced commits set
  the write rate. Keep records small (`EVIDENCE.MAX_BYTES`); store documents
  elsewhere and record their SHA-256 digests.

Use ledgers through `domainkit.contrib.evidence`
([contrib.md](contrib.md#evidence-records-that-provably-never-change)), or
directly through the `Ledger` port:

```python
from domainkit.ledger import ledgers

written = await ledgers["evidence"].append(b"key", b"value")
entry = await ledgers["evidence"].read(b"key", revision=1)
```

Tests use `LEDGERS = {"evidence": {"URL": "memory://"}}`: the same port, in
memory, without proofs.

## Testing

- The pytest plugin's `database` fixture migrates every target. Point the
  test settings at PostgreSQL to test partitions and locking for real.
- domainkit's own suite runs its database tests on SQLite, and on PostgreSQL
  when `DOMAINKIT_TEST_POSTGRES_URL` is set. Its immudb tests run when
  `DOMAINKIT_TEST_IMMUDB_URL` is set. CI sets both.
