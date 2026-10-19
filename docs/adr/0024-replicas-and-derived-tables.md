# 0024. Read from replicas in read-only work; derive tables from domain classes

- Status: Accepted
- Date: 2026-10-19

This record holds two storage decisions made together for the data layer.

## Context

**Replicas.** Read-heavy services want reads on replicas, and writes must
stay on the primary. Django routes reads with `DATABASE_ROUTERS.db_for_read`,
which sends any read to a replica, including one inside a transaction that
then writes: the transaction may read stale data and act on it. A
domainkit unit of work is one transaction on one database (ADR 0008).

**Tables.** Each aggregate was declared twice: as a dataclass in the
domain, and as a `Table` in infrastructure, with a hand-written
`TypeDecorator` per value object. The two drifted silently, and enums were
left to SQLAlchemy's `Enum`, which stores member names and orders them
differently on each database.

## Decision

**Replicas.**

- A `DATABASES` entry names its replicas (`REPLICAS`), each an alias of
  its own, so TLS, timeouts, pools, deployment checks, and `/ready` apply
  to it unchanged.
- Only a read-only unit of work (`readonly=True`, or a class with
  `readonly = True`) reads from a replica; units of work take turns on
  several. Without replicas it reads the primary. A read-only unit of work
  refuses to commit, and its PostgreSQL transaction is `READ ONLY`.
- A replica is never a target: it is not `default`, in no shard group,
  never routed to, never named as a unit of work's database, and never
  migrated.
- There is no automatic failover: a replica that is down fails its
  requests, and `/ready` reports it.

**Tables.**

- `table_for(cls, name, *extra, domain=...)` derives the columns from the
  dataclass fields and hints (`domainkit.ddd.fields`): integers are
  `BIGINT`, text `VARCHAR(n)` with `MaxLength(n)` or `TEXT`, amounts
  `ExactDecimal` with `Digits(p, s)` (required), datetimes `UTCDateTime`,
  enums `EnumValue` (their values), value objects of one field
  `ValueObjectType` (that field), and other classes what
  `register_column_type` names. `X | None` is nullable; everything else
  is `NOT NULL`. Columns passed in `extra` replace derived ones.
- `MaxLength` and `Digits` live in `domainkit.ddd`, since a domain layer
  may import nothing else.
- Child entities are relationships, not columns (`children`, `child`).
- Revisions name the plain SQL types, as for any `TypeDecorator`.

## Consequences

Reads scale out without changing what a transaction sees; a request that
must read its own writes reads them in its writable unit of work. Tests
with two SQLite files do not replicate, so test settings point replicas at
the primary or leave them out.

A field is declared once, and fakes, queries, and tables agree on its
type. The storage choices are hard to change once tables hold data: enums
store values (renaming a member is free, changing a value is a data
migration), integers are 64-bit, and text without `MaxLength` is
unbounded. Hand-written tables keep working beside derived ones, and a
derived table can be replaced by a hand-written one without a migration
as long as the columns match.
