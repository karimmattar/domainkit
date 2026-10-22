# 0061. Read without a transaction where it is safe, and ping a connection once

- Status: Accepted
- Date: 2026-10-21

## Context

A read by id through a unit of work on PostgreSQL made six round trips for
one `SELECT`, counted on the wire with a protocol proxy at 0.8.0:

- SQLAlchemy's asyncpg pre-ping, on every checkout, is `BEGIN`, a prepared
  `;`, and `ROLLBACK`: three round trips. It opens a transaction so that
  the prepared statement survives PgBouncer's transaction pooling
  (SQLAlchemy issue 10226).
- The unit of work's own `BEGIN` and `ROLLBACK` around the `SELECT`.

A keyed write took five checkouts, so fifteen of its 34 round trips were
pings.

**Pre-ping.** Turning it off measured −8% on an authenticated read in the
performance re-audit. The options were:

- Keep SQLAlchemy's ping: three round trips per checkout.
- Ping with one round trip. A `SELECT 1` by the simple query protocol
  prepares nothing, so it needs no transaction behind PgBouncer either.
  Its failures, though, would have to be judged apart from SQLAlchemy:
  treating every one as a lost connection replaces the whole pool when
  a ping only timed out under load. SQLAlchemy's own `;`, sent with the
  connection in `AUTOCOMMIT`, keeps SQLAlchemy's judgement (the pool is
  replaced only if asyncpg closed the connection). Its statement is
  prepared, so PgBouncer still needs the transaction around it.
- Turn pre-ping off, and run a statement again on a fresh connection when
  the server had closed the one it used. That is safe only for a statement
  that cannot have changed anything. A read on its own qualifies. The
  first statement of a writable transaction does too, but only while
  nothing else ran, and later statements of a transaction never do. Every
  writable path would show one error per stale connection after a
  failover or restart.

CPU per read through a unit of work, 16 in flight, on 0.8.0 with each
variant patched in (best per round, two rounds interleaved, load average
about 3.5):

| Ping | µs |
|---|---:|
| SQLAlchemy's (3 round trips) | 1,265–1,389 |
| One round trip (a `SELECT 1`, measured) | 1,194–1,217 |
| None | 1,151–1,157 |
| None, and no transaction | 1,116–1,203 |

Going from one round trip to no ping saves about 3–4% more. That is within
the noise here, and it would cost the failover guarantee on every
writable path.

**Reads without `BEGIN` and `ROLLBACK`.**

- Making `readonly=True` run without a transaction would change it for
  existing users. Server-side cursors (`stream()`) need a transaction, and
  so do `SET LOCAL` and transaction-scoped advisory locks.
- Running statements on their own on the same pool, and refusing writes in
  the session, cannot see writes inside SQL text or functions. Such a
  write would commit silently.
- Running them on a pool of their own, whose connections PostgreSQL holds
  read-only from the start (`default_transaction_read_only`), makes the
  server refuse every write, however it is sent. It costs a second pool
  per alias.

`SET LOCAL statement_timeout`, which keeps a transaction to the request's
deadline, needs a transaction. The request's own timer already cancels its
task, and asyncpg then cancels the statement on the server. A task that
only shares the deadline has no such timer. asyncpg's per-call timeout is
not reachable through SQLAlchemy without private APIs.

In `READ COMMITTED`, each statement sees its own snapshot, inside a
transaction or not. One snapshot for several statements needs `REPEATABLE
READ`.

**Core for hot reads.** A repository `get` by id through the ORM took 760
Python calls and 744–795 µs of CPU per read. A Core `SELECT` of the same
row, returning a bare row, took 591 calls and 665–673 µs. Keeping the
ORM's guarantees from Core means building the aggregate, registering it
in the identity map, and tracking its version, which is the work those
169 calls do.

## Decision

- **Pre-ping stays on by default** (`POOL_PRE_PING`). On asyncpg engines
  outside PgBouncer it is SQLAlchemy's own `;`, sent with the connection in
  `AUTOCOMMIT`: one round trip, outside any transaction. Its errors are
  judged as before: a connection asyncpg closed is replaced, with every
  older idle one, and any other error is raised. Through PgBouncer,
  SQLAlchemy's ping stays as it is. The framework's own short checkouts
  keep the ping.
- **Read-only units of work choose how they read**, with
  `reads=Reads.<mode>`. Passing `reads` makes the unit of work read-only.
  - `TRANSACTION` is the default, and `readonly=True` alone keeps it: a
    `READ ONLY` transaction, as before.
  - `STATEMENTS` runs each statement on its own.
  - `SNAPSHOT` reads one snapshot: `REPEATABLE READ`, `READ ONLY` on
    PostgreSQL, and an explicit `BEGIN` on SQLite.
- **`STATEMENTS`** reads on an engine of its own per alias (the
  `<alias>:reads` pool), on PostgreSQL through asyncpg or psycopg:
  - The engine runs in `AUTOCOMMIT`, and its connections start with
    `default_transaction_read_only=on`, added after `CONNECT_ARGS`.
  - It does not pre-ping. A statement whose connection the server had
    closed runs once more on a fresh one.
  - Each statement is bounded by the deadline, unless a timer of its own
    task already enforces it (`deadlines.enforced()`).
  - Through PgBouncer, which keeps no timeouts in the session, and on
    SQLite or other drivers, it reads in the `READ ONLY` transaction.
- `STATEMENTS` and `SNAPSHOT` sessions also refuse to flush, and refuse
  `INSERT`, `UPDATE`, `DELETE` and DDL statements, with `ReadOnlyError`.
- **No Core path for hot reads.** The measured saving was about 10–15% of
  CPU at best, within the noise, and only for a path that drops the
  guarantees.

## Consequences

Round trips:

- A read through a unit of work takes 4 round trips (was 6).
- A `STATEMENTS` read by id takes 1.
- Every checkout of an asyncpg pool outside PgBouncer saves two.

What `STATEMENTS` costs and how it behaves:

- It opens a second pool per alias, sized like the alias's own. A process
  may hold up to twice the connections at peak, so size the server's
  `max_connections` for it. `pool_stats` and the `db_pool_connections`
  metric report the pool as `<alias>:reads`.
- Its statements do not share a snapshot, as in a `READ COMMITTED`
  transaction. Use `SNAPSHOT` when they must.
- Server-side cursors (`stream()`) need a transaction: in `STATEMENTS` they
  fail at once. Use `TRANSACTION` or `SNAPSHOT` for them.
- A write in SQL text is refused by the server (SQLSTATE 25006), as in a
  `READ ONLY` transaction. Flushes and write statements are refused by the
  session before anything is sent.
- The server stops a statement cut off at the deadline when asyncpg's
  cancel request reaches it, not by `statement_timeout`.
  `STATEMENT_TIMEOUT` and `COMMAND_TIMEOUT` still bound every statement.
- A statement started after the deadline has passed is refused at once.
- After a restart or failover, a `STATEMENTS` read gets its answer from a
  fresh connection, with no error. Other pools rely on pre-ping, which
  cannot catch a connection that dies between the ping and the statement.

Reversal is cheap: `STATEMENTS` and `SNAPSHOT` are opt-in per unit of work,
and the ping is internal.
