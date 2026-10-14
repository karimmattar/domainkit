# 0010. Range-partition large tables in PostgreSQL

- Status: Accepted
- Date: 2026-10-14

## Context

Append-mostly tables (events, logs, evidence indexes, time series) grow
without bound. Deleting old rows bloats the table and holds locks, and
indexes on hundreds of millions of rows slow every write. PostgreSQL's
declarative partitioning splits a table into partitions by a key. Queries
that filter on the key skip other partitions, and expiring old data becomes
detaching a partition instead of deleting rows.

Partitions must exist before rows arrive. Options considered:

- **pg_partman** with pg_cron: mature, but an extension that managed
  PostgreSQL services do not all offer, and configuration outside the code.
- **Hand-written partitions in migrations**: they run out unless someone
  remembers to add more.
- **Framework-managed partitions**, declared on the table in code and
  created by a command and by `migrate`.

Alembic does not know about partitions. Autogenerate would propose dropping
every partition, because partitions are tables missing from the metadata.

## Decision

- `partition_by_range(table, column, interval, premake=3, retain=None)`
  declares a table range-partitioned by a timestamp column, per day, week,
  month, or year. On PostgreSQL the table is created with `PARTITION BY
  RANGE`; on SQLite it stays a plain table, so tests and development need
  nothing extra.
- The partition column must be part of the primary key, as PostgreSQL
  requires. The declaration fails at import otherwise.
- Partitions are named `<table>_p<YYYYMMDD>` (the period start, UTC), and
  cover `[start, next start)`. There is no default partition, so a row
  outside every partition fails loudly instead of piling up in a catch-all.
- `manage.py partitions` keeps `premake` future periods ready, and with
  `--prune`, detaches and drops partitions older than `retain` periods.
  `migrate` runs the creation step after each upgrade. Projects schedule the
  command (cron, a Kubernetes CronJob, or a periodic task).
- Migration autogenerate ignores tables that PostgreSQL reports as
  partitions.

## Consequences

- Every unique constraint on a partitioned table includes the partition
  column, which shapes primary keys (`(id, occurred_at)`).
- If maintenance stops for longer than `premake` periods, inserts fail. The
  `/ready` check does not cover this, so schedule the command and alert on
  its failure.
- Pruning is destructive and runs only with `--prune`, never from `migrate`.
- Existing tables are not partitioned in place. Converting one is a data
  migration (create the partitioned table, copy, swap), as PostgreSQL
  cannot alter a table into a partitioned one.
