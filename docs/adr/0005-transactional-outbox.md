# 0005. A transactional outbox instead of a broker

- Status: Accepted
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
