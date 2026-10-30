# 0118. Run a key's messages in commit order by holding the later ones in the outbox

- Status: Accepted; amended 2026-10-30 (at the end)
- Date: 2026-10-30
- Amends: [0005](0005-transactional-outbox.md)

## Context

ADR 0005 settled that "there is no ordering between messages": workers
claim due messages by priority and age, any number at once, and a retry
goes back with its delay while later messages run. Payment status
transitions, ledger postings to another system, and webhooks sent per
object need the messages about one thing to run one at a time, in the
order they were committed (FIN-19). A PoC (four workers sharing one
PostgreSQL outbox, three keys of ten messages committed interleaved) found,
on 0.13, 15 to 18 overlaps (two messages of a key running at once) and 7
to 22 inversions (a later message started before an earlier one) per run.
Through a broker the same happens, more rarely: 0 or 1 overlap and 1 or
2 inversions per run of twenty messages.

An ordering key fixes that: messages with one key run one at a time, in
commit order, across every worker and replica; messages of other keys, and
those without one, keep running in parallel. Four questions had to be
answered.

**Where the line is kept.** Two designs were measured on PostgreSQL 16,
with 300,000 finished messages, 5,000 due ones without a key, and a hot
key whose first message runs while 50,000 more wait:

- *In the claim*: claim a message only if no earlier unfinished message of
  its key exists (`NOT EXISTS`, or `DISTINCT ON`). The claim walks the
  priority index as before, and probes each blocked message of the key on
  its way: 515 ms and 151,119 pages per claim, growing with the backlog.
- *In the outbox's statuses*: only the first unfinished message of a key is
  `pending`; the ones after it are `held`, and the transaction that
  finishes a message makes the key's next one `pending`. The claim is not
  changed at all: 0.115 ms and 108 pages. Holding and promoting cost a
  probe of a partial index on the key's waiting messages, 4 or 5 pages and
  0.02 to 0.07 ms in the generic plan a prepared statement gets.

**Commit order.** A sequence or a timestamp taken when the row is written
is not commit order: two transactions can commit in the other order. Each
writer of a key must wait for the previous one to commit, and decide
whether its message is first with every earlier commit of the key in
sight. A transaction advisory lock on the key serializes the writers, but
a `REPEATABLE READ` or `SERIALIZABLE` unit of work (ADR 0092) decides on
its snapshot, which can predate the lock: it would see the key's last
message still waiting, or not see a message committed meanwhile, and two
messages of a key would be `pending`. A row per key, in a table of its
own, which every writer of the key and every transaction that finishes
one of its messages writes first (an upsert that adds to the key's
sequence), serializes them the same way, and PostgreSQL refuses a stale
snapshot's write to it with a serialization failure, which `run` retries
(a test reproduces both cases). On SQLite one writer runs at a time
anyway.

**The rest of the key after a dead letter.** A message that fails holds
its key while it is retried. Once it is dead-lettered, two choices: the key
goes on (*continue*), or it stops until an operator retries or cancels the
dead message (*park*). The PoC ran a key of ten messages whose third
failed until fixed, then was retried:

- *continue*: 9 of 10 ran before the fix; the dead one ran after the fix,
  after messages committed later than it.
- *park*: 2 ran; 7 waited until the fix, then all ran in order. Parking
  also needed new messages to queue behind a dead one, and a retried
  one to be first when nothing is pending, or the key never moved again.

**The broker.** RabbitMQ hands a queue's messages to any consumer, so the
order must come from somewhere. Routing each key to a partition queue with
a single active consumer (the consistent-hash exchange) needs a plugin,
caps a queue's parallelism at its partition count, and does not survive a
retry: a failed message goes back to the outbox with its delay (ADR 0014)
and is published again later, behind messages of its key published
meanwhile. The database has to decide anyway. With the outbox holding a
key's later messages back, the relay publishes only each key's first one;
the PoC (an in-memory broker, two keys of ten messages, sixteen consumers)
ran them without an overlap or an inversion in 0.37 s when the consumer
that finished a message published the key's next itself, against 8.24 s
when the next waited for the relay's poll (`POLL_INTERVAL`, 1 s).

## Decision

- **API.** `uow.defer(task, ordering_key=...)` and `enqueue(...,
  ordering_key=...)` give a message its key; `@ordered("name")` or
  `@ordered(function)` declares a task's or a durable handler's, from the
  task's argument or the event's attribute. A key is 1 to 255 characters;
  keys are shared by every task and handler of an outbox, and a chain's
  steps carry the first step's key.
- **The outbox.** Two columns, `ordering_key` and `sequence`, a `held`
  status, a partial index on `(ordering_key, sequence)` of the waiting and
  dead keyed messages (`ix_domainkit_messages_ordering`), and a table of
  keys, `domainkit_ordering_keys` (each key's last sequence, and its turn).
  A keyed message is written `held` when an earlier one of its key waits;
  the transaction that records a message of a key as done, dead, or
  cancelled makes the next one `pending`. The claim, the relay, and their
  index do not change.
- **Commit order.** Every transaction that writes, finishes, cancels, or
  retries a key's messages first writes the key's row, keys taken in one
  order. A `REPEATABLE READ` or `SERIALIZABLE` writer whose snapshot
  predates another transaction's write of the row fails with a
  serialization failure, which `run` retries.
- **Failures.** A failed message stays `pending`, so a retry holds its key,
  as does a message over its `rate`, a delayed one, and one whose worker
  died, until its lease ends. A dead-lettered message stops holding its
  key: the rest runs (*continue*). Retried, it is held in its place, before
  the key's messages still waiting, after the one running, if any. A key
  that must stop on a failure gives its task a high `max_attempts`: the
  message keeps retrying in place, holding the key, until it succeeds or
  is fixed; this is *park* without a mode of its own. Continue is what
  ordered queues commonly do once a message is dead-lettered (Pub/Sub
  ordering keys, SQS FIFO groups, Service Bus sessions), and a stopped key
  stops silently: every later message about one account waits on one
  operator.
- **The broker.** The database arbitrates. The relay publishes only
  pending messages, as before; a consumer that finishes a keyed message
  publishes the key's next one at once.
- **Cleanup.** Workers delete a key's row once none of its messages waits or
  is dead and it was not written for `RETENTION_SECONDS`, in batches, with
  the finished messages.

## Consequences

- Messages without a key cost nothing more: they are written as before, are
  not in the new index, and finishing one touches no key.
- A keyed write costs one upsert per key and one insert per message (they
  cannot share a statement: each sees the ones before), and finishing a
  keyed message costs an upsert and an update. One key's messages run one
  after another, so a key's throughput is one message at a time, plus a
  round trip; spread work over keys.
- The order holds within one outbox: messages of one key written to two
  databases (two shards) are two lines. A key whose messages go to queues
  no worker serves waits for one; a delayed message holds its key until
  its time.
- Projects migrate before deploying (`makemigrations`, `migrate`): the
  worker's claim reads the new column.
- A dead letter is out of its key's order for good once the rest ran; the
  docs say so, and how to stop a key instead.
- `check` warns (`blocking.W002`) about event handlers and tasks written as
  `def`, which hold the event loop, and so every key the process runs,
  then fail.

## Amendment (2026-10-30): after the security review of 0.14

The review (SEC-14c) found four gaps; each is closed as follows.

- **A key can stall (SEC-14c-3).** Only the transaction that finishes a
  key's message made the next one pending. A process that does not know
  the `held` status finishes a message without that: a 0.13 worker during
  the rolling deploy this release's upgrade asks for, or an operator's own
  `UPDATE`. The key's held messages then wait for good, and every later
  message of the key is held behind them. Each worker's cleanup round
  (`prune_keys`) now finds keys with held messages and none pending,
  through the ordering index, and promotes each one's first under the
  key's turn, logging the message; `messages --release` does the same
  at once. The upgrade guide asks to stop 0.13 workers before 0.14
  processes write keyed messages; the repair covers what slips through.
- **Where a retried dead letter goes (SEC-14c-1).** The decision said it
  goes "before the key's messages still waiting", but the code held it
  behind any pending message, so a message promoted when the older one
  died, and not yet started, ran first. Under the key's turn, a pending
  message of the key that comes after the dead one, that no worker has
  started (`started_at`) and no relay holds for the broker (a live lock),
  is held again, its expired lock cleared, and the dead one becomes
  pending. One a relay holds keeps its turn: its pointer may be in a
  queue, and a consumer would run it whatever its status; holding it
  back would leave a stale pointer the consumer skips, and the message
  locked until `REPUBLISH_AFTER`. So a retried dead letter goes after
  the message under way (running, or handed to the broker), as before.
- **A misnamed key (SEC-14c-2).** `@ordered("name")` on a task whose
  arguments do not include `name` (nor `**kwargs`) is refused when the
  task registers, or when it is marked if it registered first. Keys no
  outbox can store (over 255 characters, or holding NUL) are refused where
  they are given.
- **Two clocks (SEC-14c-4).** Operator queries (`cancel`, the listings,
  the lag) judged leases by the process's clock, while workers lock by the
  database's on PostgreSQL. `dbclock.now` alone does not settle it: it
  follows a frozen clock in tests, and a host's clock is not the
  database's. A lease now counts as alive while either clock says so (the
  earlier of the two, `admin.lease_now`): an operator's host whose clock
  runs ahead never cancels a running message.

An index on the keys' `touched_at`, for the cleanup of idle keys, was
measured and left out: at a million keys the cleanup's batch takes 78 ms
without it and 12 ms with it, once a `CLEANUP_INTERVAL`, while every write
and finish of a keyed message updates `touched_at`, which the index would
make a non-HOT update with an index entry of its own.
