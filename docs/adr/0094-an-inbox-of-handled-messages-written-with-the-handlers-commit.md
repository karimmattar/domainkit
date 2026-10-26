# 0094. Keep an inbox of handled messages, written with the handler's commit

- Status: Accepted
- Date: 2026-10-26

## Context

Delivery from the outbox is at least once (ADR 0005, 0014): a worker that
dies, or loses its database, after a durable handler committed but before
it recorded the message as done leaves the message to run again once its
lease ends; RabbitMQ hands a message out again when its consumer goes
away; a retry runs a handler that failed after its commit. ADR 0078 told
handlers to be idempotent and planned an inbox of processed message ids
(FIN-80). A test reproduced the gap on SQLite and PostgreSQL: a handler that
writes one row, whose outcome was lost and whose lease ran out, wrote two.

An inbox is a table of handled messages, written in the same transaction
as the handler's own work, so the record and the work commit or roll back
together (the "idempotent consumer"). The questions were where the row
goes, how the handler's transaction joins it, whether every handler gets
it, and how long rows are kept.

- **Where the row goes.** A handler's unit of work commits on its own
  domain's database, which may not be the outbox's (each hosting database
  has its own outbox; a handler in one domain reacts to another's events).
  Only a row on the database the handler commits to can share its
  transaction, so the inbox lives in every hosting database, as the outbox
  does, and the row goes where the commit goes.
- **How the transaction joins it.** The handler opens its own unit of work,
  so the worker cannot hand it a transaction. The `on_stage` hook
  (ADR 0085) already adds the outbox's rows to every committing unit of
  work; the worker marks the run in a context variable, and the hook adds
  the inbox row when a run is current. The unit of work needs no change.
- **Opt-in or default.** Every handler, by default, would make a handler
  that commits twice in one run unsafe: the first commit writes the row, a
  crash before the second leaves a row that skips the whole run when the
  message comes back, so the second commit's work would be lost, silently,
  where today it is done twice. Handlers written for at-least-once may
  commit several units of work; only their authors know.
- **The cost.** A worker ran 300 messages a round, three rounds, each a
  durable handler that commits one row, on PostgreSQL 18 and on SQLite,
  with `BATCH_SIZE` 50 and `CONCURRENCY` 10. The inbox costs one statement
  per message in the handler's transaction (an `INSERT ... ON CONFLICT DO
  NOTHING`) and nothing else on a first attempt:

  | Statements per message | PostgreSQL | SQLite |
  | --- | --- | --- |
  | 0.10.0 | 2.10 | 2.10 |
  | 0.11, without the inbox (the lease renewal of ADR 0095) | 3.10 | 3.10 |
  | 0.11, with the inbox | 4.10 | 4.10 |

  The time per message, the median of the three rounds, moved from 23.6
  to 25.3 ms on PostgreSQL and from 45.6 to 47.7 ms on SQLite with the
  inbox, on a machine whose load average was above 40 on four cores, so
  the time says little beyond "a statement's worth"; the counts are exact.

## Decision

- **`domainkit_inbox`**, owned by `contrib.messaging`, in every database
  that holds an outbox: `(target, message_id)` as its key, `outbox` (the
  alias of the database whose outbox holds the message), indexed, and
  `handled_at`. `target` is the handler's qualified name or the task's
  key; rows hold no arguments. Projects create it with
  `makemigrations` and `migrate`.
- **Opt-in, per handler or task:** `@inbox` from
  `domainkit.contrib.messaging`, on a durable handler or a task, before or
  after `@task`. In a chain only the last step may use it: a skipped run
  has no result to hand to the next step.
- **The run's first commit writes the row**, on the database it commits
  to, with `ON CONFLICT DO NOTHING`. If no row was written, an earlier run
  committed: the commit raises `AlreadyHandled` and rolls back, and the
  worker records the message as done (outcome `handled`). Later commits of
  the same run write nothing. On PostgreSQL a concurrent run's insert
  waits for the first to end, so two runs never both commit.
- **A run that may repeat an earlier one** (any attempt after the first)
  first looks for the row on the outbox's database, and is skipped without
  calling the handler if it is there. First attempts, nearly every run,
  pay nothing for it.
- **A row lives as long as its message may run.** Each row records the
  alias of the outbox holding its message. Each worker's cleanup task
  deletes the rows of its outbox's messages that are no longer pending
  or dead (done, cancelled, or cleaned up), in batches, in one statement
  per batch (`NOT EXISTS` a pending or dead message with that id). Rows
  of messages another database's outbox holds are read a batch at a time,
  in key order, and deleted once that outbox says their messages finished.
  A pending message may wait without spending an attempt (a task over its
  `rate`, a queue no worker serves, a broker whose consumers are down)
  and a dead one may be retried at any time, so no age is safe.

  The first version deleted rows by age (`INBOX_RETENTION_SECONDS`, 7
  days), checked against a redelivery window of attempts, retry delays,
  leases, and republishing (`messaging.E001`). The 0.11 security review
  (SEC-11b-1) proved a `1/day` task with eight messages waiting holding
  one for eight days: its row was pruned, and the message committed its
  work again. The setting and the check were removed with the age. One
  batch of 1,000 rows on PostgreSQL 18 reads 5,237 buffers (15 to 25 ms on
  the loaded machine) tied to messages, against 2,177 (10 ms) by age: a
  cost of the cleanup task, once per `CLEANUP_INTERVAL`, never of a run.
- **Brokered delivery** runs through the same code: a consumer takes the
  message over and runs it as a polling worker does, so a redelivery is
  skipped the same way. In-process handlers (ADR 0078) have no message and
  no inbox: they run at most once, after the commit, as before.

## Consequences

- A handler marked `@inbox` whose effects are in the database commits them
  once per message, however often it is delivered. What it does outside
  the database (a mail, a call) may still happen twice: before its commit,
  nothing tells the runs apart.
- Its work must be one unit of work: a run whose first commit landed is
  skipped whole when the message comes back.
- Each inbox message costs one insert in its transaction (one statement,
  above), and one row until its message finishes; cleanup deletes them in
  batches. The table holds the rows of messages still pending or dead,
  and of those finished since the last cleanup.
- Rows of a database alias that is no longer configured stay: no outbox
  can say their messages finished.
- The inbox holds handler names, message ids, and times: no personal data,
  nothing for erasure to find.
