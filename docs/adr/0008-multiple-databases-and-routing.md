# 0008. Several databases: two kinds, routed by domain

- Status: Accepted
- Date: 2026-10-14

## Context

Until now a project had one SQL database in practice. `DATABASES` accepted
more aliases, but every alias got every table, all aliases shared one
migration history, the outbox refused units of work on other aliases, and
the audit trail skipped them silently.

Teams need:

- to keep bounded contexts in separate databases (ownership, load, blast
  radius, compliance), and to spread one context over several databases
  (sharding, [0009](0009-sharding-with-hash-buckets.md))
- a second kind of database, an append-only ledger for evidence that must
  stay provably unchanged ([0011](0011-immudb-ledger-for-evidence.md))

Options considered:

- **Django-style router classes** (`db_for_read`, `db_for_write`,
  `allow_migrate`). They are flexible, but the rules live in code, and
  migrations and runtime can disagree when a router is inconsistent.
- **One Alembic environment over several engines** (Alembic's `multidb`
  template). It keeps a single revision history, so every database must
  move in lockstep, and shards would need one branch each.
- **A routing table in settings, with one migration history per target.**
  Where a domain lives is a setting, read the same way by migrations and at
  runtime.

## Decision

- Two kinds of database, each in its own setting, so neither setting's
  options or code paths mix:
  - **SQL** in `DATABASES` (any SQLAlchemy async URL; PostgreSQL in
    production, SQLite for development and tests): read and write,
    migrated, transactional. A ledger URL there is refused.
  - **Ledgers** in `LEDGERS` (`immudb://`, or `memory://` for tests): write
    once, read many, verified on the client. Never migrated, never in a
    unit of work.
- `DATABASE_ROUTING` maps a domain label to a **target**: a SQL alias, or a
  shard group from `SHARDS`. Unlisted domains use `default`. A domain's
  tables are created in its target, and its units of work open sessions
  there, from the same setting.
- Each target has its own migration history: `default` keeps
  `MIGRATIONS_DIR`, and every other target uses `MIGRATIONS_DIR/<target>`.
  A shard group has one history, applied to each of its databases.
- `DomainConfig.databases(router)` says which targets hold an app's tables
  (default: the domain's routing target). The outbox and the audit trail
  must commit with business data, so `contrib.messaging` and
  `contrib.audit` put their tables in every target that hosts a domain.
  `contrib.idempotency` keeps its table in its own `DATABASE`.
- `makemigrations` and `migrate` handle every target unless `--database`
  names one. Downgrading to a revision needs `--database`, since revision
  ids are per target.
- Workers run one outbox per database: `runworker --database <target>` runs
  every database of a target. Periodic tasks are scheduled only in the
  messaging home database (`MESSAGING.DATABASE`).

## Consequences

- A domain moves between databases by changing settings and migrating, not
  code, but its data must be copied by the operator.
- There are no transactions across databases. A use case touches one
  target, and cross-database work goes through the outbox (eventual
  consistency).
- Events committed in any database are audited there, which changes the
  0.2 behaviour where other databases were skipped. Reading the audit trail
  takes `--database`.
- Projects with only `default` see no difference: same directory, same
  commands, same output.
