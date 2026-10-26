# 0095. Fence a run's commits with its lease, instead of a heartbeat

- Status: Accepted
- Date: 2026-10-26

## Context

A worker holds a message under a lease: the claim's lock id, and
`locked_until`, `VISIBILITY_TIMEOUT` (300 s) after the claim. Once it
passes, another worker may claim the message and run it. `TASK_TIMEOUT`
(and each task's `timeout`) must stay under `VISIBILITY_TIMEOUT`, so a run
that its event loop can end is ended inside its lease. Recording the
outcome (`_done`, `_release`) already matched the lock id, so a run that
lost its lease could not mark the message done or retry it.

The handler's own commits were not fenced. A test reproduced it on SQLite
and PostgreSQL (FIN-80): a handler blocked past its lease, another worker
took the message over and ran it to the end, then the first one went on
and committed too; both effects stayed. A handler outlives its lease when
its event loop is blocked (synchronous code), its process is paused (a
long garbage collection, `SIGSTOP`, a virtual machine moved), or it ignores
its cancellation.

Two remedies were considered:

- **A heartbeat**: a task that extends `locked_until` while the handler
  runs. It runs on the handler's event loop, so it stops in exactly the
  cases above: a blocked loop or a paused process extends nothing. Where
  the loop does run, the timeout already ends the run inside its lease.
  For a handler that ignores its cancellation, a heartbeat would hold the
  message for as long as the handler lives, maybe for good, or stop at the
  timeout, which is what the lease does already. It also costs a write per
  running message per interval. It would let `VISIBILITY_TIMEOUT` be
  shorter than a task's timeout, a feature, not this fix.
- **Fencing**: check the lease where the effect lands. The lock id is the
  fencing token, new at each claim and take-over. A stale holder's commit
  is refused, whatever kept it from noticing.

## Decision

- **Fence each commit on the outbox's database.** While a worker runs a
  message, a unit of work the handler commits on the database of the
  message's outbox first runs, in its own transaction,
  `UPDATE domainkit_messages SET locked_until = <now + VISIBILITY_TIMEOUT>
  WHERE id = :id AND lock_id = :lock`. No row: another worker took the
  message over, so the commit raises `LeaseLost` and rolls back. One row:
  the lease is renewed and the row stays locked until the commit ends, so
  no claim (`SKIP LOCKED`) or take-over gets it meanwhile, and none after
  it while the renewed lease runs.
- The run's outcome is `superseded` (logged as a warning, counted in
  `messaging_messages_total`), and the message is left to the worker that
  holds it. A run that finishes after losing its lease, without a commit
  to refuse, gets the same outcome instead of `done`.
- **On another database**, where the outbox's row is not in the
  transaction, nothing is fenced: the inbox (ADR 0094) is what makes such
  a handler's commit happen once, as it does for a commit that comes after
  a crash rather than during another run.
- **No heartbeat.**

## Consequences

- A stale run can no longer commit on the outbox's database: of two runs of
  one message, only the one holding the lease commits there. Without the
  inbox a message can still run twice in sequence (a crash after the
  commit); with it, once.
- Every unit of work a running handler or task commits on its outbox's
  database costs one more statement, an update of one row by its key, and
  holds that row's lock until the commit. A handler that commits one row
  on the outbox's database ran 3.10 statements per message against 2.10
  in 0.10.0, on PostgreSQL and SQLite alike (ADR 0094 has the
  measurement).
- A commit that renews the lease gives the run `VISIBILITY_TIMEOUT` again
  from there.
- A handler that catches `LeaseLost` must not commit elsewhere instead;
  letting it propagate is what the documentation asks.
- A shorter `VISIBILITY_TIMEOUT` than a task's timeout stays refused: a
  heartbeat would be needed for that, and this decision does not add one.
