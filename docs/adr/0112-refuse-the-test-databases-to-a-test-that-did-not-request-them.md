# 0112. Refuse the test databases to a test that did not request them

- Status: Accepted
- Date: 2026-10-29

## Context

The 0.9.0 audit's TEST-08 compared `domainkit.testing` with
pytest-django. pytest-django refuses the database to a test without its
`db` mark or fixture (`django_db_blocker`); domainkit's plugin did not. A
test that reached the database without `db` or a client saw whatever the
test before it left, so its outcome depended on the order of the run. A
first such test, run before any test had made the test databases, reached
the databases the test settings name: the settings were not yet pointed
at `test_<name>`. Both were proven on 0.12.0 by a project's tests run
through the plugin.

Where the refusal could sit, each tried by a proof of concept against
the five ways a test reaches a database: a unit of work, a session from
`connections`, `connections.autocommit()`, an owner engine
(`create_owner_engine`: migrations, fixtures, snapshots), and an engine
a module kept from an earlier, allowed moment:

| Refused at | Unit of work | Session | `autocommit` | Owner engine | Kept engine |
| --- | --- | --- | --- | --- | --- |
| The unit of work's `__aenter__` | refused | reached | reached | reached | reached |
| The connection handler and `create_owner_engine` | refused | refused | refused | refused | reached |
| Every pool's checkout (a listener) | refused | refused | refused | refused | refused |

On SQLite a session and a statement took 1.06 to 1.15 ms with each, as
without: the cost is lost in the noise. The pool listener refuses the most,
but it is a Python call on every checkout of every process, which the
benchmark ladder counts per request, or, installed only by the plugin,
a listener on SQLAlchemy's `Pool` class that would refuse engines a test
makes itself, and would need to tell domainkit's pools from those. An
engine kept across tests is not how domainkit code reaches a database:
`connections.engine()` and `session()` are called for each use.

How a test allows it: a mark checked by an autouse fixture, as
pytest-django does, cost every test about 180 µs (3,000 empty tests: 3.3 s
on 0.12.0, 3.9 s with the fixture). A `pytest_runtest_protocol` hook that
reads the mark costs nothing measurable (3.3 s).

## Decision

- `ConnectionHandler.refuse(reason)` and `allow()`: while a refusal is in
  force, every engine and session the handler hands out, and every
  `create_owner_engine`, raises `DatabaseAccessRefused(reason)`, in every
  thread. `close_all` and `pool_stats` still work. Nothing else in the
  framework calls them.
- The plugin refuses the databases from `pytest_configure` to
  `pytest_unconfigure`, putting back what it found. `database`,
  `_migrated_rows`, and `db` allow them while they work; `db` while its
  test runs. `DatabaseBlocker`, the `database_blocker` fixture, gives
  wider fixtures `unblock()` and `block()`, which nest.
- The message names the fixture to request, the blocker, and the two ways
  to keep 0.12's behaviour: `@pytest.mark.allow_database` for a test or a
  module (read by the hook, which also makes `database` the test's first
  fixture, so a marked test never reaches the configured databases), and
  `domainkit_allow_database = true` for the run.
- An xdist worker is a process of its own, with its own refusal; with
  `--keepdb` the fixtures reuse, migrate, and restore the databases as
  before, with them allowed.
- This repository's suite runs with the refusal on too: its fixtures that
  make databases allow them, and a test of the settings' own in-memory
  database, of a database out of reach, or of engines themselves requests
  `database_access`.

## Consequences

- A test's outcome no longer depends on what ran before it, and no test
  reaches the databases the settings name.
- Project tests that relied on reaching the database without `db` fail,
  named in the message; `docs/upgrading.md` lists the fix and the two
  ways to wait.
- An engine a test makes itself with SQLAlchemy, a ledger, a cache, and
  a broker are not refused. Refusing at the pool would catch the first; it
  stays the way to go if such engines turn out to matter.
- `refuse` is a process-wide switch, as `override_settings` is
  ([0090](0090-module-singletons-overridden-per-test.md)): tests that use
  it run one at a time in a process.
