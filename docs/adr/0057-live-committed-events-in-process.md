# 0057. Stream committed events to clients in process, filtered per event by an access rule

- Status: Accepted
- Date: 2026-10-20

## Context

Clients want to see what changed without polling: an order shipped, a
document edited. The domain already says what changed, in the domain
events its aggregates record, which the unit of work publishes after the
commit (`ddd/events.py`), in process, and writes to the outbox for durable
handlers when messaging is installed.

Questions to settle:

- **Where events come from.** The in-process bus after the commit, or the
  outbox through the broker. The bus is immediate and costs nothing, but
  only sees the commits of its own process. The outbox sees every commit,
  but the broker port is a set of work queues: each message goes to one
  consumer, where every replica would need its own copy.
- **Who sees what.** The route's rule decides who may connect. Events of
  one type concern different people (my order, your order), so one rule
  per connection is not enough: each event must be checked.
- **Where the rule runs.** In the committing request, for every
  subscriber, or in each subscriber's own task. The first makes a commit
  wait for every client's checks; the second puts the cost where the
  client is.
- **What is sent.** The whole event, or a rendering of it. Events may hold
  personal data, marked `Personal`, which a client should not receive by
  default.
- **Slow clients.** A buffer per client that grows, a buffer that drops the
  oldest events, or a bounded buffer that ends the subscription when full.

## Decision

- `domainkit.http.live_events.live_events` subscribes itself to every
  event of the global bus, once a connection subscribes. Publishing offers
  each event to the subscribers of its type, in their own event loop, and
  never waits or fails: a commit is not slowed by its watchers.
- Each subscriber buffers at most `LIVE_SEND_QUEUE` events its rule
  allows. One more ends the subscription as too slow (`Closed(TOO_SLOW)`:
  a 1013 close, or the end of an event stream), and the client reconnects.
  Nothing is dropped silently. Events wait for the rule in an intake of
  their own, 16 times as large, which ends the subscription the same way
  when full: counting every event of the type against the send queue let
  one user's large commit close everyone else's subscriptions. Publishing
  yields to the loop after each event, so a subscriber that keeps up takes
  a large commit one event at a time. (Amended 2026-10-21.)
- Each subscription names its event types and an access rule, which is
  required (`AllowAny()` says "everyone may see these" on purpose). In the
  subscriber's task, each event passes only if the rule's
  `has_permission` and its `has_object_permission`, with the event as the
  object, both allow it, for the connection's user as of the last check
  (ADR 0056).
- An event is sent as its type's name, its `event_id`, and its fields as
  JSON, but those marked `Personal`. `render` sends other fields.
- `stream` serves an event stream, `forward` a WebSocket, and `subscribe`
  the events themselves. All take the connection's limits (ADR 0055).
- It stays in process. A client sees the events its own replica commits.
  We record how to fan out across replicas, and do not build it now: a
  durable handler publishes each event to a fan-out exchange that every
  replica binds a queue of its own to, and feeds what it receives to
  `live_events.publish`. It needs a publish-to-all operation in the broker
  port, which only offers work queues today.

## Consequences

- Domains stream their events without new code in the domain: events are
  already recorded and published after the commit.
- A client sees only events committed after it subscribed. A route that
  must not lose any replays from its own records, after the client's
  `Last-Event-ID`.
- Behind several replicas, live events are partial until the fan-out is
  built; projects that need every event on every replica must wait for it,
  or fan out themselves through their broker.
- A rule that queries the database does so for every event and every
  subscriber: rules for live events should decide from the event and the
  loaded user.
- Events that cross access rules can only come from a rule that allows
  them; there is no default rule.
