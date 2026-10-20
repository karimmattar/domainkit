# 0043. Let the outbox's database decide when work is due, and wake workers

- Status: Accepted
- Date: 2026-10-20

## Context

Workers on several hosts share an outbox (ADR 0005). Each read its own
clock to decide what was due and when a lock had expired (re-audit
ASYNC-18). A host whose clock ran ahead took other workers' locks for
expired and ran their messages twice; one that ran behind left due work
waiting. Writers had the same problem: a message due at once, written on a
host whose clock ran ahead, waited out the difference.

Idle workers polled every `POLL_INTERVAL`, so a message waited up to a
poll for a worker, and every idle worker queried the database even with
nothing to do (R20). Cleanup ran inside the claim and relay loops as one
unbounded `DELETE` (ASYNC-07): a slow one held up claiming, and one that
kept exceeding `STATEMENT_TIMEOUT` never succeeded again, so the outbox
grew for good.

Options for the clock:

1. Read the database's time once per round (`SELECT now()`) and compute in
   Python: one more round trip per claim.
2. Use the database's clock inside the statements. Tests that freeze the
   clock (`domainkit.clock.freeze_time`, ADR 0037) would no longer move the
   outbox on PostgreSQL.
3. Use it inside the statements, unless a frozen clock is in use.

Options for scale: partition the outbox by time (domainkit has range
partitioning, ADR 0010) and drop old partitions instead of deleting rows;
or keep one table, delete in small batches, and wake workers with
`LISTEN`/`NOTIFY`.

## Decision

- **The database's clock**, option 3. On PostgreSQL, due checks, lock
  expiry, and the times workers write (locks, leases, retries, starts and
  ends) use `statement_timestamp()` in SQL: stable within a statement, so
  the indexes on `available_at` still apply (`clock_timestamp()` is
  volatile and would not). A message due at once is written as due at the
  earlier of the writer's time and the database's. Under a frozen clock
  the frozen time is used, on every database. SQLite runs in the worker's
  process, so its clock is the host's and nothing changes there. There is
  no setting: the database's clock is right wherever there is one.
- **Wake-ups.** On PostgreSQL with asyncpg, the transaction that writes
  messages due at once sends `pg_notify('domainkit_messages', queue)` for
  each queue, delivered on commit. Polling and relaying workers listen on
  one connection of their pool, never returned to it, and claim at once
  when a commit to one of their queues is announced. Polling stays as the
  fallback: a lost connection is opened again after a growing pause, and a
  notification that nobody hears changes nothing. `MESSAGING.NOTIFY` turns
  it off; with a broker that publishes on commit, nothing is sent.
- **Cleanup** runs in a task of each polling or relaying worker, first a
  random 30 to 60 seconds after start, then every `CLEANUP_INTERVAL`. It
  deletes `CLEANUP_BATCH_SIZE` rows per statement (`LIMIT` with
  `FOR UPDATE SKIP LOCKED` on PostgreSQL, a bounded subquery on SQLite),
  each batch in its own transaction. A failed batch halves the next, down
  to one row; full batches double back.
- **No partitioning, for now.** PostgreSQL requires every unique
  constraint of a partitioned table to include the partition key, so the
  outbox's `dedupe_key` (which makes periodic and chained messages
  exactly-once) and its `id` primary key would stop being unique across
  partitions. Rows move from pending to finished by update, so a partition
  by status would turn each outcome into a delete and an insert. Existing
  outboxes would need a data migration. Small batches with `SKIP LOCKED`
  keep deletes off the claim path, and the claim indexes cover pending
  rows only.

## Consequences

- Clock skew between hosts no longer runs a message twice or holds it
  back. Delays (`delay=`, schedules) still count from the writing host's
  clock, which is acceptable for delays of seconds and more.
- Tests keep moving time with `freeze_time`, on PostgreSQL too.
- A committed message starts within milliseconds on PostgreSQL, and
  `POLL_INTERVAL` can be raised to cut idle queries. Each worker holds one
  more database connection, and each commit that writes due messages sends
  one more statement. Every idle worker wakes on each commit to its queues
  and the first claim wins, so very many idle workers on one busy queue can
  turn `NOTIFY` off.
- Cleanup can no longer stall claiming, and statement timeouts slow it
  down instead of stopping it. Revisit partitioning if deletes cannot keep
  up with the write rate at the chosen `RETENTION_SECONDS`; measure first.
