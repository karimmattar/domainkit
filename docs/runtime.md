# How domainkit runs async work

domainkit serves requests on one event loop per process. Work that is not a
request (computing a cached value, checking readiness, settling an
`Idempotency-Key`) runs as background tasks of that loop. This page covers
how those tasks start, who owns them, how the process stops them, and how
long anything may wait while it does. Then it covers what keeps the loop
free: which loop runs, how logs and error reports leave it, how blocking
code is found before production, and which servers and Python builds
domainkit runs on; how workers and the outbox stay correct under outages;
and how a busy service stays responsive: request deadlines, load shedding,
database timeouts, and outgoing calls. Settings are in
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
| `flush` | Closers registered to run last, then error reports, then logs | 100% |

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
  `TimeoutError` naming the domain, and the others still stop. Each hook
  runs as a task: one cut off is cancelled and left behind, like a closer,
  so a hook that ignores cancellation (a flush behind `asyncio.shield`, a
  retry loop that catches everything) holds up neither the hooks after it
  nor the later phases.

Other parts of the framework, and projects, add closers to the `drain`,
`resources`, or `flush` phase:

```python
from domainkit import lifecycle
from domainkit.lifecycle import Phase

lifecycle.register("search index", index.close)  # resources
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

## Event loops: uvloop

`runserver` and every management command with an `async` handle
(`runworker`, `migrate`, `seed`, ...) run on [uvloop](https://github.com/MagicStack/uvloop)
when it is installed: `pip install 'domainkit[uvloop]'` (uvicorn's
`standard` extra brings it too, on Linux and macOS). `EVENT_LOOP` chooses:

| `EVENT_LOOP` | Loop |
| --- | --- |
| `"auto"` (default) | uvloop when it can be imported, asyncio's own otherwise |
| `"asyncio"` | always asyncio's own: rule uvloop out when chasing a bug |
| `"uvloop"` | uvloop, or a configuration error when it is missing |

`runserver` follows it on uvicorn and on Granian. Production servers pick
their own loop: uvicorn and Granian take `--loop uvloop` (both default to
`auto`, and Granian's `auto` takes rloop first when it is installed). Code
that starts a loop of its own can follow the setting:

```python
import asyncio

from domainkit.conf import settings
from domainkit.utils.loops import loop_factory

asyncio.run(main(), loop_factory=loop_factory(settings.EVENT_LOOP))
```

uvloop does not run on Windows; `auto` falls back to asyncio's loop there,
and the extra installs nothing.

## Logging and error reports

Writing a log line can block: stdout is a pipe a log collector may stop
reading, and a file or socket can stall. A handler called on the event loop
would then stop every request. `build_logging_config` writes through a
`domainkit.log.BackgroundHandler` instead, and the project settings `init`
writes use it:

```python
from domainkit.log import build_logging_config

LOGGING = build_logging_config(log_format="json", level="INFO")
```

- The caller, the event loop included, merges the message, adds the
  request id and trace ids (they live in its context), and puts the record
  on a queue of 10,000 (`queue_size`). One thread, `domainkit-logging`,
  formats and writes it; exceptions are formatted there too.
- A full queue never makes the caller wait. The record is dropped and
  counted (`handler.dropped`), and the next record that fits with it is
  preceded by a warning from `domainkit.log` that counts them by level:
  `Dropped 120 log records (118 INFO, 2 WARNING): the logging queue was
  full`.
- Records below `WARNING` fill at most nine tenths of the queue (9,000 of
  10,000); the last tenth is kept for warnings and errors
  (`WARNING_RESERVE`), so a flood of access lines while the stream is
  stalled cannot crowd out a security event or an error. A queue of fewer
  than 10 places keeps none.
- At shutdown the lifespan and management commands wait for the queue to
  drain in the `flush` phase, last of all, after error reports; Python's own
  logging shutdown does the same at exit. A stream that stays blocked is
  given up on, so it cannot hold the process: at exit, the handler's
  `flush()` and `close()` share one `FLUSH_TIMEOUT` (5 seconds), and once
  the flush gives up the close does not wait again.
- `queue_size=None` writes on the caller's thread, as plain handlers do:
  for scripts that must see each line before going on.

Another `LOGGING` can use the handler around any handlers:

```python
LOGGING = {
    "version": 1,
    "handlers": {
        "file": {"class": "logging.FileHandler", "filename": "app.log"},
        "background": {
            "class": "domainkit.log.BackgroundHandler",
            "handlers": ["file"],
            "queue": {"()": "queue.Queue", "maxsize": 10000},
            "listener": "domainkit.log.BackgroundListener",
            "filters": ["context"],
        },
    },
    "filters": {"context": {"()": "domainkit.log.ContextFilter"}},
    "root": {"handlers": ["background"], "level": "INFO"},
}
```

Error reporters (`ERROR_REPORTERS`) run off the loop too, on one
`domainkit-reporting` thread: `report_error` queues the report and returns.
Each reporter runs in a copy of the caller's context, so Sentry still sees
the request's scope and log lines keep its request id. At most 100 reports
wait; more are dropped and counted, and the count is logged. Shutdown waits
for waiting reports, within 5 seconds, before the logs. In tests, call
`domainkit.reporting.flush_reports()` before checking what was reported
([testing.md](testing.md#fixtures)).

## Finding blocking code

A call that blocks inside an `async def` (a sync HTTP client, `time.sleep`,
`open()` on a slow disk, a CPU-heavy loop) stops every request on the
event loop while it runs. domainkit looks for it in four places, so it is
found before production:

- **While developing.** With `DEBUG` on, the application runs its loop in
  asyncio's debug mode, and every callback or task step that holds it for
  `SLOW_CALLBACK_DURATION` seconds (0.1) or more is logged by the `asyncio`
  logger: `Executing <Task ... coro=<list_orders() running at
  shop/api/routes.py:42>> took 0.250 seconds`. Debug mode also reports
  resources left open, and work handed to the loop from another thread
  without `call_soon_threadsafe`. `SLOW_CALLBACK_DURATION = None` turns it
  off; staging and production refuse `DEBUG`.
- **In tests.** `pytest --detect-blocking` runs every loop the tests start
  in debug mode, and fails a test whose loop was held that long in one
  step; `--blocking-seconds 0.5` raises the bar (0.1 is the least asyncio
  reports). Fixtures are not timed. Turn it on for a run, or always:

  ```toml
  [tool.pytest.ini_options]
  addopts = "-p domainkit.testing --detect-blocking"
  ```

  Debug mode reports resources left open as `ResourceWarning`, so with
  `filterwarnings = error` it can find unclosed connections too.
- **In `check`.** It warns about the route handlers and dependencies of the
  installed domains written as `def`: FastAPI calls each on its thread pool
  (40 threads by default), a hop per call, and a busy service queues on it.
  Make them `async def`. One that must stay sync (it calls a library with
  no async API) can say so, and `check` stops warning about it:

  ```python
  from domainkit.http.blocking import allow_sync


  @allow_sync
  def render_invoice(invoice_id: UUID) -> bytes: ...
  ```

  Classes used as dependencies are left alone: FastAPI builds every class
  on its thread pool, whatever it does. The dependencies domainkit itself
  provides are `async`: `CurrentUser` (`acurrent_user`; `current_user` stays
  a plain function to call with a request), and a `ResourceRouter`'s unit of
  work and `FilterSet`, which it builds on the loop.
- **While writing.** Projects made by `init` turn on Ruff's `ASYNC` rules,
  which catch `time.sleep`, `open()`, `subprocess`, and sync HTTP clients
  inside `async def`.

## TLS contexts

Building a TLS context reads the system's CA store and the certificate and
key files: milliseconds of blocking work. domainkit builds each one once
per process, for each set of TLS settings, and never on the event loop
while it serves:

| Connection | When the context is built |
| --- | --- |
| Databases (`SSLMODE`, `SSLROOTCERT`, `SSLCERT`, `SSLKEY`) | by `get_asgi_application`, before the server's loop serves; by a command, at its first connection |
| RabbitMQ (`TLS_CA`, `TLS_CERT`, `TLS_KEY`) | at the first connection, on a worker thread |
| Redis and Valkey (`rediss://`, `TLS_CA`, `TLS_CERT`, `TLS_KEY`) | at the first connection, on a worker thread, then shared by every connection of every pool with those settings (servers, Sentinel primaries, and Cluster nodes) |
| immudb (`TLS_CA`) | on the ledger's own thread |

A process keeps its contexts until it ends: restart it to use renewed
certificate files.

## Free-threaded Python

domainkit runs on free-threaded CPython 3.14 (`python3.14t`, without the
GIL). Importing domainkit and its runtime dependencies keeps the GIL off;
the `immudb` extra is the exception, since grpcio publishes no
free-threaded build yet. CI runs the tests of the state below, and of the
framework's own threads, on 3.14t with the GIL off throughout. The whole
suite does not run there yet: on 3.14.6t it crashes the interpreter now
and then (a segmentation fault inside the allocator, at a different test
each time), as it did before any of this was added, so that waits for a
fix in the interpreter or an extension.

Most state lives in one event loop and needs no lock. What event loops on
several threads can share (a server with a loop per thread, or threads that
start their own) is guarded by a lock, held only for plain Python work,
never across an `await`:

- the in-memory rate limiter (`MemoryBackend`) and its callers;
- `AuthenticationMiddleware`'s refused-credential holds;
- the password hashing pool's count of pending calls;
- the logging and reporting queues, and memory caches (`cache/memory.py`);
- the registries that keep each loop's background tasks
  (`domainkit.tasks`), commit publishers, and brokers, which a new loop
  walks to let go of the closed ones;
- the open live connections counted against `LIVE_MAX_CONNECTIONS` and
  `LIVE_MAX_CONNECTIONS_PER_CALLER`, and the making of them on first use;
- the event bus's subscriptions, so a handler subscribed from several
  threads at once (live events, as clients connect) is subscribed once.

A project's own module-level state needs the same care: a dict two threads
change at once needs a `threading.Lock` on free-threaded Python, as it would
with threads anywhere.

## Servers

domainkit is an ASGI application, served by
[uvicorn](https://www.uvicorn.org/) (a dependency) or
[Granian](https://github.com/emmett-framework/granian) (the `granian`
extra). `manage.py runserver --server granian` develops on Granian;
[reference.md](reference.md#server-limits) gives a production command and
the limits for each.

- **uvicorn** is Python on uvloop and httptools. It answers 503 past
  `--limit-concurrency`, limits WebSocket messages (`--ws-max-size`), and
  closes connections left open past `--timeout-graceful-shutdown`.
- **Granian** is an HTTP server in Rust that runs the same ASGI
  application, lifespan included (`--interface asgi`). It parses HTTP/1
  and HTTP/2 outside Python, bounds requests in flight per worker
  (`--backpressure`), times out slow headers, and restarts workers that
  die (`--respawn-failed-workers`). It has no WebSocket message limit: turn
  WebSockets off (`--no-ws`) when the API has none.

Both run one event loop per worker process; run one worker per core. The
test suite serves the application with Granian in a subprocess, so a
change that breaks it fails CI.

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
  alike, within the time that phase always has: a fifth of
  `SHUTDOWN_TIMEOUT` (3 seconds of the default 15), since it ends at half
  and begins by 30% at the latest. The flush ends `RELEASE_TIMEOUT` (five
  seconds), or half that time if less, before then, so a publication it
  cuts short is unlocked within the phase; the relays send the rest. Cut
  short, it costs a few seconds' delay, never a message.

**Brokered shutdown.** A stopping worker stops consuming within five
seconds, even from a broker that is lost or stalled, and logs it if it
could not. Running deliveries still get `MESSAGING.SHUTDOWN_TIMEOUT` (20
seconds), counted from the stop; a delivery that arrives meanwhile is given
back to the broker, not run. Relays, cleanup, and the listener of a
stopping worker finish the step they are on, for up to five seconds, so a
round under way is published rather than given back. A second signal stops
everything at once, as before.

A worker's shutdown is longer than a web process's, on purpose: its
messages get their time first, then the process shuts down within
`SHUTDOWN_TIMEOUT` like any other. With the defaults that is up to
20 + 5 + 15 = 40 seconds, so give worker pods a longer grace period than
Kubernetes' 30-second default (`terminationGracePeriodSeconds: 45`), or
lower `MESSAGING.SHUTDOWN_TIMEOUT`.

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
listening connection drops polls until it listens again. Only rows the
transaction wrote count (the inserts return them): a row skipped for its
dedupe key, such as a periodic slot that every worker queues and one
insert wins, wakes no one. With a broker that publishes on commit, the
broker wakes the workers and nothing is sent. Set `MESSAGING.NOTIFY` to
`False` to turn it off.

**Gauges.** `messaging_outbox_messages` and `messaging_outbox_lag_seconds`
count the outbox at most once per `METRICS_TTL` seconds (15) in each
process, and scrapes that arrive during a count share it.

The outbox is not partitioned: see
[ADR 0043](adr/0043-the-outbox-database-decides-when-work-is-due.md).

## Request deadlines

Every HTTP request gets a deadline when it arrives: `REQUEST_TIMEOUT`
seconds (30 unless set), started by `DeadlineMiddleware` before any other
work. It covers everything until the response starts: authentication,
reading the body, the handler, and the database and outgoing calls it
waits on. When it passes, that work is cancelled, and the request is
answered:

```http
HTTP/1.1 503 Service Unavailable
Retry-After: 1

{"detail": "The request took too long; try again shortly.",
 "code": "deadline_exceeded", "request_id": "6f1c7e6a..."}
```

and one warning is logged: `POST /invoices ran past its deadline of 30s`.
The cancellation unwinds the request as any other error does: an open
unit of work rolls back and gives its connection back, and an
`Idempotency-Key` that committed nothing is released, so the client can
try again with the same key.

A route that needs longer, or less, says so; the time counts from when the
request arrived, and `None` removes the deadline:

```python
from fastapi import Depends
from domainkit.http.deadlines import RequestTimeout

@router.post(
    "/exports", dependencies=[Depends(Require(IsAdminUser)), Depends(RequestTimeout(300))]
)
async def export(...): ...
```

The body is read before route dependencies run, so it is read under
`REQUEST_TIMEOUT`.

**What has no deadline.** The deadline is lifted as soon as the response
starts: a streamed response (an export, Server-Sent Events) runs as long
as it streams, and background tasks that run after the response are not
cut off. That covers every long-lived response, whether or not it marks
its scope `domainkit.long_lived`, as an event stream does. WebSocket
connections never get one; their limits are their own.
A long-lived endpoint that does slow work before it starts answering uses
`RequestTimeout(None)`, or a longer deadline. Code can also lift the
deadline of the work it is doing with
`domainkit.deadlines.current_deadline().clear()`. Tasks a request starts
share its deadline while the request runs, and are free of it once the
request ends.

### Keeping to the deadline

`domainkit.deadlines` reads and applies the deadline of the work being
done, in a request or anywhere else:

```python
from domainkit import deadlines

deadlines.remaining()  # Seconds left, or None without a deadline.
deadlines.remaining(5.0)  # The smaller of 5 and the seconds left.

rate = await deadlines.within(fetch_rate(), 2.0)  # Bound one await.

async with deadlines.timeout(10.0):  # Bound a block.
    await rebuild_report()
```

`within` and `timeout` bound their work by the smaller of their own
seconds and the time left, and make that the deadline inside, so what they
call sees the tighter one. Their own seconds running out raises
`TimeoutError`, which the code may catch and work around. The deadline
running out cancels the work up to the block that set it, which raises:
the request answers 503, and a `timeout` block raises `DeadlineExceeded`
(a `TimeoutError` too). A block entered with no time left raises
`DeadlineExceeded` at once. Workers and commands bound their own work the
same way: `async with deadlines.timeout(60): await handle(message)`.

`remaining()` reads `domainkit.clock`, so a frozen clock stops it; the
timers that cancel work run on the event loop's own clock.

### What keeps to it

- **The database.** On PostgreSQL, a transaction begun with less time
  left than `STATEMENT_TIMEOUT` (by a second or more, `DEADLINE_SLACK`)
  sets `statement_timeout` to the time left, with `SET LOCAL`, so the
  server stops the work too. SQLite has no such setting; the cancellation
  stops the wait. A transaction begun after the deadline passed (cleanup)
  keeps `STATEMENT_TIMEOUT`. The statement is not counted by
  `assert_num_queries`.
- **Outgoing HTTP calls** through `domainkit.http.client` are bounded by it
  (below).
- **Ledgers and caches** keep their own `TIMEOUT`: a request's call to
  either is cancelled at the deadline like anything else it awaits, and
  the request answers 503. Code of your own can bound one await by both,
  and refuse at once when no time is left:
  `await deadlines.within(call(), 5)`.

Choose `REQUEST_TIMEOUT` below the load balancer's idle timeout (60 seconds
on most), so a slow request gets the application's 503 rather than the
balancer's 504; and `STATEMENT_TIMEOUT` no longer than `REQUEST_TIMEOUT`, so
transactions rarely need to set their own. ADR 0048 explains the choices.

## Load shedding

`ConcurrencyLimitMiddleware` lets each event loop work on at most
`MAX_CONCURRENT_REQUESTS` HTTP requests at once (1000 unless set; `None`
turns it off). A process serves on one loop, so that is the cap per
process; a server running several loops in one process (free-threaded
Python, a loop per thread) admits the cap on each. A request past the cap waits for a slot, first come
first served, for up to `REQUEST_QUEUE_TIMEOUT` seconds (1), behind at most
`MAX_QUEUED_REQUESTS` others (100). When the queue is full, or the wait
runs out, it is answered at once:

```http
HTTP/1.1 503 Service Unavailable
Retry-After: 1

{"detail": "The server is busy; try again shortly.", "code": "overloaded", ...}
```

- The health and readiness probes and the metrics scrape
  (`CONCURRENCY_EXEMPT_PATHS`) are never counted or refused, so a busy
  replica is not taken for a dead one; behind `--root-path`, the paths are
  compared without it.
- A request holds its slot until its response starts, or until it ends:
  a streamed response, an event stream among them, gives its slot back as
  it starts, and WebSocket connections never take one. A request that is
  cancelled, while it waits or while it runs (its deadline passed, or the
  server cancelled it when the client went away), gives its slot or its
  place back.
- Time spent waiting on the client is not counted. While the application
  waits for a chunk of the request's body, its slot is lent to the next
  request; when the chunk arrives, the request takes a slot back through
  the queue, as a new request would, and is answered 503 `overloaded` if
  none frees in time. So clients that send their headers and then their
  body slowly, or never, hold no slot. `RequestBodyLimitMiddleware` answers
  them 408 `request_timeout` once the body has taken
  `REQUEST_BODY_TIMEOUT` seconds (10) in all, and closes the connection.
  A buffering proxy in front (nginx's `proxy_request_buffering on`) still
  reads slow bodies before the application sees them
  ([reference.md](reference.md#server-limits)).
- Shedding is logged at most every 10 seconds, with a count. With
  `domainkit[metrics]`, `http_requests_admitted` and `http_requests_queued`
  show the slots and the queue, and `http_requests_shed_total` counts
  refusals by `reason` (`queue_full`, `queue_timeout`).
- List it right after `DeadlineMiddleware` (the wait counts against the
  deadline) and before `AuthenticationMiddleware`, so a refused request
  costs no database lookup. Both are in `DEFAULT_MIDDLEWARE` and in the
  settings `init` writes; `check` warns in staging and production when
  either is missing.

Set the cap from what the process can finish: with a database pool of
`POOL_SIZE` plus `MAX_OVERFLOW` connections, requests past a few times that
only queue for connections. Keep uvicorn's `--limit-concurrency` above
the cap plus the queue, as a backstop ([reference.md](reference.md#server-limits)).
ADR 0049 explains the choices.

## Database timeouts and PgBouncer

PostgreSQL connections get three kinds of bounds:

- **Server-side:** `STATEMENT_TIMEOUT`, `LOCK_TIMEOUT`, and
  `IDLE_IN_TRANSACTION_TIMEOUT`, set for the session when it connects
  (migrations are exempt), and `statement_timeout` lowered per
  transaction to a request's deadline.
- **Client-side:** asyncpg waits at most `CONNECT_TIMEOUT` (10 seconds) to
  connect, instead of its own 60, which `POOL_TIMEOUT` does not cover; and
  at most `COMMAND_TIMEOUT` for one statement (`STATEMENT_TIMEOUT` plus 5
  seconds unless set), so a network or a server that stops answering
  cannot hold a request. The margin lets the server's timeout end a slow
  statement first, with an error that keeps the connection. psycopg gets
  `connect_timeout`.
- **The pool:** `POOL_TIMEOUT` (10 seconds for network databases).

**Behind PgBouncer** in transaction pooling, a client's statements may run
on a different server connection in each transaction, so nothing can be
kept in the session. Set `PGBOUNCER = True`:

```python
DATABASES = {
    "default": {
        "URL": env.str("DATABASE_URL"),  # postgresql+asyncpg://app@pgbouncer:6432/app
        "MIGRATION_URL": env.str("DATABASE_OWNER_URL"),  # PostgreSQL itself.
        "SSLMODE": "verify-full",
        "PGBOUNCER": True,
    }
}
```

Then asyncpg keeps no statement cache (`statement_cache_size=0`, and
SQLAlchemy's `prepared_statement_cache_size=0`), each prepared statement
gets a unique name, so two clients never collide on one server
connection, and the timeouts are not sent at connect time (PgBouncer
refuses unknown startup parameters) but set at the start of each
transaction with `set_config(..., true)`, which is `SET LOCAL`. psycopg
gets `prepare_threshold=None`. This is the setup SQLAlchemy documents for
asyncpg behind PgBouncer; each transaction pays one more round trip for
its timeouts. domainkit itself takes only transaction-scoped advisory
locks, which transaction pooling allows. `runworker` does not listen for
new messages through it (`LISTEN` needs a session of its own): its
workers poll every `POLL_INTERVAL`, and `NOTIFY` from commits is simply
not heard. Point `MIGRATION_URL` at PostgreSQL directly: migrations may
take longer, and need the schema owner.

`check` warns in staging and production about a database that seems to
go through PgBouncer without the mode: one on port 6432, or one whose
`CONNECT_ARGS` turn the statement cache off by hand. `PGBOUNCER` on
anything but PostgreSQL is refused.

## Outgoing HTTP calls

`domainkit.http.client` (the `httpclient` extra, httpx2) calls other
services from a request, a task, or a command, within the deadline of the
work it serves:

```python
from domainkit.http.client import HttpClient

rates = HttpClient(base_url="https://rates.example.com/v1", timeout=3.0)


async def euro_rate() -> Decimal:
    response = await rates.get("/rates/EUR")
    response.raise_for_status()
    return Decimal(response.json()["rate"])
```

Make one client per service, at module level. Each keeps one pool of
connections per process (per event loop), opened by its first call and
closed with the other resources when the application or a command ends;
`domainkit.http.client.client` serves one-off calls. A relative URL is
appended to `base_url`, and may not climb out of its path: a `.` or `..`
segment, spelled out or percent-encoded (`%2e%2e`, `..%2f`), raises
`UnsafeURL`. A URL naming a host must keep to its origin. So a path built
from someone's input cannot send the call elsewhere.

A client keeps one circuit per host for all its callers (below), so make
one client per trust domain: a client that calls URLs your users chose
should not share its circuits with your own services', nor one user's
failing URLs refuse another's calls. Give such a client `breaker=False`:

```python
from domainkit.http.client import HttpClient

callbacks = HttpClient(guard=True, breaker=False, retries=0)
```

Every call:

- is bounded by its `timeout` (`HTTP_CLIENT.TIMEOUT`, 10 seconds), retries
  included, and by the deadline. Past `timeout` it raises `CallTimeout`;
  past the deadline the work is cut off as above. httpx2's own timeouts
  get the time left, and connecting at most `HTTP_CLIENT.CONNECT_TIMEOUT`.
- is retried up to `retries` times (`HTTP_CLIENT.RETRIES`, 2) after a
  network error, a timeout, or a 429, 502, 503, or 504 answer, when its
  method is idempotent (GET, HEAD, OPTIONS, PUT, DELETE, TRACE) or it
  passes `retry=True`. A call that could not connect is retried whatever
  its method, since nothing was sent; `retry=False` retries nothing. The
  waits are jittered ("full jitter": random, below `BACKOFF` doubled per
  retry, at most `MAX_BACKOFF`), and a `Retry-After` is honoured up to
  `MAX_BACKOFF`. No retry starts that could not finish in time; out of
  retries, the last answer is returned, or the last error raised.
- fails at once with `CircuitOpen` while its host's circuit is open:
  after `BREAKER_FAILURES` failures in a row (network errors, timeouts, 5xx
  answers) for `BREAKER_COOLDOWN` seconds, after which one call at a time
  tries the host again. A call cut off by the work's deadline is not held
  against the host. Opening is logged once. A client made with
  `breaker=False` keeps no circuits.
- sends the request id (`X-Request-ID`) and the trace context
  (`traceparent`, `tracestate`) of the work it serves.
- reads at most `MAX_RESPONSE_BYTES` of the answer (10 MiB, counted after
  decompression), or raises `ResponseTooLarge`, and follows no redirects.
- is logged by method, origin, and path: never a query string, a header,
  or a body. Keep secrets out of paths, or use a guarded client, whose
  logs show only the origin.

**URLs someone else chose** (a callback, a link to fetch) go through a
client made with `guard=True`. It calls only public addresses, with the
checks `domainkit.contrib.webhooks` uses (`domainkit.http.guard`): https
only (`allow_http=True` adds http), no credentials in the URL, not
`localhost` or a cloud metadata name, and every address the host resolves
to public, unless in `allowed_networks`; metadata addresses never are. The
host is resolved, and every address checked, as each connection opens,
and the connection goes to an address that was checked, so DNS rebinding
gains nothing; TLS still checks the certificate against the host name.
A guarded client ignores proxy settings from the environment, and the CAs
it names (`SSL_CERT_FILE`, `SSL_CERT_DIR`): it trusts `CA_BUNDLE`, or the
system's CAs. It asks for uncompressed answers, and sends no request id or
trace context unless `propagate=True`. A refused URL raises `UnsafeURL`, a
422 `unsafe_url` when the request does not catch it. Host names are looked
up on threads of the guard's own, never the event loop's default
executor, which `asyncio.to_thread` and new database connections need: at
most 4 lookups at once, 2 of one host, and a host whose lookup failed
fails at once for 5 seconds. A lookup past those bounds fails at once, as
a connection error, so a host whose DNS never answers costs the other
calls nothing. A guarded client takes no `transport=`, which would skip
its address checks; tests use `stub`.

| Raised | When |
| --- | --- |
| `CallTimeout` (an `httpx2.TimeoutException`) | `timeout` passed, retries included |
| `DeadlineExceeded` | no time was left when the call began |
| `CircuitOpen` (an `httpx2.TransportError`) | the host's circuit is open |
| `ResponseTooLarge` (an `httpx2.RequestError`) | the answer was over `MAX_RESPONSE_BYTES` |
| `UnsafeURL` | a guarded client may not call the URL, or it leaves `base_url` |
| `httpx2.HTTPError` | the call failed on its last try |

**In tests**, `stub` sends every call made in its block, from any client,
to a handler, through httpx2's `MockTransport`:

```python
import httpx2
from domainkit.http.client import stub


def rates_api(request: httpx2.Request) -> httpx2.Response:
    assert request.url.path == "/v1/rates/EUR"
    return httpx2.Response(200, json={"rate": "1.08"})


async def test_euro_rate() -> None:
    with stub(rates_api):
        assert await euro_rate() == Decimal("1.08")
```

The handler may be async, or a transport of its own. A guarded client's
URL checks still apply; the address checks need the network, so a stub
skips them. A client made with `transport=` always uses that transport.
ADR 0050 explains the choices.
