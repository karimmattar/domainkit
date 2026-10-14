# 0014. Keep the outbox as the record; use the broker to dispatch

- Status: Accepted
- Date: 2026-10-14

## Context

The worker polls the outbox table ([0005](0005-transactional-outbox.md)):
every `POLL_INTERVAL`, each worker claims due rows with `FOR UPDATE SKIP
LOCKED`. That costs a query per worker per interval, adds up to a second
of latency, and gives every worker every kind of work. Teams that run
RabbitMQ want messages to start at once, spread over many workers with the
broker's flow control, and routed to separate worker pools.

The outbox must stay, whatever the broker: only a row written in the
business transaction makes "the change committed, so the work will happen"
true. The question is what the broker owns.

- **The broker owns messages after relay.** The relay publishes the whole
  message and deletes or marks the row. Retries then need delays, which
  RabbitMQ lacks: the delayed-message plugin is not on managed services,
  and its repository was archived; TTL queues with dead-letter exchanges
  need one queue per delay. Dead letters move to a queue that `messages
  --dead` cannot list, and polling and brokered workers behave differently.
- **The outbox stays the record; the broker dispatches.** The relay
  publishes a pointer, and the consumer takes the row over and runs it
  exactly as a polling worker would. Retries, delays, dead letters, and
  operator commands do not change. Each message costs one more database
  update (the take-over) than it would if the broker owned it.

Deduplication matters either way: an outbox relay is at least once (it may
publish again after a crash or a lost confirmation), and RabbitMQ redelivers
when a consumer goes away.

## Decision

- `BROKERS` holds brokers by alias, chosen by URL: `amqp://`/`amqps://`
  (RabbitMQ through aio-pika, the `rabbitmq` extra) and `memory://` (one
  process, for tests). `MESSAGING.BROKER` turns brokered workers on; without
  it, workers poll as before.
- The outbox stays the record. Messages are published as pointers: the
  message id, its database, and the lock they were published under (held
  for `REPUBLISH_AFTER`). Publications are persistent, `mandatory`, and
  confirmed; a failed one is unlocked and left to the relay.
- The committing process publishes a commit's due messages right after the
  commit (`PUBLISH_ON_COMMIT`), through a new after-commit hook,
  `DomainConfig.committed(uow)`, whose failures are reported and never fail
  the commit. Relays, in the workers, publish everything else: delayed
  tasks when due, retries, and what a commit could not publish.
  Consume-only workers (`runworker --no-relay`) never poll the database.
- A consumer takes a delivered message over with one conditional update:
  pending, due, and still locked by the delivered lock (or with an expired
  lock). Anything else is a duplicate or stale, and is acknowledged and
  skipped. The message then runs through the polling worker's code, and the
  delivery is acknowledged after the outcome is recorded.
- Retries return to the outbox with their delay; dead letters stay there.
  A message RabbitMQ loses is published again after `REPUBLISH_AFTER`, and
  one whose worker dies returns after `VISIBILITY_TIMEOUT`.
- Topology: one durable direct exchange, `<PREFIX>.work`, and a quorum
  queue per queue, `<PREFIX>.<queue>`, bound by name. Messages carry a
  queue, stored in the outbox (`@task(queue=...)`), so polling workers and
  brokered workers can both be limited to queues.
- In both modes, a message whose worker died during its last attempt is
  dead-lettered instead of being retried forever.

## Consequences

- Work starts as soon as the commit's process publishes it, workers scale
  out with the broker's prefetch, queues route work to separate pools, and
  only the relaying workers poll the database.
- Each request that writes messages pays one more database update and one
  broker round trip after its commit; `PUBLISH_ON_COMMIT = False` moves
  that cost to the relays, at the price of their polling delay.
- The database still records every message: the publication's lock, the
  take-over, and the outcome are each one update.
- Polling and brokered workers behave the same way (at least once,
  unordered, idempotent handlers), so switching costs a setting, not code.
- A RabbitMQ outage delays messages but loses none; the relay publishes
  them once it is back.
- Projects upgrading must migrate the outbox for its `queue` column.
- Integration events for other services, and consumers of theirs, are a
  separate decision; they will reuse `BROKERS`.
