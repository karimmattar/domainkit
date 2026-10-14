# Changelog

All notable changes to domainkit. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/); see `docs/versioning.md`.

## [Unreleased]

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

[Unreleased]: https://github.com/karimmattar/domainkit/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/karimmattar/domainkit/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/karimmattar/domainkit/releases/tag/v0.1.0
