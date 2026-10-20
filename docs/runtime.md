# How domainkit runs async work

domainkit serves requests on one event loop per process. Work that is not a
request (computing a cached value, checking readiness, settling an
`Idempotency-Key`) runs as background tasks of that loop. This page covers
how those tasks start, who owns them, how the process stops them, and how
long anything may wait while it does. Settings are in
[reference.md](reference.md#core).

## Background tasks: spawn and supervise

Every task the framework starts goes through `domainkit.tasks`, and a
project's own background work may too:

```python
from domainkit.cache import caches
from domainkit.tasks import spawn, supervise


async def warm_up() -> None:
    await caches["default"].get_or_set("rates", load_rates, Rates)


spawn(warm_up(), name="billing.warm_up")  # Once, in the background.

# Kept running: restarted after a crash, until shutdown.
supervise(lambda: watch_rates(feed), name="billing.rates", max_backoff=30)
```

`spawn(coro, name=...)` returns an `asyncio.Task` that:

- runs in a [clean context](#clean-contexts);
- is kept by a strong reference until it ends, so a task waiting on
  something only it holds is never collected half-way;
- logs an unexpected exception once, at ERROR, with its name, and counts it
  in `tasks_failed_total{task}`. Pass `log_errors=False` when callers await
  the task and handle its errors themselves;
- belongs to the event loop that runs it, which cancels it at
  [shutdown](#shutting-down-within-a-budget).

`supervise(factory, name=..., backoff=1.0, max_backoff=60.0)` calls
`factory()` for each run, in a task of its own. A run that crashes (or that
something other than the supervisor cancels) is logged, counted in
`tasks_restarted_total{task}`, and started again after a wait that doubles
from `backoff` up to `max_backoff`, jittered to between half and all of it.
A run that stayed up for `max_backoff` counts as healthy, so the next crash
waits `backoff` again. A run that returns ends the supervision. Cancelling
the supervisor, as shutdown does, stops the run and every restart.

Names label log lines and metrics: use fixed names, never request data.
Each event loop owns its own tasks, so the several loops of a test run or a
management command stay apart, and a loop that closes is not kept alive by
the tasks it left. See [ADR 0045](adr/0045-own-background-tasks-per-loop.md).

## Clean contexts

Context variables carry the request's facts: its id and actor
(`current_request()`), the `Idempotency-Key` it runs under, its
OpenTelemetry span, its language and time zone. A task started with
`asyncio.create_task` copies them, so a task that outlives the request, or
serves many requests, would carry the first one's for its whole life: its
logs and spans would name that request, and a unit of work it committed
would mark that request's key.

`spawn` starts each task in a new, empty context instead
(`domainkit.tasks.clean_context()`). Only a frozen clock
(`domainkit.clock.freeze_time`) is carried, since it belongs to a test, not
to a request. The framework's own background work runs this way:

| Task | Name |
| --- | --- |
| A `get_or_set` (or `@cached`) computation shared by concurrent callers | `domainkit.cache.compute` |
| A round of readiness checks shared by concurrent probes | `domainkit.readiness` |
| Storing a keyed response, or releasing the key | `domainkit.idempotency.settle` |

A cached value is shared by every caller of its key, so its factory must
not depend on who asked: pass what it needs as arguments, and make them
part of the key. `@cache_page` keys entries by user and language, so it runs
the endpoint as the caller, in a copy of the caller's context, inside the
clean computation.

## Shutting down within a budget

When the server stops the application (its lifespan ends), or a management
command ends, the process shuts down in phases, all within
`SHUTDOWN_TIMEOUT` seconds (15 by default):

| Phase | What stops | Ends at |
| --- | --- | --- |
| `domains` | Each started domain's `DomainConfig.shutdown`, in reverse order (the application only) | 30% |
| `drain` | Closers registered to finish work in flight, such as a publisher sending the messages it holds | 50% |
| `tasks` | The loop's background tasks: cancelled, and waited for | 60% |
| `resources` | Caches, databases, ledgers, brokers, and kinds others register (an HTTP client), all at once | 90% |
| `flush` | Closers registered to run last, such as logs | 100% |

Each phase may run until its share of the budget has passed, counted from
the start of the shutdown. Time one phase does not use goes to the next,
and every phase keeps its own share whatever came before it. Inside a
phase:

- every closer is tried; one that fails is logged, and the others go on;
- the kinds of resources close at once, and so do the items of each kind,
  so one database that hangs neither delays the caches nor keeps the other
  databases open;
- a closer still running at the end of its phase is cancelled, named in
  the log, and left behind, even one that ignores cancellation;
- domain hooks run one after another, in reverse order. Each may use what
  is left of their phase, less a reserve kept for every hook still to run
  (a quarter of the phase, split among the hooks), so one slow hook may
  use most of the phase, while one that hangs is cut off with a
  `TimeoutError` naming the domain, and the others still stop.

Other parts of the framework, and projects, add closers to the `drain`,
`resources`, or `flush` phase:

```python
from domainkit import lifecycle
from domainkit.lifecycle import Phase

lifecycle.register("http clients", clients.close_all)  # resources
lifecycle.register("publishers", close_publishers, phase=Phase.DRAIN)
```

A closer closes what the running loop opened; registering a name again
replaces its closer.

The budget covers what happens after the server stops taking requests. The
server first waits for the requests in flight: tell it how long, and keep
the sum under your orchestrator's grace period (Kubernetes sends SIGKILL
30 seconds after SIGTERM by default):

```bash
uvicorn config.asgi:application --timeout-graceful-shutdown 10  # + SHUTDOWN_TIMEOUT 15 = 25 s
```

Without `--timeout-graceful-shutdown`, uvicorn waits for requests without a
limit, and a slow client can take the whole grace period. See
[ADR 0046](adr/0046-one-shutdown-budget-in-phases.md).

## Readiness

`/ready` runs the readiness checks of domains, databases, ledgers, and
caches (those with `READINESS`) at once, each within `READINESS_TIMEOUT`,
so a round takes about one timeout at worst, however many dependencies
hang. Probes that arrive during a round wait for it rather than starting
their own, and its result is reused for `READINESS_CACHE_SECONDS` from when
it ended. The round runs as a background task in a clean context: a probe
that gives up stops no one else, and shutdown cancels it.

## Event handlers

In-process event handlers run right after a unit of work commits, inside
the request. Each runs within `EVENT_HANDLER_TIMEOUT` seconds (10 by
default); one still running then is cancelled, logged, and reported to the
error reporters as `HandlerTimedOut` (a `TimeoutError`), and the next
handler runs. A handler may have its own limit:

```python
event_bus.subscribe(InvoicePaid, refresh_totals, timeout=2)
event_bus.subscribe(InvoicePaid, notify_crm, durable=True)  # In a worker.
```

A handler that must not be cut off, or must survive a crash, belongs in the
outbox: `durable=True` stores the call with the commit, and a worker runs it
and retries it ([contrib.md](contrib.md)). Durable handlers take no
`timeout`; the worker's task policies apply.

## Stores that stay bounded

Background work in the stores is bounded too:

- **Idempotency keys.** A keyed request that raises or is cancelled
  releases its key in a task of its own, within 5 seconds, so a retry need
  not wait out the lease. Without messaging, each claim's purge of expired
  keys skips those another claim is deleting ([contrib.md](contrib.md)).
- **Audit sealing.** A run looks for unsealed rows from a high-water mark,
  the newest sealed row, less `AUDIT.SEAL_LATE_WINDOW`, so its cost follows
  what is new, not the length of the trail; it seals at most
  `AUDIT.SEAL_BATCHES` batches and counts what it leaves in
  `audit_unsealed_rows` ([ADR 0047](adr/0047-seal-from-a-high-water-mark.md)).
- **Password hashing.** A call keeps its place in the pool until its hash is
  done, refusals ask clients to come back when the queue will have drained,
  and `AUTH.HASHING_RESERVED` places are kept for password changes
  ([auth.md](auth.md#passwords)).
- **The in-memory rate limiter** forgets idle callers at most 64 per
  request, so no one request pays for a crowd that left.

## Workers and the outbox

`runworker` and the publisher that sends committed messages to a broker
(`domainkit.contrib.messaging`, see [contrib.md](contrib.md)) stay
correct under outages, overload, and shutdown: committed messages are
never stranded, the process runs a bounded number of messages, every broker
call has a deadline, and cleanup never holds up claiming.

**One concurrency budget.** A worker process runs at most
`MESSAGING.CONCURRENCY` messages at once, whatever it serves. The polling
worker of each outbox, `runworker --once`, and the deliveries of every
queue a broker hands over draw from one budget. A message holds its slot
from its claim (or delivery) until its outcome is recorded. On RabbitMQ the
prefetch is each consumer's: quorum queues refuse a prefetch shared by a
channel, and RabbitMQ 4.3 quietly turns one into a prefetch per consumer.
So a worker of three queues may hold up to `3 × CONCURRENCY` deliveries;
at most `CONCURRENCY` of them run, and the others wait for a slot,
unacknowledged. Keep `TASK_TIMEOUT` times the number of queues well under
RabbitMQ's `consumer_timeout` (30 minutes by default), or give busy queues
workers of their own (`runworker --queue`).

```python
MESSAGING = {"BROKER": "default", "CONCURRENCY": 20}  # 20 at once, in all
```

**Deadlines on the broker.** Every RabbitMQ call has one:
`BROKERS[...].CONNECT_TIMEOUT` for connecting, opening channels,
declaring, binding, setting the prefetch, and starting or stopping
consumers, and `PUBLISH_TIMEOUT` for each confirmation. A broker that
accepts connections but stops answering fails the call instead of holding
the publisher, and everything queued behind it, until its heartbeat ends.

**Publishing on commit.** The background publisher sends committed
messages to the broker without ever holding up a commit:

- *Leases.* A message is locked for a short lease while it is published:
  the broker's `publish_timeout` (`CONNECT_TIMEOUT` plus
  `PUBLISH_TIMEOUT` on RabbitMQ) and five seconds. Once the broker
  confirms it, the lock lasts `REPUBLISH_AFTER`. Whatever the broker did
  not confirm, for any reason (a refusal, the deadline, a stop), is
  unlocked at once, in a clean-up that a cancellation cannot interrupt and
  that has five seconds of its own. If even that fails, the lease expires
  within seconds, and the relay sends the message.
- *A circuit breaker.* After `BREAKER_FAILURES` failed broker calls in a
  row (5), commits leave their messages to the relays for
  `BREAKER_COOLDOWN` seconds (30), touching neither the database nor the
  broker. Then one round probes, checking that the broker answers before
  it locks any row; a confirmed message closes the breaker. It logs once
  when it opens and once when it closes, and
  `messaging_publisher_breaker_open` shows it.
- *Bounded rounds.* A round locks at most `BATCH_SIZE` messages and has at
  most that many publications in flight; a backlog of thousands goes a
  batch at a time. Past 10,000 waiting messages, commits leave theirs to
  the relays (logged once until the backlog clears).
  `messaging_left_to_relay_total` counts the messages left to the relays,
  by reason.
- *An owned task.* The publisher's task is
  [supervised](#background-tasks-spawn-and-supervise): it runs in a clean
  context, never the
  committing request's, starts again after a crash, after a pause that
  doubles up to a minute, and ends when it has nothing to do, so an idle
  publisher holds nothing of its event loop.
- *Shutdown.* The shutdown's [drain phase](#shutting-down-within-a-budget)
  closes the publishers, in the application and in management commands
  alike: each gets
  `MESSAGING.CLOSE_TIMEOUT` (10 seconds, and at least the broker's
  `PUBLISH_TIMEOUT`) to finish, then stops, and the relays send the rest.
  The drain phase ends halfway through `SHUTDOWN_TIMEOUT`; raise
  `SHUTDOWN_TIMEOUT` if a publisher should get all of its `CLOSE_TIMEOUT`.
  Cut short, it costs a few seconds' delay, never a message.

**Brokered shutdown.** A stopping worker stops consuming within five
seconds, even from a broker that is lost or stalled, and logs it if it
could not. Running deliveries still get `SHUTDOWN_TIMEOUT`, counted from
the stop; a delivery that arrives meanwhile is given back to the broker,
not run. Relays, cleanup, and the listener of a stopping worker finish the
step they are on, for up to five seconds, so a round under way is
published rather than given back. A second signal stops everything at
once, as before.

**Cleanup.** Each polling and relaying worker deletes finished messages
older than `RETENTION_SECONDS` in a task of its own, every
`CLEANUP_INTERVAL`, starting a random 30 to 60 seconds after it starts.
It deletes `CLEANUP_BATCH_SIZE` rows per statement (1,000), each batch in
its own short transaction, skipping rows other workers lock on PostgreSQL.
A batch that fails, for example past `STATEMENT_TIMEOUT`, halves the next
one, down to a single row, and full batches double back: a slow table
slows cleanup down but never stops it, and claiming never waits for it.

**The database's clock.** On PostgreSQL, the outbox's server decides what
is due and when locks expire (`statement_timestamp()`), so a worker on a
host whose clock runs ahead never takes over another's message early, one
whose clock runs behind never leaves due work waiting, and a message due
at once is due at once whatever the clock of the host that wrote it.
SQLite lives in the worker's process, so its clock is the host's. Under
`domainkit.clock.freeze_time` the frozen clock decides on every database,
so tests can move time. Delays (`delay=`, schedules) still count from the
clock of the host that writes them.

**Waking on commit.** On PostgreSQL (with asyncpg), a transaction that
writes messages due at once also sends `NOTIFY domainkit_messages` with
each queue's name, and polling and relaying workers `LISTEN` on one
connection of their pool. An idle worker then starts a committed message
right away instead of on its next poll, so `POLL_INTERVAL` can be longer.
Polling stays: a notification is only a hint, sent when the transaction
commits and lost harmlessly when nobody listens, and a worker whose
listening connection drops polls until it listens again. With a broker that
publishes on commit, the broker wakes the workers and nothing is sent. Set
`MESSAGING.NOTIFY` to `False` to turn it off.

**Gauges.** `messaging_outbox_messages` and `messaging_outbox_lag_seconds`
count the outbox at most once per `METRICS_TTL` seconds (15) in each
process, and scrapes that arrive during a count share it.

The outbox is not partitioned: see
[ADR 0043](adr/0043-the-outbox-database-decides-when-work-is-due.md).
