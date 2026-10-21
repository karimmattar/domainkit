# 0044. Run one concurrency budget per worker process

- Status: Accepted
- Date: 2026-10-20

## Context

`MESSAGING.CONCURRENCY` was documented as the messages one worker process
runs at once, but each source of work counted on its own (re-audit R23):

- `runworker` started one polling worker per outbox, each with
  `CONCURRENCY` slots (IND-A4), so a process serving four shards ran four
  times as many messages, and opened as many connections;
- `runworker --once` ran `BATCH_SIZE` messages at once, for every outbox
  at the same time (ASYNC-19);
- RabbitMQ consumers each had a prefetch of `CONCURRENCY` (IND-A5), so a
  worker of five queues ran up to five times as many;
- the in-memory broker's pumps each held a slot while their queue was
  empty, so idle queues starved busy ones (IND-A9).

For RabbitMQ, a prefetch shared by the channel (`basic.qos` with
`global=true`) would bound the deliveries, but quorum queues, which the
worker uses (ADR 0014), refuse it with `NOT_IMPLEMENTED`, and RabbitMQ 4.3
quietly turns it into a prefetch per consumer. Splitting `CONCURRENCY`
between the queues would leave slots idle whenever one queue is busy and
the others are not.

## Decision

- **One `Budget` per worker process**, of `CONCURRENCY` slots, behind an
  asyncio-native counting semaphore that serves waiters first come, first
  served. `runworker` creates it and hands it to every outbox's worker and
  to the broker's consumers; `Worker` and `BrokerWorkers` take one as
  `budget=` (default: one of their own).
- A polling worker waits for a free slot, then claims as many messages as
  are free (at most `BATCH_SIZE`) and gives back what it did not claim.
  `run_once` does the same, so `--once` stays within the budget. A message
  holds its slot until its outcome is recorded.
- Broker deliveries take a slot before they run. RabbitMQ keeps a
  prefetch of `CONCURRENCY` per consumer, so a delivery may wait for a
  slot, unacknowledged; one that is still waiting when the worker stops is
  given back to the broker once consuming has stopped. A test against
  real RabbitMQ (`test_rabbitmq.py`) checks that several queues never run
  more than the budget at once.
- The in-memory broker serves all the queues of a subscription from one
  pump, in turn, which holds at most one slot while it waits.
- Task sets and `gather` give way to `asyncio.TaskGroup`, so a failure in
  one task (a bug in a relay or in cleanup) ends the worker with an error
  instead of being lost (R15). Drains keep their grace period, then cancel.

## Consequences

- `CONCURRENCY` means what it says: the most messages a process runs at
  once, and so a bound on the connections and memory they use. A process
  that served several outboxes or queues runs fewer at once than before;
  raise `CONCURRENCY`, or run more processes.
- On RabbitMQ, a worker of `n` queues may hold up to `n × CONCURRENCY`
  deliveries, unacknowledged; those beyond the budget wait in the process.
  Their wait counts towards RabbitMQ's `consumer_timeout`, so keep
  `TASK_TIMEOUT` times the number of queues well under it, or give busy
  queues workers of their own.
- `run_once` no longer runs a whole batch at once when the budget is
  smaller than `BATCH_SIZE`.
