# 0142. While the broker is away, relaying workers run the outbox

- Status: Accepted; amended 2026-11-18 (at the end), by [0187](0187-keep-the-workers-heartbeat-to-its-liveness-fail-deliveries-whose-task-ended-and-share-provider-clients.md)
- Date: 2026-11-15

## Context

[ADR 0014](0014-outbox-as-record-broker-for-dispatch.md) kept the outbox
as the record and gave the broker the dispatch: with `MESSAGING.BROKER`
set, workers take their work from the broker only. One of its
consequences reads "a RabbitMQ outage delays messages but loses none".
A project that runs an identity service on domainkit found what that
delay costs (user report W3): while RabbitMQ was away, every one-time
code, notice, and background job stopped, though PostgreSQL, which held
every one of them, was fine. Meanwhile each relay leased, unlocked, and
warned about each message every `POLL_INTERVAL`; only commits had a
breaker ([ADR 0042](0042-lease-publications-and-break-the-circuit.md)).

A worker without a broker already runs the outbox: it claims due rows
with `FOR UPDATE SKIP LOCKED` and runs them. Under 0014 the brokered
consumer runs a delivery through the same code, after taking the row over
with a conditional update that holds only for the lock the delivery
carries. So a message run from the outbox, whose copy the broker still
holds, is skipped when that copy arrives: its row carries another lock,
or is done. Nothing runs twice for running the outbox while a broker
holds copies.

The options considered:

- **Keep waiting for the broker.** Simple, and what 0014 chose; the
  outage of one component stops the work of the whole application.
- **Run the outbox from every worker, always.** Brokered workers would
  poll the database as polling workers do, and the broker would add
  nothing but load; consume-only workers exist precisely not to.
- **Run the outbox from the relaying workers while their broker is
  away.** The relays poll the outbox already; a breaker tells them when
  the broker is away, as commits' breaker does.

## Decision

- Each relaying worker has a breaker per outbox, of
  `BREAKER_FAILURES` and `BREAKER_COOLDOWN`, as commits' breaker has.
  Its relay publishes through it: failed publications count, a
  confirmation closes it.
- While it is open, the relay does not publish. It claims the due
  messages of the worker's queues and runs them, as a worker without a
  broker does, within the process's budget, which deliveries share. Its
  polls do not back off then, since commits publish nothing.
- Once the cooldown is over, the next round checks the broker before any
  row is leased. An answer closes the breaker, and the relay publishes
  again; a failure opens it for another cooldown. It logs once when it
  opens, and once when it closes.
- The next message of an ordering key, which a finished message makes
  pending, is published at once only while the breaker lets it; else the
  relay runs it from the outbox in turn.
- Messages the broker held before the outage stay locked in the outbox
  until `REPUBLISH_AFTER`: then the relay takes them as lost, and runs
  them too. Their copies, delivered later, are skipped.
- Consume-only workers (`runworker --no-relay`) never touch the outbox:
  they wait for the broker, as before.

## Consequences

- A broker outage delays what the broker held for up to
  `REPUBLISH_AFTER`, and what was committed meanwhile for a poll: codes
  and notices go out while the database is up.
- The relaying workers do the work of the whole pool during an outage:
  run enough of them, or enough `CONCURRENCY`, for the load.
- A relay that cannot reach the broker no longer leases and unlocks each
  message every `POLL_INTERVAL`.
- The relays' breakers are not `messaging_publisher_breaker_open`, which
  stays commits'; their log lines say when a relay runs the outbox.
- 0014's consequence on outages is amended; its decisions stand.

## Amendment (2026-11-18): the heartbeat lets the relays carry on

`runworker --heartbeat-file` aged while the broker was away, so the
documented health check restarted the relaying workers this decision
relies on (audit A28-REL-02). [0187](0187-keep-the-workers-heartbeat-to-its-liveness-fail-deliveries-whose-task-ended-and-share-provider-clients.md) makes the heartbeat the worker's
liveness alone, rewritten whatever its checks find, and reports the
checks in `--ready-file`. A stopping relay waits for what is left of
`DRAIN_TIMEOUT` only while it runs messages it claimed from the outbox,
or its breaker is not closed; one still publishing gets `HELPER_GRACE`
(A28-TEST-07).
