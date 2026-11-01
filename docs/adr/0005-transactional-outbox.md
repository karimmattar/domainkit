# 0005. A transactional outbox instead of a broker

- Status: Accepted; amended by [0118](0118-run-a-keys-messages-in-commit-order-by-holding-the-later-ones-in-the-outbox.md), and 2026-11-01 (at the end)
- Date: 2026-10-14

## Context

Some work must happen after a change commits, even if the process dies right
after: notifying other systems, sending emails, scheduled jobs. Publishing to
a broker after commit loses messages on a crash, and publishing before commit
sends messages for changes that roll back. A broker (Kafka, RabbitMQ) also
adds infrastructure that a single service does not otherwise need.

## Decision

`domainkit.contrib.messaging` writes deferred tasks and durable events to an
outbox table in the same transaction as the change. Worker processes claim
messages with `FOR UPDATE SKIP LOCKED`, run them with a timeout, retry with
exponential backoff, and dead-letter after `max_attempts`. Periodic tasks are
scheduled through the same table, deduplicated per interval across workers.

## Consequences

Delivery is at least once, so handlers and tasks must be idempotent, and
there is no ordering between messages. Throughput is bounded by PostgreSQL.
On a laptop, one worker kept up with about 300 events per second, the most
the load test produced.
If cross-service streaming is needed later, a relay can forward outbox rows
to a broker without changing the domains.

## Amendment (2026-11-01, the 0.15 audit): unknown targets and the deploy order

A message names its task or durable handler by name, and the worker that
claims it looks the name up in its own process. Until 0.15 a name it did
not know dead-lettered the message at once (CONC-15). In a rolling deploy
that is the normal case for a moment: the new web processes commit
messages for a task or handler the new release adds while older workers
still claim, and a task moved to another module changes its default
name, `module.function`, so the new workers do not know the messages the
old code queued. Each was dead-lettered and reported, an operator had to
retry it, and a dead letter let its ordering key's later messages run
before it (ADR 0118), which a retry cannot undo.

Three answers were weighed: keep the dead letter and only document the
order (one wrong step still loses the key's order); retry as any failure
(spends `max_attempts`, a task of one attempt dies anyway); or give the
message back without spending an attempt, for a bounded time. The owner
chose the last.

- **A grace.** `MESSAGING.UNKNOWN_TARGET_GRACE` (3600 seconds by default)
  is how long a message whose target a worker does not know waits for one
  that does. The worker gives it back pending, its attempt not counted,
  due after a delay that doubles (the time it has waited so far, between
  `RETRY_BASE_SECONDS` and `RETRY_MAX_SECONDS`, and never past the end
  of the grace), records when a worker first found it unknown in its
  headers, and counts the outcome `unknown`. Pending, it keeps its
  ordering key's place. Once the grace has passed it is dead-lettered as
  before; `0` dead-letters at once, the old behaviour. The time is the
  worker's host's clock: an hour's grace does not need the database's.
  The test plugin's `drain` dead-letters at once: a test's process has
  every task.
- **The deploy order.** Workers first when a release adds tasks or
  durable handlers, web processes first when it removes them
  (`docs/contrib/messaging.md`, `docs/operations.md`). Within the grace
  either order works.
- **Names.** A task's name is its messages' key; `@task(name=...)` keeps it
  whatever the code does, and `check` warns where deployed about tasks
  without one (`messaging.W001`). Names are at most 255 characters, the
  outbox's column (CONC-16).

Two smaller changes to this decision came with the same audit. A periodic
task is still deduplicated per interval through its row's `dedupe_key`,
but cleanup now keeps that row until its slot ends when the interval is
longer than `RETENTION_SECONDS`, so a worker that starts later in the slot
does not queue it again (CONC-13). And queueing periodic tasks no longer
holds up claiming: a failure is logged and tried again after a doubling
pause while due messages still run (CONC-16).
