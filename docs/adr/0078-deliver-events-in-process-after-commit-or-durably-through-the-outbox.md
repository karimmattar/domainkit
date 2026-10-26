# 0078. Deliver events in process after commit, or durably through the outbox

- Status: Accepted; amended 2026-10-26 (at the end)
- Date: 2026-10-25; amended 2026-10-26 (the inbox)

This records a decision taken in 0.1.0 and kept since, which no record
described (DSGN-15). It changes nothing in the code.

## Context

Aggregates record domain events; other aggregates and bounded contexts react
to them without importing each other's internals (ADR 0003). Something has
to deliver the events, and the reactions differ in what they need:

- Some are cheap and only useful at once: refresh a cached total, push to a
  live connection (ADR 0057). Losing one when the process dies is harmless;
  a delay of seconds is not.
- Others must happen even if the process dies right after the commit:
  notify another system, start a payment's next step. They need the
  outbox's guarantee (ADR 0005): written with the change, run by a worker,
  retried.

Options considered:

- **In process only** (Django's signals). Simple and fast, but a crash
  between the commit and the handler loses the reaction, and a slow
  handler holds the request.
- **Through the outbox only.** Every reaction survives a crash, but every
  event of every commit costs an outbox row and a worker round trip, the
  cheap reactions arrive late, and a project cannot react to an event
  without installing messaging and running a worker.
- **One bus, and the delivery chosen per subscription.**

## Decision

One event bus per process, `domainkit.ddd.event_bus`, which domains
subscribe to in `DomainConfig.ready()`. Each subscription chooses its
delivery:

- **In process** (the default): after the unit of work commits, the bus
  calls the event's handlers where it committed (the request, or the task
  that committed), most specific event type first, one at a time. Each
  runs within `EVENT_HANDLER_TIMEOUT` (or the subscription's `timeout=`);
  a handler that raises or overruns is logged, reported, and skipped, and
  the next one runs. A handler never undoes the commit, and nothing
  retries it.
- **Durable** (`durable=True`): when the unit of work commits, each event
  with durable handlers becomes an outbox message per handler, in the same
  transaction as the change. A worker runs it at least once, with the
  retries, timeouts, and dead letters of every task. A unit of work that
  would publish such an event without `domainkit.contrib.messaging`
  installed fails rather than lose it.

The domain layer only records events; the bus belongs to `domainkit.ddd`,
which has no I/O, and the outbox to `contrib.messaging`, which joins the
commit through its `DomainConfig` hooks (DSGN-03).

## Consequences

- An in-process handler may not run at all (a crash after the commit) and
  must not hold work that has to happen; that belongs in a durable one.
- A durable handler may run twice and must be idempotent; there is no
  inbox of processed message ids yet (FIN-80, planned).
- Moving a subscription between the two changes its guarantees, not its
  code: the handler's signature is the same.
- Order is not guaranteed between handlers, nor between durable messages.
- Tests see what was published with `captured_events`, and run durable
  work with the `outbox` fixture of messaging's pytest plugin
  (`docs/testing.md`).
- The bus is a module-level object, as the registry and the settings are;
  DSGN-07 plans a container for 1.0 that would own it per application. Its
  delivery rules would stay as written here.

## Amendment (2026-10-26, 0.11): the inbox

FIN-80's inbox exists ([0094](0094-an-inbox-of-handled-messages-written-with-the-handlers-commit.md)).
A durable handler marked `@inbox` (`domainkit.contrib.messaging`) writes
its message's id to `domainkit_inbox` in the transaction of its first
commit, and a later run of the message is skipped, so its database work
commits once. It is opt-in: unmarked handlers stay at least once and must
stay idempotent. A commit a durable handler makes on its outbox's database
after another worker took its message over is refused (`LeaseLost`,
[0095](0095-fence-a-runs-commits-with-its-lease-instead-of-a-heartbeat.md)).
In-process handlers have no message and no inbox, and run at most once, as
written above.
