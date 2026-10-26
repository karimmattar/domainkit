# 0004. Async SQLAlchemy with imperative mapping

- Status: Accepted; amended 2026-10-25 (at the end)
- Date: 2026-10-14; amended 2026-10-25 (an aggregate table without `version` is refused)

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

## Amendment (2026-10-25): an aggregate table without `version` is refused

Optimistic locking turned on only when the table happened to have a
`version` column. `table_for` always derives one, but a hand-written table
without it (the docs' own money examples, and brownfield schemas) was
mapped silently with no locking at all: in the 0.9.0 audit (FIN-77), 50
concurrent withdrawals on PostgreSQL all answered 201 and 48 of them were
lost.

- `map_imperatively` raises `ImproperlyConfigured` for an aggregate whose
  table has no `version` column, naming the class, the table, the column
  to add, and the opt-out.
- `map_imperatively(..., versioned=False)` maps it without optimistic
  locking, on purpose. A mapping that passes `version_id_col`, and a
  subclass mapped with `inherits`, have chosen their version already.
- Entities and other classes are unaffected.

This is a breaking change in a patch release (0.9.1), made on purpose.
Warning first (`DomainkitDeprecationWarning`) and refusing in 0.10 was
considered and rejected:

- the failure it reports is lost money with no error. A
  `DeprecationWarning` raised outside `__main__` is not even shown by
  Python's default filters, so in production the warning would be as
  silent as the bug;
- a project that turns warnings, or `DomainkitDeprecationWarning` as
  `docs/versioning.md` recommends, into errors in its tests fails on the
  warning just as on the error;
- the refusal happens at startup, when the mappers are set up, never in
  the middle of a request, and either fix is one line (a column with a
  migration, or `versioned=False`).

Nothing domainkit ships trips it: every contrib aggregate's table, the
default and abstract user columns, and the tables `startdomain` and
`startaggregate` write (`table_for`) have the column.
