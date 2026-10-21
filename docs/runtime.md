# How domainkit runs async work

domainkit serves requests on one event loop per process. Work that is not a
request (computing a cached value, checking readiness, settling an
`Idempotency-Key`) runs as background tasks of that loop. This page covers
how those tasks start, who owns them, how the process stops them, and how
long anything may wait while it does. Then it covers what keeps the loop
free: which loop runs, how logs and error reports leave it, how blocking
code is found before production, and which servers and Python builds
domainkit runs on. Settings are in [reference.md](reference.md#core).

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
  counted (`handler.dropped`), and the next record that fits is preceded by
  a warning from `domainkit.log`: `Dropped 120 log records: the logging
  queue was full`.
- At shutdown the lifespan and management commands wait for the queue to
  drain in the `flush` phase, last of all, after error reports; Python's own
  logging shutdown does the same at exit. A stream that stays blocked is
  given up on, so it cannot hold the process.
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
- the logging and reporting queues, and memory caches (`cache/memory.py`).

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
