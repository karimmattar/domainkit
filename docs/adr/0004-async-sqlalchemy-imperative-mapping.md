# 0004. Async SQLAlchemy with imperative mapping

- Status: Accepted
- Date: 2026-10-14

## Context

The domain model must not depend on the persistence library (see 0003),
yet aggregates need to be stored without hand-written mappers. The service
is async end to end and targets PostgreSQL, with SQLite for fast local work.

## Decision

- Use SQLAlchemy 2 in async mode, with asyncpg for PostgreSQL and aiosqlite for SQLite.
- Map tables onto the plain domain classes imperatively, in each domain's
  `infrastructure/orm.py`, so `domain/` never imports SQLAlchemy.
- Keep one engine per database alias and event loop.
- Use optimistic locking on `version` columns, surfaced as `ConcurrentUpdate` (409).
- Generate migrations with Alembic autogenerate, configured from settings.

## Consequences

Domain classes stay plain dataclasses that are trivial to test. Mapping
mistakes show up at startup or in repository tests, not in the model. Every
model change needs a revision, which `makemigrations --check` enforces in CI.
