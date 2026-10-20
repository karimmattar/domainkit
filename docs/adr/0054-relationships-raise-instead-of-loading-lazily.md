# 0054. Make relationships raise instead of loading lazily

- Status: Accepted
- Date: 2026-10-20

## Context

SQLAlchemy loads a relationship lazily by default (`lazy="select"`): the
first access of an unloaded attribute runs a query. An `AsyncSession`
cannot run I/O inside an attribute access, so in async code that access
raises `MissingGreenlet` ("greenlet_spawn has not been called"), from deep
inside SQLAlchemy, wherever the attribute happens to be read first: a
schema serializer, a log line, a test (the re-audit's ASYNC-22, R19).

0.7.0 added `children` and `child`, which load an aggregate's entities with
it (`lazy="selectin"`), and auth names `lazy` on all of its relationships.
Any other `relationship()` a project maps, by hand or with a `backref`,
still loads lazily.

Options considered:

- **Document it and leave the default.** The failure stays far from its
  cause, and the default is the one that cannot work.
- **Default to `selectin`.** Every page of aggregates loads every such
  relationship, used or not: a hidden query per relationship, which is how
  N+1 problems and slow pages begin.
- **Default to `raise`.** An unloaded access fails at once, at the access,
  with a message that names the relationship and says why. Loading becomes
  a decision made in the query.

## Decision

- `map_imperatively` turns every relationship in `properties` whose
  `lazy` is SQLAlchemy's default, `"select"`, into `lazy="raise"`, and
  gives the reverse side of its `backref` `lazy="raise"` unless the backref
  names its own.
- A relationship that names `lazy` keeps it. `lazy=True`, SQLAlchemy's
  synonym for `"select"`, is how a project asks for lazy loading on
  purpose, for `AsyncAttrs.awaitable_attrs` or code run with
  `session.run_sync`; SQLAlchemy cannot tell an explicit `"select"` from its
  default.
- The change sets `lazy` and the loader's `strategy_key` before the mapper
  is configured, as SQLAlchemy itself does for `WriteOnlyMapped`
  annotations.

## Consequences

- An access that used to fail with `MissingGreenlet` now fails with
  `InvalidRequestError: 'Order.customer' is not available due to
  lazy='raise'`, at the line that reads it, in development and tests.
- Code that relied on lazy loading in sync contexts (`run_sync`) needs
  `lazy=True`, or `selectinload` in its query: the changelog says so.
- `raise_on_sql` (allow loads that need no query) stays a choice a project
  makes per relationship; the default is the stricter one.
