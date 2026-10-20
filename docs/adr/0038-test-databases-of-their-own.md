# 0038. Give the tests PostgreSQL databases of their own

- Status: Accepted
- Date: 2026-10-19

## Context

The `database` fixture of `domainkit.testing` migrated whatever database the
test settings named, down to base and up again, which emptied it. Only
in-memory SQLite was replaced (by a temporary file). On PostgreSQL that meant:

- the tests used, and emptied, a database someone had to create and name;
  a mistake in the test settings emptied the wrong one, held back only by
  the `ENVIRONMENT = "test"` guard of 0.6.1;
- two runs at once (pytest-xdist workers, or two checkouts on one server)
  shared one database and broke each other;
- every run paid for the full migration round trip.

Django creates `test_<name>`, migrates it, and drops it after the run; it
keeps it with `--keepdb`, and pytest-django gives each xdist worker its own
(`test_<name>_gw0`).

## Decision

- On PostgreSQL the plugin creates `test_<name>` (or the entry's
  `TEST_NAME`) on the same server, from its `postgres` database, as the
  schema owner (`MIGRATION_URL` when set). Under pytest-xdist, worker
  `gw0` gets `test_<name>_gw0`; the worker's id is read from
  `PYTEST_XDIST_WORKER`, so xdist is not a dependency. Entries naming the
  same database share one test database.
- The plugin only ever creates or drops names that start with `test_`; a
  `TEST_NAME` that does not, or a name PostgreSQL would cut short (over 63
  bytes), is refused before anything happens. A `test_` database left by
  an interrupted run is replaced.
- The test database is migrated up, down to base, and up again, as before,
  and dropped (`WITH (FORCE)`) when the run ends.
- `--keepdb` keeps it: the next run reuses it, migrates it up only, and the
  rows are put back as `migrate` left them when each run ends.
- In-memory SQLite still becomes a temporary file; any other URL is used as
  it is. The `ENVIRONMENT = "test"` guard stays.

## Consequences

- The configured database is never touched by the tests, and parallel runs
  no longer collide.
- The test settings' role needs `CREATEDB` and access to the `postgres`
  database. A role without them can still run the tests: create
  `test_<name>` once, by hand, and run with `--keepdb`. **This changes what
  existing PostgreSQL test setups need.**
- With separate roles, the test database is owned by the `MIGRATION_URL`
  role; the `URL` role gets what the migrations grant it, as in production.
- A run killed before it ends leaves `test_<name>` behind. The next run
  replaces it, or, with `--keepdb`, reuses it with whatever rows it held.
