# 0042. Lease publications, and stop publishing on commit while the broker fails

- Status: Accepted
- Date: 2026-10-20

## Context

With a broker (ADR 0014), a relay or the background publisher locks the due
messages it is about to publish, publishes a pointer to each (its id, its
database, and the lock), and a consumer takes the message over only with
that lock. The lock was set for `REPUBLISH_AFTER` (an hour) before
publishing, and released only when the broker refused. Three problems
followed:

- **A cut-short publication stranded its messages for an hour.** Shutdown
  cancelled the publisher after five seconds, less than the broker's
  `PUBLISH_TIMEOUT`, so a slow broker during a deploy left a round's
  messages locked for an hour while the log said the relay would send them
  (re-audit N1, reproduced).
- **A failing broker cost the web pool on every commit.** Each commit's
  round still locked and unlocked its rows (two transactions) and logged a
  warning per message, for messages the relay sends anyway (N5).
- **A round could be the whole backlog**: up to 10,000 ids in one
  `UPDATE ... IN` and as many publications at once (N11).

Options considered for the lock:

1. Keep the hour, and unlock on cancellation. A crash, a kill, or a failed
   unlock still strands the messages for an hour.
2. Publish first, lock after. A consumer can receive the pointer before the
   lock exists, and take-over by lock no longer works.
3. Lock with a short lease, and extend it once the broker confirms.

For the failing broker: retry less often (a backoff per round), or a
circuit breaker that skips rounds entirely while the broker is down.

## Decision

- **Leases.** A relay locks a round's messages for the broker's
  `publish_timeout` (on RabbitMQ, `CONNECT_TIMEOUT` plus
  `PUBLISH_TIMEOUT`: publishing may first connect) plus `LEASE_MARGIN`
  (five seconds). The round's publications must finish within
  `publish_timeout`. Then, in one transaction, the confirmed messages are
  locked for `REPUBLISH_AFTER` and every other message of the round is
  unlocked, whatever the reason it was not confirmed: a refusal, the
  deadline, a cancellation, a bug. That step runs shielded from
  cancellation, within `RELEASE_TIMEOUT` (five seconds); if it fails, it is
  logged and the lease expires. Both updates match the relay's own lock
  only, so a consumer that already took a message over keeps it.
- **Shutdown time.** The publishers close in shutdown's drain phase
  (ADR 0046), within the time that phase always has: it ends at half of
  `SHUTDOWN_TIMEOUT` and begins by 30% at the latest, so a fifth (3 s of
  the default 15). The flush ends `RELEASE_TIMEOUT` (or half that time,
  if less) before then, so a publication it cuts short gives its messages
  back before the phase ends; `close` returns by then even if the
  give-back is slower, which goes on in the task, and shutdown's tasks
  phase awaits it. A setting of its own (`CLOSE_TIMEOUT`, at least the
  broker's `PUBLISH_TIMEOUT`) was tried and dropped: the phase ends first,
  so it promised time shutdown does not give.
- **A circuit breaker** in the publisher. Broker calls are watched:
  `BrokerUnavailable` counts as a failure, a refusal (`BrokerRefused`) as
  an answer, and a confirmation closes the breaker. After
  `BREAKER_FAILURES` failures in a row (5), commits leave their messages
  to the relays for `BREAKER_COOLDOWN` (30 s) without touching the
  database or the broker. After the cooldown one round probes: the
  broker's `check` first, before any row is locked, then its
  publications; a failure opens the breaker for another cooldown. It logs
  once when it opens and once when it closes; a gauge,
  `messaging_publisher_breaker_open`, shows it, and
  `messaging_left_to_relay_total` counts what commits left to the relays.
  Relay rounds log one line per round, not per message.
- **Bounded rounds.** `relay_ids` and the publisher lock `BATCH_SIZE` ids
  per statement and publish at most a batch at once.
- **An owned publisher task**, supervised by `domainkit.tasks.supervise`
  (ADR 0045): a clean context, a restart with backoff after a crash, and
  an end when idle, so an idle publisher holds nothing of its event loop.
  Shutdown's drain phase closes it.

## Consequences

- A stop, a crash, or a cancellation costs at most the lease (by default
  25 seconds on RabbitMQ) of delay, never an hour, and never a message.
- A confirmed round costs one more short transaction (the extension).
  The take-over check and the at-least-once guarantee are unchanged: a
  lease that expires while a pointer is still queued only means the relay
  publishes again, and the older pointer is skipped.
- While the breaker is open, messages wait for the relays' next poll
  (`POLL_INTERVAL`) instead of going out at once. The web pool and the
  logs stay quiet during a broker outage.
- At shutdown, a publication slower than the drain phase's time is cut
  short and published by a relay later; the give-back makes that cost a
  few seconds, not the lease.
- Adapters must honour `publish_timeout`: `publish` raises
  `BrokerUnavailable` instead of outlasting it, or the breaker cannot count
  the failure (the relay's own deadline still unlocks the round).
