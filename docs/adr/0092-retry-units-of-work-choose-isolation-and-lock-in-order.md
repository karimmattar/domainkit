# 0092. Run units of work again after a lost race, let them choose their isolation, and lock in order

- Status: Accepted; amended 2026-10-28 (at the end)
- Date: 2026-10-26

## Context

The 0.9.0 audit's only open High finding, FIN-78: the default write path
has no retry, no isolation choice, and no lock API beyond one row. Every
SQL unit of work ran at the database's default, `READ COMMITTED` on
PostgreSQL. Optimistic versions (ADR 0004) stop lost updates of one row,
and `get_for_update` (DJ-18) locks one row; nothing else was there.

Proofs of concept on 0.10.0, PostgreSQL 18, asyncpg:

- **Write skew.** Two gadgets with a stock of 1 each, and the rule "at
  least one left across gadgets". Two units of work each read the total
  (2), each take one from a different gadget, and both commit: stock 0.
  No row was changed twice, so no version check failed.
- **A serialization failure.** The only way to run serializable was
  `SET TRANSACTION` as the first statement. Through a `ResourceRouter`
  command, of two such requests one answered 503 `database_busy`
  (SQLSTATE `40001`), and nothing ran it again.
- **A deadlock.** Two transfers that lock their accounts with
  `get_for_update`, source then target, in opposite directions: one ended
  with SQLSTATE `40P01`.

The questions were where a retry lives and what it must undo, how a level
is set and what SQLite does with it, which locks to offer, and what each
costs on the default path, which must not get slower.

**Where the retry lives.**

- **Around the `async with` body.** A context manager cannot run its body
  again; a retry there would have to be a loop the caller writes, and code
  before the block (a read, a call out) would not be repeated with it.
- **In the router**, around a command's service. It helps HTTP only;
  workers, management commands, and handlers would have none. It cannot
  tell which errors are conflicts for a database.
- **A decorator on services.** It re-runs the service, but a scoped
  resource lends the service the transaction its reach check ran in
  (ADR 0025, `Joined`): a second run would get a new transaction without
  the check.
- **In the unit of work, taking the work as a callable** (`run(work)`).
  The unit of work knows which errors mean a lost race on its database
  and whether it committed during the run; the router's retry can be
  built on it, re-making its checks in each run.

**What a failed run leaves behind.** The run's transaction takes its rows
with it: the outbox's messages (ADR 0005) and the audit entries are
written by `on_stage` hooks in the transaction, and so is the
`Idempotency-Key`'s mark (ADR 0019). In-process events are published only
after a commit succeeds. Two things outlived the transaction: messaging
kept the rows it staged, per unit of work, to hand to the publisher after
the commit, and added the next commit's rows to them; and in-memory
repositories are not transactional. A key pinned on another database
(ADR 0062) is pinned by a statement of its own, which a second run
repeats harmlessly.

**Round trips**, counted on the wire with a TCP proxy, per unit of work
that reads a gadget by id, changes it, and commits (pool of one, after
warming the statement cache):

| Variant | Round trips |
| --- | ---: |
| Default (`READ COMMITTED`) | 7 |
| `isolation=SERIALIZABLE`, the `isolation_level` execution option | 7 |
| `SET TRANSACTION ISOLATION LEVEL SERIALIZABLE` as the first statement | 8 |
| `lock(name)`, then the same work | 8 |
| Two rows, `get_for_update` twice | 11 |
| Two rows, `get_many_for_update` | 8 |

The execution option puts the level in the `BEGIN` asyncpg sends, and
SQLAlchemy restores the connection's default when it goes back to the pool
by setting an attribute of the driver's connection: no round trip either
way. psycopg does the same.

**Python calls** of a command request through `ResourceRouter`, 200
requests on PostgreSQL, no conflict: 5,609 per request without `retry`,
5,633 with `retry=Retry()` (+24, 0.4%); 4 statements and 1 transaction
either way.

**SQLite** knows no levels. Its driver begins a transaction only before
the first write, so the reads before it are outside it, and the write
skew above happens on SQLite too. A plain `BEGIN` before the first read
would hold a snapshot, but a transaction that then writes after another
committed fails at once with "database is locked", which looks the same as
a lock wait that ran out. `BEGIN IMMEDIATE` takes the write lock before
the first statement: such transactions run one at a time.

**Which locks.** PostgreSQL takes the row locks of `SELECT ... FOR UPDATE
... ORDER BY` after sorting, in that order. Locking a set of rows by
ascending id in one statement therefore gives every transaction the same
order. Work without a row to lock ("one payout per account and day")
needs a lock by name: an advisory lock. PostgreSQL's `hashtext` gives 32
bits and is not promised stable across major versions.

## Decision

- **`UnitOfWork.run(work, *, retry=None)`** runs `work`, a function of no
  arguments that does the whole job and enters the unit of work itself, and
  runs it again when it raises an error `retry` names: PostgreSQL's
  serialization failures (`40001`) and deadlocks (`40P01`) for the SQL unit
  of work (`_conflicted`), and the errors in `Retry.retry_on` everywhere.
  In memory nothing conflicts. Lock and statement timeouts (`55P03`,
  `57014`) are not run again: their time is spent.
- **`Retry(attempts=3, backoff=0.02, max_backoff=1.0, retry_on=())`**,
  validated: 1 to 10 runs; before each next run a random wait between 0
  and `backoff` doubled per run, capped at `max_backoff` (at most 60
  seconds), "full jitter". `run` without `retry` uses `Retry()`.
- A run is not repeated, and its failure is raised as it was, when the
  unit of work committed during it (running it again would commit twice),
  when the wait would pass the deadline (`domainkit.deadlines`), or after
  the last run, with a note saying how many there were. Under a frozen
  clock the wait only yields, so tests never sleep.
- Before a run is repeated, in-memory repositories are put back as they
  were before it, events included; a SQL run opens its own session.
  Messaging keeps the rows it hands to the publisher per session, which
  each run opens anew, instead of per unit of work.
- **Routes retry by declaration**: `command_route(..., retry=Retry())`
  runs the reach check, `If-Match`, and the service in each run. The cost
  is small, but a service that calls out, or commits another unit of work,
  would repeat that; under the default level a retry only helps after a
  deadlock; and no route pays for what it does not use.
- **`isolation`** on `SqlAlchemyUnitOfWork`, a class attribute or an
  argument: `Isolation.READ_COMMITTED`, `REPEATABLE_READ`, `SERIALIZABLE`
  (names read in any case). None, the default, keeps the database's own.
  On PostgreSQL it is the `isolation_level` execution option of an engine
  made once per engine and level, sharing its pool. On SQLite the two
  stricter levels begin with `BEGIN IMMEDIATE`. A read-only unit of work
  refuses it: `reads=` says how it reads, and PostgreSQL refuses
  `SERIALIZABLE` on hot standbys, where read-only work goes. `READ ONLY
  DEFERRABLE` is left out for that reason. There is no setting: the level
  belongs to the work, and a database-wide one is PostgreSQL's
  `default_transaction_isolation`.
- **`Repository.get_many_for_update(ids)`** locks rows in one statement by
  ascending id, and returns them by id; `nowait` and `skip_locked` as for
  one row; a missing id is the repository's `not_found`.
- **`UnitOfWork.lock(name, *, nowait=False)`**: on PostgreSQL
  `pg_advisory_xact_lock` (or `pg_try_advisory_xact_lock`, refused as
  `LockNotAvailable`) on `lock_key(name)`, a BLAKE2b hash of the name's
  UTF-8 to 64 bits, personalized `domainkit.lock`; transaction-scoped only,
  so nothing outlives the unit of work on a pooled connection. On SQLite
  the database's write lock (`BEGIN IMMEDIATE` unless the transaction
  wrote already). In memory, one registry for the process. A read-only
  unit of work takes no locks.

## Consequences

After the change, the proofs of concept:

- serializable and `run`: one transfer takes, the other is run again,
  sees the change, and refuses: stock 1;
- through a command route with `retry=Retry()`: 200 and 200;
- `get_many_for_update` in opposite directions: both transfers move, one
  after the other. With `run`, the deadlocked transfer of the first proof
  also completes.

On the benchmark ladder nothing changes: the default path adds no
statement, no round trip, and no call. Calls per request against 0.10.0,
on PostgreSQL 18 (statements, transactions, checkouts, and pings equal on
each rung):

| Rung | 0.10.0 | Now |
| --- | ---: | ---: |
| `read` | 1,834 | 1,834 |
| `write` | 2,155 | 2,155 |
| `resource-read` | 2,601 | 2,601 |
| `resource-command` | 3,331 | 3,331 |
| `keyed-write` | 3,618.1 | 3,618.1 |

A first version kept the commit count and the named-lock flag on every
instance: `write` and `resource-command` took 14 calls more, and
`keyed-write` 3, because `repositories()` checks each attribute of the
unit of work. Both now live on the class until `run` or `lock` sets them.

What to watch:

- The guard against committing twice sees only the unit of work `run` is
  called on. Work that commits another unit of work, or calls out, is run
  again with the rest; the docs say to leave such calls to deferred tasks.
- A keyed request's key notes the people the events of every run are
  about, the lost run's too: what a stored response is linked to errs on
  the side of more.
- `batch_route` takes no `retry` yet; each item's service may call `run`
  itself.
- SQLite's "database is locked" after its busy timeout is not run again.
- `isolation=SERIALIZABLE` without `run` (or `retry=`) answers more 503s
  under contention than before: choose both together.
- A deadlock's loser run again at once can lock its first row before the
  winner, which waited on the loser's transaction, locks it again, and
  deadlock a second time: the tests saw it about once in fifteen runs
  without a wait. The jittered wait makes it rare, and `attempts` bounds
  it; `get_many_for_update` avoids the deadlock altogether.

## Amendment (2026-10-28, 0.12): batch routes retry by declaration too

`batch_route(..., retry=Retry())` takes the policy a command route takes.
Routes have no `isolation=`: the level belongs to the unit of work the
resource names (`isolation` on its class), for batches as for commands.

- Without `atomic`, each item is a use case of its own, so each item that
  loses a race runs again alone, through `run`, its reach check and
  `If-Match` made again, as a command route's service is.
- With `atomic`, the race ended the batch's transaction, so the whole
  batch runs again, through `run`, in a new one. An item that loses (a
  failure `retry` names, or the database's conflict) leaves the batch
  instead of answering for itself; a race lost at the batch's commit is
  run again as `run` runs any. Once the runs are spent, the batch answers
  as one that does not retry: that item `503 database_busy` and the
  others `424 not_applied`.

Before, a batch could not say it; each item that lost a race answered
503, and an atomic batch was undone. A test that fails one item's
transaction once answers `[200, 503]` without `retry` and `[200, 200]`
with it, and `[424, 503]` and `[200, 200]` atomically.
