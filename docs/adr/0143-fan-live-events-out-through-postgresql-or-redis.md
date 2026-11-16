# 0143. Fan live events out through PostgreSQL or Redis

- Status: Accepted; amended by [0157](0157-priorities-on-rabbitmq-an-unpartitioned-outbox-and-audit-trail-and-live-routes-in-several-processes.md)
- Date: 2026-11-15

## Context

[ADR 0057](0057-live-committed-events-in-process.md) streams committed
events to clients in process: a client sees the events its own replica
commits. It recorded a fan-out through the broker for later. A project
that runs an identity service on domainkit found that the events its
clients wait for are mostly committed elsewhere (user report L1): by the
worker (a verification decided, an invitation expired), or by another
replica (the phone approves a sign-in on one, the dashboard's stream is
open on another). A stream that delivers some events and silently drops
the rest is worse than polling, so the project polls.

The project runs PostgreSQL and Redis; many do, and some run no broker.
The options:

- **The broker.** ADR 0057's sketch: a fan-out exchange with a queue per
  replica. The broker port has work queues only, each message for one
  consumer, and a project without a broker gets nothing.
- **PostgreSQL's `LISTEN`/`NOTIFY`.** Every process already reaches the
  database; the outbox wakes workers this way. A notification is
  delivered to every session listening on its channel, once its
  transaction commits, and lost by a session that was not listening. Its
  payload holds less than 8,000 bytes; it goes through the server's
  notification queue, which a listener that stops reading fills.
- **Redis publish/subscribe.** A cache many projects share already; a
  message goes to every subscriber connected at the time, and is lost by
  the others. No size worth worrying about.

Which events to send is the second question. A worker has no
subscriptions, so it cannot know which events some replica's clients
watch; sending every domain event would cost a round trip for most of
them for nothing.

## Decision

- `LIVE_FANOUT = {"BACKEND": "postgres" | "redis", "ALIAS": ...,
  "CHANNEL": "domainkit_live"}` turns it on: `ALIAS` names a database of
  `DATABASES` (PostgreSQL, through asyncpg, not through PgBouncer's
  transaction pooling) or a cache of `CACHES` (Redis, not a cluster).
  Without it, live events stay in process, as before.
- A domain shares the event types its clients watch, in `ready()`:
  `live_events.share(OrderShipped)`. Every process loads the same
  domains, so every process knows them: those it sends, and those it
  reads back.
- When a unit of work commits, the bus publishes its events in process,
  as before: the committing process's subscribers get them at once.
  Each shared one is also handed to the process's sender, a background
  task of its event loop, which sends `{origin, type, data}` as JSON:
  `pg_notify` on the database, in one statement per batch, or `PUBLISH`
  on the cache. The commit never waits on it, and a failure is logged.
  An event whose message would not fit a notification (8,000 bytes) is
  not sent, and logged.
- Each process that has subscribers runs a listener, from its first
  subscription: a connection of its own that listens on the channel, or
  a Redis subscription. It publishes each event another process sent
  (its origin differs) to its subscribers, as if committed here. A lost
  connection is opened again after a pause that doubles; events sent
  meanwhile are missed, as a client that reconnects misses them.
- The message carries the event's fields, `Personal` ones included,
  since access rules decide from them. It goes between the application's
  processes, through infrastructure clients never reach; clients still
  get the fields `render` gives them, `Personal` ones left out by
  default.
- A subscription to a type that is not shared, while the fan-out is on,
  is logged once: only this process's commits reach it. `check` warns,
  in staging and production, when domains share types and no fan-out is
  set (`live.W003`).

## Consequences

- A client sees the events committed by any process that loads the
  domains, the worker included, within a round trip of the commit.
- Delivery stays best effort: a listener that reconnects misses what was
  sent meanwhile. Routes that must not lose events still replay from the
  client's `Last-Event-ID`.
- Each commit with shared events costs one more round trip, in the
  background. A busy stream of large events belongs on Redis rather than
  PostgreSQL, whose notification queue every listener must keep reading.
- ADR 0057's "it stays in process" is amended; the broker's fan-out it
  sketched is not built.
