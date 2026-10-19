# 0035. Keep per-task policies, priorities, chains, and rates in the outbox

- Status: Accepted
- Date: 2026-10-19

## Context

Every task shared `MESSAGING`'s timeout and retry delays, and the worker
took due messages oldest first. Projects need a slow export to have a
longer timeout than an email, a payment call to retry only on network
errors, urgent work to jump the queue, a task to start when another
succeeds, a call to a rate-limited API to stay under its limit, and
operators to see and cancel what is waiting. Celery has all of these; Django
6.0's tasks have priorities from -100 to 100, higher first.

The outbox is the record of every message ([0005](0005-transactional-outbox.md),
[0014](0014-outbox-as-record-broker-for-dispatch.md)), so each feature had
to say what it stores there, and keep polling and brokered workers alike.

## Decision

- **Policies** are task options: `timeout`, `max_attempts`, `backoff`
  (`Backoff`: base, cap, exponential or fixed, jitter), `retry_on`, and
  `dont_retry_on`; `MESSAGING` stays the default. A timeout at or above
  `VISIBILITY_TIMEOUT` stops the worker from starting. Delays are capped
  at a year. `current_attempt()` tells a task which run it is.
- **Priorities** use Django's range and direction, are stored per message
  in a `priority` column, and order claims (`priority DESC, available_at`)
  through a new index. Brokers deliver in queue order; priorities act
  while messages wait in the outbox, and queues remain the way to isolate
  work.
- **Chains** keep only the next step in the outbox: the remaining task
  keys travel in the message's headers, and the next row is written in the
  transaction that marks the previous one done, deduplicated by the
  previous message's id. A step's result, or its arguments when it
  returns `None`, becomes the next step's arguments.
- **Rates** are counted by `RATE_LIMIT_BACKEND` under `task:<key>`, so a
  shared Redis backend limits every worker and the memory one each
  process. A message over its rate goes back without spending an attempt.
- **Inspection and cancelling.** A `started_at` column marks a message a
  worker runs, which tells it apart from one a relay published. A message
  may be cancelled (a `cancelled` status) unless it is running; one
  conditional update decides against a concurrent claim.
- **Trace context.** Messages carry `traceparent` and `tracestate` in their
  headers; the worker validates them and continues the trace.
- **Test support** reads committed rows through a list of watchers called
  after each commit, which production leaves empty.

## Consequences

- Projects must migrate the outbox (two columns, an index) before running
  this version's code; older workers ignore the new columns.
- A claim sorts by priority as well as age; the index keeps that cheap.
- Rates enforced in memory multiply by the number of worker processes;
  the documentation says to use Redis for a shared limit.
- Messages a worker writes (chain steps, webhook deliveries) are left to
  the relay rather than published on commit, so a stopping worker cannot
  strand them under a publication lock.
