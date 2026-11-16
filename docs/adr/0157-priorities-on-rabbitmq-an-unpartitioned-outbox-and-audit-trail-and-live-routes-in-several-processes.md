# 0157. Priorities on RabbitMQ, an unpartitioned outbox and audit trail, and live routes in several processes

- Status: Accepted
- Date: 2026-11-16
- Amends: [0043](0043-the-outbox-database-decides-when-work-is-due.md), [0143](0143-fan-live-events-out-through-postgresql-or-redis.md)

## Context

A project that runs an identity service on domainkit listed limits of
the worker and of live events (user reports W9 and L1). The audit of the
report found three still open.

**Priorities under a broker.** A task's `priority` (-100 to 100) orders
the outbox's claims ([ADR 0035](0035-task-policies-priorities-chains-in-the-outbox.md)).
With `MESSAGING.BROKER`, the relay publishes each round in priority
order, but `Broker.publish` takes no priority: the broker hands out what
it holds in the order it came, so a priority counts only while its
message waits in the outbox.

- RabbitMQ. domainkit declares quorum queues. Classic queues take
  priorities through the `x-max-priority` argument, fixed when the queue
  is declared: "Priority queues can never change the number of
  priorities they support after queue declaration", and policies cannot
  set it [1]. RabbitMQ refuses a queue declared again with other
  arguments (`PRECONDITION_FAILED`), so no queue in use could gain it.
  Quorum queues need no argument and ignore `x-max-priority` [2]. From
  RabbitMQ 4.0 they map a message's priority onto two levels: normal (0
  to 4, or none) and high (above 4), and hand out two high messages for
  each normal one [3]. From 4.3 they hand out strictly by 32 levels, 0 to
  31, a message without a priority counting as 4; a requeued message
  loses its priority, and a steady stream of higher ones holds lower ones
  back [2]. Before 4.0, quorum queues have no priorities. Either way a
  priority orders only the messages that wait in the queue: those within
  a consumer's prefetch are delivered already [1].
- Redis Streams, NATS JetStream, Kafka, and SQS have no message
  priority. An adapter could emulate it with a stream, subject, topic or
  queue per band of priorities, and consumers that read the higher band
  first. That is what a project gets today from a queue of its own for
  urgent work, and workers of its own (`@task(queue=...)`, `runworker
  --queue`), which `docs/contrib/messaging.md` advises.

**Partitioning the outbox and the audit trail.** `partition_by_range`
([ADR 0010](0010-postgresql-range-partitioning.md)) needs the timestamp
in the primary key: on a partitioned table PostgreSQL requires that
every unique constraint's "columns must include all of the partition key
columns", because "the individual indexes making up the constraint can
only directly enforce uniqueness within their own partitions" [4]. The
outbox's key is `id`, with a unique `dedupe_key`; the audit trail's is
`id`.

- The outbox. [ADR 0043](0043-the-outbox-database-decides-when-work-is-due.md)
  decided "no partitioning, for now". Its reasons hold, and there are
  more. With the time in `dedupe_key`'s constraint, a periodic slot that
  two workers queue at different times would be written twice, where it
  is written once. Pending, held and dead messages live in every period
  (a delay may be a year; a dead message stays until dropped), so
  dropping a partition by age would drop messages that still run; the
  cleanup deletes finished rows only. Partitioning by status turns every
  outcome into a move between partitions, which PostgreSQL makes a
  delete and an insert, and a concurrent `UPDATE` or `DELETE` of the
  moved row "will get a serialization failure error" [5]: the claims,
  takeovers and settles of workers that run side by side.
- The audit trail. It only grows, and by time: the case partitioning is
  made for. But its key is `id`, and a minor release does not change a
  stored primary key; nor can PostgreSQL "turn a regular table into a
  partitioned table" [4]: each project would copy its trail into a new
  table with its triggers off. Its retention follows the chain, not the
  calendar: `audit prune` deletes below a signed checkpoint, by seal
  number. Rows are sealed in `(occurred_at, id)` order, but a row that
  commits late is sealed later (`audit seal --late`), with a higher
  number, so a month's partition can hold rows above the checkpoint.
  Dropping it by age would remove sealed rows, and `audit verify` would
  report them missing, as it reports tampering. Its append-only
  protection is a table's: PostgreSQL clones row triggers to each
  partition, but a statement trigger fires only for the table a
  statement names [6], so a `TRUNCATE` of one partition skips the
  parent's `BEFORE TRUNCATE` trigger (checked on PostgreSQL 16), and the
  startup check reads the parent only. Each tenant schema keeps a trail
  of its own, whose partitions `partitions` would have to keep too.
- The seals and checkpoints are small rows; a unique `entry_id` keeps one
  seal per row. SQLite has no partitions: `partition_by_range` leaves a
  plain table there (ADR 0010).

**Live routes in several processes.** [ADR 0143](0143-fan-live-events-out-through-postgresql-or-redis.md)
has `check` warn (`live.W003`) when domains share event types and
`LIVE_FANOUT` names no backend. A project that serves live routes and
shares nothing yet, as each one starts, gets no warning, though its
clients miss what the worker and the other processes commit. What the
framework can see:

- the live routes: WebSocket routes, and routes whose endpoint returns
  an `EventSourceResponse`;
- the server's worker processes, from its command line in its own
  process, or the variables each server reads in place of its options,
  as `servers.W001` reads them: uvicorn's `--workers`, `UVICORN_WORKERS`
  and `WEB_CONCURRENCY`; gunicorn's `-w`, `GUNICORN_CMD_ARGS` and
  `WEB_CONCURRENCY` [7]; Granian's `--workers` and `GRANIAN_WORKERS`;
- whether messaging is installed: its worker always runs in a process of
  its own.

It cannot see replicas behind a load balancer.

## Decision

- **Priorities on RabbitMQ.** `Broker.priorities`, false by default,
  says whether a broker hands out a queue's waiting messages by
  priority. A broker that does takes `priority` in `publish`: the
  message's, -100 to 100. The relay and the commit publisher give each
  message's priority to such a broker only, so an adapter written before
  has nothing to change. RabbitMQ's adapter does: each message carries
  `amqp_priority(priority)`. 0 is 4, what a quorum queue gives a message
  without one; 1 to 100 spread over 5 to 31, and -100 to -1 over 0 to 3,
  in order, so a positive priority is high on RabbitMQ 4.0 to 4.2 too.
  No queue argument changes: queues already declared keep working.
- **The other brokers keep none.** Redis Streams, NATS JetStream, Kafka
  and SQS publish as before. Where deployed, `check` warns
  (`messaging.W004`) when tasks name a priority and the class of
  `MESSAGING.BROKER` has no priorities, and says to give urgent work a
  queue and workers of its own. No band of streams is built.
- **No partitions for the outbox or the audit trail in 1.x.** ADR 0043's
  decision stands, with the reasons above, and the audit tables keep
  their keys. Their size stays with what deletes from them in batches,
  off the claim and write paths: the outbox's cleanup
  (`RETENTION_SECONDS`, `CLEANUP_BATCH_SIZE`) and `audit prune`. Revisit
  for 2.0 if they cannot keep up, measured first. A partitioned audit
  trail would need the key `(id, occurred_at)`, a prune that drops a
  partition only once each of its rows is at or below the checkpoint,
  the triggers on every partition, and a startup check of each.
- **`live.W003` for live routes served by several processes.** Where
  deployed, with no `LIVE_FANOUT` backend, it also warns when no type is
  shared, the application serves live routes, and events are committed
  in more than one process: the server starts more than one worker
  process, or messaging is installed. It names the routes and the
  processes. One process, a server it cannot find, or no live route: no
  warning. A deployed application logs it when it starts too, from its
  own command line, as it logs `servers.W001`.

## Consequences

- On RabbitMQ 4.3 and later, a task's priority orders the messages that
  wait in its queue, as it orders the outbox. On 4.0 to 4.2, positive
  priorities go out two to one before the rest; before 4.0, they are
  ignored. A priority reorders nothing within a consumer's prefetch
  (`CONCURRENCY` per queue), nor across queues, and a requeued delivery
  loses it.
- A broker adapter outside domainkit works as before, and gains
  priorities by setting `priorities` and taking `priority`.
- A project on Redis Streams, NATS JetStream, Kafka or SQS whose tasks
  name priorities sees `messaging.W004`.
- The outbox and the audit trail keep their keys and unique constraints:
  one row per dedupe key, one seal per row, and the append-only triggers
  hold as they do. Deleting in batches leaves dead rows for autovacuum to
  reclaim, which a dropped partition would not.
- A project that serves live routes from several processes, or beside
  the worker, without a fan-out, sees `live.W003`; the cure is ADR
  0143's: share the types and set `LIVE_FANOUT`. One whose live routes
  stream no committed events silences it. Replicas each running one
  process, with no worker, are not seen and not warned.

## Sources

1. RabbitMQ, "Classic Queues Support Priorities":
   <https://www.rabbitmq.com/docs/priority>
2. RabbitMQ, "Quorum Queues", Message Priorities:
   <https://www.rabbitmq.com/docs/quorum-queues#priorities>
3. RabbitMQ blog, "Quorum Queues in 4.0" (2024-08-28):
   <https://www.rabbitmq.com/blog/2024/08/28/quorum-queues-in-4.0>
4. PostgreSQL 16, "Table Partitioning", 5.11.2 and its limitations:
   <https://www.postgresql.org/docs/16/ddl-partitioning.html>
5. PostgreSQL 16, `UPDATE`, Notes:
   <https://www.postgresql.org/docs/16/sql-update.html>
6. PostgreSQL 16, `CREATE TRIGGER`, Notes:
   <https://www.postgresql.org/docs/16/sql-createtrigger.html>
7. Gunicorn, settings (`workers`, which defaults to `WEB_CONCURRENCY`):
   <https://gunicorn.org/reference/settings/>; uvicorn reads
   `WEB_CONCURRENCY` when `--workers` is not given (`uvicorn/config.py`),
   and both uvicorn and Granian read their options from variables with
   their prefix (`UVICORN_`, `GRANIAN_`).
