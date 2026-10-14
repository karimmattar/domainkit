# Changelog

All notable changes to domainkit. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/); see `docs/versioning.md`.

## [Unreleased]

## [0.5.0] - 2026-10-14

### Added

- Workers on RabbitMQ (ADR 0014): `BROKERS` by alias (`amqp://`,
  `amqps://`, or `memory://`, through the new `rabbitmq` extra) and
  `MESSAGING.BROKER`. Commits publish their messages at once
  (`PUBLISH_ON_COMMIT`), workers relay the rest and run what the broker
  delivers, and `runworker --no-relay` workers only consume. The outbox
  stays the record of every message, so retries, delays, dead letters, and
  `messages` work as before.
- `DomainConfig.committed(uow)`: a hook that runs after each SQL unit of
  work commits; its failures are reported and never fail the commit.
- Queues: `@task(queue=...)` and `@periodic(..., queue=...)`, and
  `runworker --queue` to run only some queues, with or without a broker.
- `domainkit.broker`: the `Broker` port, `MemoryBroker`, and the RabbitMQ
  adapter (durable direct exchange, quorum queues, persistent mandatory
  messages, publisher confirms).
- The `broker` command: `check` connects, `declare` creates the queues.
- `MESSAGING.REPUBLISH_AFTER`, deployment checks for brokers (TLS,
  certificate checks, a user other than `guest`, no `memory://`), and the
  metrics `messaging_relayed_total` and `messaging_deliveries_skipped_total`.

### Changed

- The outbox has a `queue` column. Run `makemigrations` and `migrate` in
  projects that install messaging.

### Fixed

- A message whose worker died during its last attempt was claimed again
  forever; it is now dead-lettered after `max_attempts`.

## [0.4.0] - 2026-10-14

### Added

- Caches (ADR 0012): `CACHES` by alias, with `memory://` (per process,
  least recently used first out), `redis://` and `rediss://` (Redis or
  Valkey, through the new `redis` extra), and `dummy://`. `default` is a
  memory cache unless set.
- `domainkit.cache.caches[alias]`: `get` and `set` with typed reads, `add`,
  `get_many`, `set_many`, `delete_many`, `incr`, `touch`, `clear` (its own
  namespace only), and `get_or_set`. Values are JSON, never pickled; an
  entry that no longer fits its type reads as a miss.
- `get_or_set` and `@cached` share one computation among concurrent misses,
  refresh hot entries early (XFetch, `EARLY_RECOMPUTE`), and compute values
  when the cache fails. A failed cache is skipped for `FAILURE_COOLDOWN`.
- `invalidate_on(Event, keys)` deletes entries when a domain event is
  published after commit.
- `domainkit.http.ratelimit.RedisBackend` (ADR 0013): rate limits shared by
  every replica, run as GCRA in Redis on the server's clock, in the cache
  named by the new `RATE_LIMIT_CACHE`. Requests pass if Redis fails.
- The `cache` command: `check` pings caches, `clear` empties them.
- Deployment checks: a Redis cache on another host needs `rediss://`,
  certificate checks, and a password or client certificate.
- `/ready` checks caches whose `READINESS` is on; metrics
  `cache_lookups_total` and `cache_errors_total`.
- The testing plugin empties memory caches before every test.

### Changed

- The layer rules forbid `redis` in the application layer, as they forbid
  the other database libraries. Use `domainkit.cache` there.

## [0.3.0] - 2026-10-14

### Added

- Several databases of two kinds (ADR 0008): SQL databases in `DATABASES`
  (PostgreSQL, SQLite), and ledgers in the new `LEDGERS` setting
  (`immudb://`, `memory://`).
- `DATABASE_ROUTING` sends each domain's tables and units of work to a
  target. Each target keeps its own migration history in
  `MIGRATIONS_DIR/<target>`, and `makemigrations`, `migrate`, and `check`
  cover every target (`--database` picks one).
- Sharding (ADR 0009): `SHARDS` groups of SQL databases sharing a schema,
  split by shard key over fixed hash buckets, with optional bucket `RANGES`
  and `PINNED` keys. `SqlAlchemyUnitOfWork(shard_key=...)`,
  `domainkit.db.routing.router`, `bucket_for`, and `for_each_shard`.
- PostgreSQL range partitioning (ADR 0010):
  `domainkit.db.partitioning.partition_by_range`, partitions created by
  `migrate` and by the new `partitions` command (`--prune` drops old ones),
  and ignored by `makemigrations`.
- Ledgers (ADR 0011): the `domainkit.ledger` port with `ImmudbLedger`
  (verified operations, TLS, signed states; the new `immudb` extra) and
  `MemoryLedger`. `/ready` checks ledgers.
- `domainkit.contrib.evidence`: canonical, receipted evidence records on a
  ledger, idempotent by id, with tampering detection, durable recording
  through the outbox (`defer_evidence`), and the `evidence` command.
- `runworker --database`, `messages --database`, and `auditlog --database`.
- `DomainConfig.databases(router)`: where an app keeps its tables.

### Changed

- The outbox and the audit trail are created in every database that hosts a
  domain. Units of work on any database can defer tasks, and their events are
  audited there; before, other databases were refused (messaging) or skipped
  (audit).
- `runworker` runs one worker per outbox, every hosting database by default.
- `startdomain` tags generated tables with `info={"domain": ...}`, so
  routing moves them with their domain.
- `SqlAlchemyUnitOfWork.database` defaults to `None`: follow
  `DATABASE_ROUTING` for the domain that defines the subclass. The resolved
  alias is `uow.alias`.

### Fixed

- The audit protection docstring said the table owner was exempt from the
  triggers; they refuse every role, as `docs/contrib.md` says.

## [0.2.0] - 2026-10-14

### Added

- `domainkit init <name> [directory]` creates a project that passes `check`
  and its tests as it is: base, local, test, and production settings, the
  root router, the ASGI application, `manage.py`, `.env`, and a first test.
  The package now installs a `domainkit` command to run it.

## [0.1.1] - 2026-10-14

### Fixed

- `domainkit.testing` needed pytest and httpx2 without declaring them, so
  the quick start's `pytest` step failed on a fresh install. They now come
  with the new `testing` extra: `pip install "domainkit[testing]"`.

## [0.1.0] - 2026-10-14

First version, extracted from a production service template.

### Added

- Django-style project structure: settings modules validated at startup,
  `INSTALLED_DOMAINS` with `DomainConfig`, `ROOT_ROUTER`, `MIDDLEWARE`, and
  `manage.py` commands (`check`, `startdomain`, `makemigrations`, `migrate`,
  `showmigrations`, `runserver`, `shell`).
- Environment variables prefixed from the project name, `.env` files, and
  `<NAME>_FILE` secrets.
- DDD building blocks: `ValueObject`, `Entity`, `AggregateRoot`,
  `DomainEvent`, `Command`, `Query`, `Repository`, `UnitOfWork`, `Page`, and
  business errors mapped to HTTP statuses.
- Layer rules between and within domains, enforced by `check`.
- Async SQLAlchemy persistence: one engine per alias and event loop,
  imperative mapping, optimistic locking with `version` columns, UTC
  timestamps, hidden bound values, libpq TLS options, server-side timeouts.
- Migrations configured from settings: per-domain tables, `--check`,
  `--merge` for parallel branches, reversible-revision testing, and refused
  downgrades in staging and production without `--allow-data-loss`.
- HTTP: one error format, request ids, access logs without query strings,
  security headers, CORS, allowed hosts, body size limits, trusted proxy
  headers, rate limiting (GCRA), health and readiness probes, and keyset
  pagination with opaque cursors.
- Observability: JSON logs with trace ids, Prometheus metrics labelled by
  route template, OpenTelemetry spans, and `ERROR_REPORTERS`.
- `domainkit.contrib.messaging`: transactional outbox, durable event
  handlers, background and periodic tasks, and a worker with retries,
  dead letters, and metrics.
- `domainkit.contrib.audit`: audit trail of committed events and explicit
  records, with database triggers that make it append-only on PostgreSQL.
- `domainkit.contrib.idempotency`: `Idempotency-Key` replay for unsafe
  requests.
- `domainkit.deprecation.DomainkitDeprecationWarning` and the deprecation
  policy.
- `domainkit.testing`: a pytest plugin with migrated `database` and `client`
  fixtures, used by the tests `startdomain` generates.

[Unreleased]: https://github.com/karimmattar/domainkit/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/karimmattar/domainkit/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/karimmattar/domainkit/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/karimmattar/domainkit/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/karimmattar/domainkit/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/karimmattar/domainkit/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/karimmattar/domainkit/releases/tag/v0.1.0
