# Runtime

A domainkit service runs as a few processes. Each process serves its
requests on one event loop, and does its other work as background tasks
of that loop. This page explains how a process starts, how it keeps the
loop free, how long each piece of work may take, and how the process
stops within a fixed time. Read it before you run a service under real
load, or when you write code that runs outside a request.

On this page you learn how to:

- build the application with `get_asgi_application`, and know what its
  lifespan starts and stops;
- run your own background work with `spawn` and `supervise`, and close
  your own resources at shutdown;
- keep the shutdown inside your orchestrator's grace period;
- choose the event loop, find blocking code, and run on free-threaded
  Python;
- write logs and error reports without holding the event loop;
- bound work with deadlines, shed load, and set database timeouts;
- call other services with `HttpClient`, safely and within the deadline.

Settings are listed on [reference/settings.md](reference/settings.md#core)
and [reference/http.md](reference/http.md#http). The words *use case*,
*unit of work*, *domain event*, *handler*, *outbox*, and *worker* are
explained in [the glossary](learning.md#words).

## A first example

This program starts a background task, bounds two pieces of work with
deadlines, then shuts down as a server would. It needs no settings and no
database:

```python
import asyncio

from domainkit import deadlines, lifecycle, tasks


async def slow_report() -> str:
    await asyncio.sleep(2)
    return "done"


async def main() -> None:
    warm_up = tasks.spawn(asyncio.sleep(60), name="shop.warm_up")
    print(warm_up.get_name(), "is owned:", warm_up in tasks.owned())

    async with deadlines.timeout(0.5):
        print(f"{deadlines.remaining():.1f} seconds left")
        try:
            await deadlines.within(slow_report(), 0.1)
        except TimeoutError:
            print("The report took more than 0.1 seconds.")

    await lifecycle.close_resources()
    print("warm_up cancelled:", warm_up.cancelled())


asyncio.run(main())
```

It prints:

```text
shop.warm_up is owned: True
0.5 seconds left
The report took more than 0.1 seconds.
warm_up cancelled: True
```

- `tasks.spawn` starts the task and keeps it. The process now *owns* the
  task, so the shutdown waits for it.
- `deadlines.timeout(0.5)` gives the block 0.5 seconds. Code inside the
  block reads the time left with `deadlines.remaining()`.
- `deadlines.within(..., 0.1)` bounds one `await`. Its own 0.1 seconds ran
  out first, so it raised `TimeoutError`, and the code went on.
- `lifecycle.close_resources()` is the shutdown. It cancelled the task
  that was still running, then closed caches, databases, and logs.

The rest of the page explains each of these, and the code the framework
runs the same way.

## The application

A service is an ASGI application. The project's `config/asgi.py` builds
it once, when the server imports the module:

```python
from domainkit.http import get_asgi_application

application = get_asgi_application()
```

The server (uvicorn or Granian) serves `config.asgi:application`.

### The application factory: `get_asgi_application`

`get_asgi_application()` builds the application from the settings, then
checks it. It does these steps, in order:

1. It calls `domainkit.setup()`. `setup()` applies `LOGGING`, loads
   `ERROR_REPORTERS`, gives the event bus `EVENT_HANDLER_TIMEOUT`, and
   loads `INSTALLED_DOMAINS`. A second call does nothing.
2. It checks each database's driver options (`CONNECT_ARGS`, the TLS
   files) now, not at the first connection.
3. It makes a `domainkit.http.application.Application`, a `FastAPI` with
   `PROJECT_NAME` as its title and `VERSION` as its version. FastAPI's
   own telemetry is off (see [Observability](reference/observability.md)).
   The API documentation is served only when `API_DOCS` allows it. Unset,
   it follows `DEBUG`: on in development, off when deployed.
4. It adds domainkit's error handlers and OpenAPI document, then includes
   `ROOT_ROUTER`. With `FLATTEN_ROUTERS` (the default), the routes of the
   included routers are served from the top, and indexed by their first
   path segment ([performance.md](performance.md#the-http-stack-does-each-piece-of-work-once)).
5. It wraps the routes in the middleware of `MIDDLEWARE`. The first name
   in the list is the outermost.
6. It checks the application: every route must name its access rule
   (`security.E101`), and operation ids must not clash (`openapi.E001`).
   Installed domains add their own checks. In staging and production, a
   problem stops the process with `CheckFailed`. Elsewhere, it is logged
   as a warning.
7. In staging and production, it logs a warning for server options that
   read larger WebSocket messages than `MAX_REQUEST_BODY_SIZE`, and for a
   unix socket that `TRUSTED_PROXIES` does not trust.

A route without a rule shows what step 6 does. In development, the
application starts and logs:

```text
The application has problems:
  security.E101: GET /reports declares no access rule: add Depends(Require(...)), or dependencies=[Public] if anyone may call it.
```

With `ENVIRONMENT = "production"`, the same problem stops the import:

```text
domainkit.checks.CheckFailed: The application has problems:
  security.E101: GET /reports declares no access rule: add Depends(Require(...)), or dependencies=[Public] if anyone may call it.
```

`Application` does one more thing on each HTTP and WebSocket request. It
marks the request as measured, so a FastAPI application you mount inside
it skips FastAPI's telemetry too. FastAPI's telemetry would record query
strings and exception messages, which domainkit keeps out of logs.

### The lifespan: startup and shutdown

When the server starts the application, the application's lifespan
runs. It does this:

1. With `DEBUG` on and `SLOW_CALLBACK_DURATION` set, it puts the loop in
   asyncio's debug mode ([Finding blocking code](#finding-blocking-code)).
2. It starts the installed domains, in `INSTALLED_DOMAINS` order. A
   domain starts by running the `on_startup` hooks it registered in its
   `ready()`. A domain that fails to start stops the domains started
   before it, and the process does not serve.
3. It checks the application again. Routes added after
   `get_asgi_application` returned (in `asgi.py`, or by a domain's
   startup) must name a rule too. Staging and production refuse to start
   with a new problem.
4. The server serves requests.
5. When the server stops, the lifespan
   [shuts down within a budget](#shutting-down-within-a-budget): the
   domains' `on_shutdown` hooks in reverse order, then work in flight,
   background tasks, connections, and logs.

`domainkit.lifecycle.serving()` is the part of the lifespan that starts
the domains and shuts everything down. A process that serves without
FastAPI's lifespan can use it directly:

```python
from domainkit import lifecycle


async def serve() -> None:
    async with lifecycle.serving():
        await run_until_stopped()
```

Management commands do not start domains. When a command ends, it calls
`lifecycle.close_resources()`, which runs every shutdown phase but the
domains'.

### Middleware order

Each request goes through the middleware in `MIDDLEWARE`, from the first
to the last, then to the route. The order matters: each middleware may
rely on the ones before it. For example, `DeadlineMiddleware` starts the
deadline before any other work, and `ConcurrencyLimitMiddleware` sheds a
request before `AuthenticationMiddleware` looks the caller up.

The project that `init` writes lists 11 middleware.
`DEFAULT_MIDDLEWARE`, which applies when a settings module sets no
`MIDDLEWARE`, lists 8: proxy headers, security headers, request context,
deadlines, the concurrency cap, allowed hosts, WebSocket limits, and the
body limit. [reference/http.md](reference/http.md#middleware) gives the
full order and why each one sits where it does. The application adds one
middleware of its own, innermost: it adds the headers of deprecated
routes.

### Shutting down within a budget

When the server stops the application (its lifespan ends), or a
management command ends, the process shuts down in phases. All phases
together take at most `SHUTDOWN_TIMEOUT` seconds (15 by default):

| Phase | What stops | Ends at |
| --- | --- | --- |
| `domains` | Each started domain's `on_shutdown` hooks, in reverse domain order (the application only) | 30% |
| `drain` | Closers registered to finish work in flight, such as a publisher sending the messages it holds | 50% |
| `tasks` | The loop's background tasks: cancelled, and waited for | 60% |
| `resources` | Caches, HTTP clients, databases, ledgers, brokers, and kinds others register, all at once | 90% |
| `flush` | Closers registered to run last, then the counts of held-back log lines, then error reports, then logs | 100% |

Each phase may run until its share of the budget has passed, counted
from the start of the shutdown. With the default 15 seconds, the
`domains` phase ends at 4.5 seconds and the `drain` phase at 7.5
seconds. Time one phase does not use goes to the next. Every phase keeps
its own share, whatever came before it. `lifecycle.PHASE_ENDS` holds
these shares, and `lifecycle.Budget` works out each phase's end:

```python
from domainkit.lifecycle import PHASE_ENDS, Phase

print({phase.value: PHASE_ENDS[phase] for phase in Phase})
```

```text
{'domains': 0.3, 'drain': 0.5, 'tasks': 0.6, 'resources': 0.9, 'flush': 1.0}
```

Inside a phase:

- Every closer is tried. One that fails is logged, and the others go on.
- The kinds of resources close at once, and so do the items of each kind.
  One database that hangs neither delays the caches nor keeps the other
  databases open.
- A closer still running at the end of its phase is cancelled, named in
  the log, and left behind, even one that ignores cancellation. It is an
  owned task from then on. The `tasks` phase waits for it to end, and
  names it as `domainkit.close:<name>` if it is still running when that
  phase ends. So it ends before the resources it may use close.
- Domain hooks run one after another, in reverse order. Each hook may use
  what is left of the phase, less a reserve for every hook still to run
  (a quarter of the phase, split among the hooks). So one slow hook may
  use most of the phase. A hook that hangs is cut off with a
  `TimeoutError` that names the domain, and the other hooks still run.
  Each hook runs as a task. A hook that is cut off is cancelled and left
  behind, like a closer. So a hook that ignores cancellation (a flush
  behind `asyncio.shield`, a retry loop that catches everything) holds up
  neither the hooks after it nor the later phases.

#### Closing your own resources

Other parts of the framework, and your project, add closers to the
`drain`, `resources`, or `flush` phase with `lifecycle.register`:

```python
from domainkit import lifecycle
from domainkit.lifecycle import Phase

lifecycle.register("search index", index.close)  # resources, by default
lifecycle.register("publishers", close_publishers, phase=Phase.DRAIN)
```

- A closer is an `async` function with no arguments. It closes what the
  running loop opened.
- Registering a name again replaces its closer. `lifecycle.unregister(name)`
  removes it. An unknown name is ignored.
- The framework's own kinds (`caches`, `HTTP clients`, `databases`,
  `ledgers`, `brokers`, `held-back log lines`, `error reports`, `logs`)
  are in the same table, so their names are taken. `register` raises
  `ValueError` for one of them, and for the `domains` and `tasks` phases.
- `lifecycle.closers(phase)` lists what a phase will close, in the steps
  it runs them. A registered closer runs in the first step, at the same
  time as the framework's kinds of its phase. The `flush` phase then
  writes held-back counts, error reports, and logs, one step each:

```python
from domainkit.lifecycle import Phase, closers

print([list(step) for step in closers(Phase.RESOURCES)])
print([list(step) for step in closers(Phase.FLUSH)])
```

```text
[['caches', 'HTTP clients', 'databases', 'ledgers', 'brokers']]
[['held-back log lines'], ['error reports'], ['logs']]
```

`lifecycle.close_resources(budget=None)` runs the `drain`, `tasks`,
`resources`, and `flush` phases. It never raises: every failure is
logged, and every closer is tried. Pass a `Budget` to share one budget
with work done before; by default, a new budget begins when it is called.

#### The server's grace period

The budget covers what happens after the server stops taking requests.
The server first waits for the requests in flight. Tell it how long, and
keep the sum below your orchestrator's grace period (Kubernetes sends
`SIGKILL` 30 seconds after `SIGTERM` by default):

```bash
uvicorn config.asgi:application --timeout-graceful-shutdown 10  # + SHUTDOWN_TIMEOUT 15 = 25 s
```

Without `--timeout-graceful-shutdown`, uvicorn waits for requests without
a limit, and a slow client can take the whole grace period. See
[ADR 0046](adr/0046-one-shutdown-budget-in-phases.md).

### Readiness

`/ready` runs the readiness checks of domains, databases, ledgers, and
caches (those with `READINESS`) at the same time, each within
`READINESS_TIMEOUT` seconds (5). So a round of checks takes about one
timeout at worst, however many dependencies hang.

- Probes that arrive during a round wait for it, rather than starting
  their own round.
- The result is reused for `READINESS_CACHE_SECONDS` (1) from when the
  round ended. A flood of probes costs at most one round per interval.
- The round runs as a background task in a [clean context](#clean-contexts).
  A probe that gives up stops no one else, and the shutdown cancels the
  round.
- A group of checks that raises, rather than reporting what failed,
  fails under its group's name (`domains`, `databases`, `ledgers`,
  `caches`). It is logged with its traceback and kept as the round's
  result. Probes then get a 503 for `READINESS_CACHE_SECONDS`, not a 500
  and a new round each.

The answer is `{"status": "ready"}`, or a 503 with
`{"status": "unavailable"}`. With `READINESS_DETAILS = True`, a 503 also
lists the failing checks under `failing`. Leave it off where anyone can
reach `/ready`. [operations.md](operations.md#health-checks) explains how
to use the probes.

### Servers

domainkit is an ASGI application, served by
[uvicorn](https://www.uvicorn.org/) (a dependency) or
[Granian](https://github.com/emmett-framework/granian) (the `granian`
extra). `manage.py runserver --server granian` runs Granian while you
develop. [reference/servers.md](reference/servers.md) gives a production
command and the limits for each.

- **uvicorn** is Python on uvloop and httptools. It answers 503 past
  `--limit-concurrency`, limits WebSocket messages (`--ws-max-size`), and
  closes connections left open past `--timeout-graceful-shutdown`. Run it
  with `--no-proxy-headers`. By default it reads `X-Forwarded-For` and
  `X-Forwarded-Proto` itself, from `FORWARDED_ALLOW_IPS` (`127.0.0.1`),
  before domainkit does. Then neither `TRUSTED_PROXIES` nor
  `PROXY_SCHEME_HEADER` decides the client's address and scheme. With
  `PROXY_SCHEME_HEADER = None`, a client's own `X-Forwarded-Proto`,
  passed through a proxy on the same host, would still set the scheme.
- **Granian** is an HTTP server written in Rust. It runs the same ASGI
  application, lifespan included (`--interface asgi`). It parses HTTP/1
  and HTTP/2 outside Python, bounds the requests in flight per worker
  (`--backpressure`), times out slow headers, and restarts workers that
  die (`--respawn-failed-workers`). It has no WebSocket message limit:
  turn WebSockets off (`--no-ws`) when the API has none.

Both run one event loop per worker process. Run one worker per core. The
test suite serves the application with Granian in a subprocess, so a
change that breaks Granian fails CI.

## Background work

Work that is not a request runs as a background task of the event loop:
a cache value computed once for many callers, a round of readiness
checks, the end of an `Idempotency-Key`, a publisher that sends committed
messages. This part explains how such tasks start, who owns them, and
how they stay bounded.

### Background tasks: spawn and supervise

Every task the framework starts goes through `domainkit.tasks`. Your
project's own background work can use it too:

```python
from domainkit.cache import caches
from domainkit.tasks import spawn, supervise


async def warm_up() -> None:
    await caches["default"].get_or_set("rates", load_rates, Rates)


spawn(warm_up(), name="billing.warm_up")  # Once, in the background.

# Kept running: restarted after a crash, until shutdown.
supervise(lambda: watch_rates(feed), name="billing.rates", max_backoff=30)
```

`spawn(coro, name=..., log_errors=True, deadline=None)` returns an
`asyncio.Task`. The task:

- runs in a [clean context](#clean-contexts);
- is kept by a strong reference until it ends, so a task that waits on
  something only it holds is never garbage-collected half-way;
- logs an unexpected exception once, at ERROR, with its name, and counts
  it in `tasks_failed_total{task}`. Pass `log_errors=False` when callers
  await the task and handle its errors themselves;
- belongs to the event loop that runs it, which cancels it at
  [shutdown](#shutting-down-within-a-budget);
- with `deadline=` (the caller's `deadlines.current_deadline()`), works
  under that deadline: its transactions and outgoing calls are bounded by
  it. Nothing cuts the task off when the deadline passes. The caller that
  waits for it is cut off, and should cancel it.

`spawn` raises `RuntimeError` when no event loop is running, and closes
the coroutine without running it.

`supervise(factory, name=..., backoff=1.0, max_backoff=60.0)` keeps work
running. It calls `factory()` for each run, in a task of its own:

- A run that crashes (or that something other than the supervisor
  cancels) is logged and counted in `tasks_restarted_total{task}`. It
  starts again after a wait that doubles from `backoff` up to
  `max_backoff`, then is jittered to between half and all of it. So tasks
  that crashed together do not restart together.
- A run that stayed up for `max_backoff` seconds counts as healthy, so
  the next crash waits `backoff` again.
- A run that returns ends the supervision.
- Cancelling the supervisor, as the shutdown does, stops the run and
  every restart.
- `supervise` raises `ValueError` unless `0 < backoff <= max_backoff`.

`factory` is a `domainkit.tasks.Factory`: a function with no arguments
that returns a new coroutine each time.

Two more functions help with ownership:

- `tasks.adopt(task)` makes a task that started some other way owned too,
  so the shutdown waits for it. The framework does this for the closers
  it gave up on.
- `tasks.owned()` returns the running loop's owned tasks that have not
  finished.

Names label log lines and metrics: use fixed names, never request data.
Each event loop owns its own tasks, so the several loops of a test run or
a management command stay apart, and a loop that closes is not kept alive
by the tasks it left. See [ADR 0045](adr/0045-own-background-tasks-per-loop.md).

### Clean contexts

Context variables carry the request's facts: its id and actor
(`current_request()`), the `Idempotency-Key` it runs under, its
OpenTelemetry span, its language, and its time zone. A task started with
`asyncio.create_task` copies them. A task that outlives the request, or
serves many requests, would then carry the first request's facts for its
whole life. Its logs and spans would name that request, and a unit of
work it committed would mark that request's key.

`spawn` starts each task in a new, empty context instead
(`domainkit.tasks.clean_context()`). Only a frozen clock
(`domainkit.clock.freeze_time`) is carried, because it belongs to a test,
not to a request. Code that must create a task itself can pass
`context=clean_context()` to `loop.create_task`.

The framework's own background work runs this way:

| Task | Name |
| --- | --- |
| A `get_or_set` (or `@cached`) computation shared by concurrent callers | `domainkit.cache.compute` |
| A round of readiness checks shared by concurrent probes | `domainkit.readiness` |
| Storing a keyed response, or releasing the key | `domainkit.idempotency.settle` |
| Giving back the leases of a relay round, even once the relay is cancelled | `domainkit.messaging.settle` |

A cached value is shared by every caller of its key, so its factory must
not depend on who asked. Pass what it needs as arguments, and make them
part of the key. `@cache_page` keys entries by user and language, so it
runs the endpoint as the caller, in a copy of the caller's context,
inside the clean computation. A computation has a deadline of its own,
the cache's `COMPUTE_TIMEOUT`. One that borrows its caller's resources is
bound to that caller and its deadline instead
([cache.md](cache.md#what-a-factory-may-use)).

### Event handlers

In-process [event handlers](learning.md#words) run right after a unit of
work commits, inside the request. Each runs within
`EVENT_HANDLER_TIMEOUT` seconds (10 by default). A handler still running
then is cancelled, logged, and reported to the error reporters as
`HandlerTimedOut` (a `TimeoutError`). Then the next handler runs. A
handler may have its own limit:

```python
event_bus.subscribe(InvoicePaid, refresh_totals, timeout=2)
event_bus.subscribe(InvoicePaid, notify_crm, durable=True)  # In a worker.
```

A handler that must not be cut off, or must survive a crash, belongs in
the outbox. `durable=True` stores the call with the commit, and a worker
runs it and retries it ([messaging.md](contrib/messaging.md)). Durable
handlers take no `timeout`: the worker's task policies apply.

In a four-file domain, mark the handler where you write it, in
`controllers.py`. The domain subscribes it when it is ready, with no
`apps.py`:

```python
from domainkit.ddd import handles


@handles(InvoicePaid, durable=True)
async def notify_crm(event: InvoicePaid) -> None: ...
```

A handler may handle several events, with one `@handles` each. The mark
subscribes nothing by itself, so importing the module in a test does not
subscribe it. Django calls a marked handler a `@receiver`.

### Stores that stay bounded

The framework's background work in its stores is bounded too:

- **Idempotency keys.** A keyed request that raises or is cancelled
  releases its key in a task of its own, within 5 seconds, so a retry
  need not wait out the lease. Without messaging, a claim purges expired
  keys once a minute per process, and on each claim while a purge finds
  a full batch. It skips the keys another claim is deleting
  ([idempotency.md](contrib/idempotency.md)).
- **Audit sealing.** A run looks for unsealed rows from a high-water
  mark: the newest sealed row, less `AUDIT.SEAL_LATE_WINDOW`. So its cost
  follows what is new, not the length of the trail. It seals at most
  `AUDIT.SEAL_BATCHES` batches, and counts what it leaves in
  `audit_unsealed_rows` ([ADR 0047](adr/0047-seal-from-a-high-water-mark.md)).
  A second sealer never queues behind the first: when it finds the lock
  taken, it leaves the work to the first
  ([audit.md](contrib/audit.md#tamper-evidence)).
- **Outbox cleanup** reads the oldest done and cancelled messages from an
  index of their own. A run costs what it deletes, not the size of the
  outbox ([messaging.md](contrib/messaging.md)).
- **Password hashing.** A call keeps its place in the pool until its hash
  is done. Refusals ask clients to come back when the queue will have
  drained, and `AUTH.HASHING_RESERVED` places are kept for password
  changes ([users.md](auth/users.md#passwords)).
- **The in-memory rate limiter** forgets idle callers, at most 64 per
  request, so no one request pays for a crowd that left.

### Workers and the outbox

`runworker`, and the publisher that sends committed messages to a broker
(`domainkit.contrib.messaging`, see [messaging.md](contrib/messaging.md)),
stay correct under outages, overload, and shutdown. Committed messages
are never stranded, a process runs a bounded number of messages, every
broker call has a deadline, and cleanup never holds up claiming.

**One concurrency budget.** A worker process runs at most
`MESSAGING.CONCURRENCY` messages at once, whatever it serves. The polling
worker of each outbox, `runworker --once`, and the deliveries of every
queue a broker hands over all draw from one budget. A message holds its
slot from its claim (or delivery) until its outcome is recorded.

```python
MESSAGING = {"BROKER": "default", "CONCURRENCY": 20}  # 20 at once, in all
```

On RabbitMQ the prefetch belongs to each consumer. Quorum queues refuse a
prefetch shared by a channel, and RabbitMQ 4.3 quietly turns one into a
prefetch per consumer. So a worker of three queues may hold up to
`3 × CONCURRENCY` deliveries. At most `CONCURRENCY` of them run, and the
others wait for a slot, unacknowledged. Keep `TASK_TIMEOUT` times the
number of queues well below RabbitMQ's `consumer_timeout` (30 minutes by
default), or give busy queues workers of their own (`runworker --queue`).

**Deadlines on the broker.** Every RabbitMQ call has one:
`BROKERS[...].CONNECT_TIMEOUT` for connecting, opening channels,
declaring, binding, setting the prefetch, and starting or stopping
consumers, and `PUBLISH_TIMEOUT` for each confirmation. A broker that
accepts connections but stops answering fails the call. It does not hold
the publisher, and everything queued behind it, until its heartbeat ends.

**Publishing on commit.** The background publisher sends committed
messages to the broker without ever holding up a commit:

- *Leases.* A message is locked for a short lease while it is published:
  the broker's `publish_timeout` (`CONNECT_TIMEOUT` plus
  `PUBLISH_TIMEOUT` on RabbitMQ) and 5 seconds. Once the broker confirms
  it, the lock lasts `REPUBLISH_AFTER`. Whatever the broker did not
  confirm, for any reason (a refusal, the deadline, a stop), is unlocked
  at once. The unlock runs in a clean-up that a cancellation cannot
  interrupt, and that has 5 seconds of its own. If even that fails, the
  lease expires within seconds, and the relay sends the message.
- *A circuit breaker.* After `BREAKER_FAILURES` failed broker calls in a
  row (5), commits leave their messages to the relays for
  `BREAKER_COOLDOWN` seconds (30), touching neither the database nor the
  broker. Then one round probes: it checks that the broker answers before
  it locks any row. A confirmed message closes the breaker. The breaker
  logs once when it opens and once when it closes, and
  `messaging_publisher_breaker_open` shows it.
- *Bounded rounds.* A round locks at most `BATCH_SIZE` messages and has
  at most that many publications in flight. A backlog of thousands goes a
  batch at a time. Past 10,000 waiting messages, commits leave theirs to
  the relays (logged once until the backlog clears).
  `messaging_left_to_relay_total` counts the messages left to the
  relays, by reason.
- *An owned task.* The publisher's task is
  [supervised](#background-tasks-spawn-and-supervise). It runs in a clean
  context, never the committing request's. It starts again after a
  crash, after a pause that doubles up to a minute. It ends when it has
  nothing to do, so an idle publisher holds nothing of its event loop.
- *Shutdown.* The shutdown's [drain phase](#shutting-down-within-a-budget)
  closes the publishers, in the application and in management commands
  alike. It does so within the time that phase always has: a fifth of
  `SHUTDOWN_TIMEOUT` (3 seconds of the default 15), because the phase
  ends at half and begins by 30% at the latest. The flush ends
  `RELEASE_TIMEOUT` (5 seconds), or half that time if less, before then,
  so a publication it cuts short is unlocked within the phase. The relays
  send the rest. Cut short, the flush costs a few seconds' delay, never a
  message.

**Brokered shutdown.** A stopping worker stops consuming within 5
seconds, even from a broker that is lost or stalled, and logs it if it
could not. Running deliveries still get `MESSAGING.DRAIN_TIMEOUT` (20
seconds), counted from the stop. A delivery that arrives meanwhile is
given back to the broker, not run. Relays, cleanup, and the listener of a
stopping worker finish the step they are on, for up to 5 seconds, so a
round under way is published rather than given back. A second signal
stops everything at once.

A worker's shutdown is longer than a web process's, on purpose: its
messages get their time first, then the process shuts down within
`SHUTDOWN_TIMEOUT` like any other. With the defaults that is up to
20 + 5 + 15 = 40 seconds. So give worker pods a longer grace period than
Kubernetes' 30-second default (`terminationGracePeriodSeconds: 45`), or
lower `MESSAGING.DRAIN_TIMEOUT`.

**Cleanup.** Each polling and relaying worker deletes finished messages
older than `RETENTION_SECONDS`, in a task of its own, every
`CLEANUP_INTERVAL`. It starts a random 30 to 60 seconds after the worker
starts. It deletes `CLEANUP_BATCH_SIZE` rows per statement (1,000), each
batch in its own short transaction, and skips rows other workers lock on
PostgreSQL. A batch that fails (for example, past `STATEMENT_TIMEOUT`)
halves the next one, down to a single row, and full batches double back.
A slow table slows cleanup down but never stops it, and claiming never
waits for it.

**The database's clock.** On PostgreSQL, the outbox's server decides what
is due and when locks expire (`statement_timestamp()`). So a worker on a
host whose clock runs ahead never takes over another worker's message
early. A worker whose clock runs behind never leaves due work waiting. A
message due at once is due at once, whatever the clock of the host that
wrote it. SQLite lives in the worker's process, so its clock is the
host's. Under `domainkit.clock.freeze_time`, the frozen clock decides on
every database, so tests can move time. Delays (`delay=`, schedules)
still count from the clock of the host that writes them.

**Waking on commit.** On PostgreSQL (with asyncpg), a transaction that
writes messages due at once also sends `NOTIFY domainkit_messages` with
each queue's name. Polling and relaying workers `LISTEN` on one
connection of their pool. An idle worker then starts a committed message
at once, instead of on its next poll. So `POLL_INTERVAL` can be longer,
or `POLL_INTERVAL_IDLE` can let a listening worker poll less often while
it finds nothing.

- Polling stays. A notification is only a hint: it is sent when the
  transaction commits, and lost without harm when nobody listens. A
  worker whose listening connection drops polls until it listens again.
- Only rows the transaction wrote count (the inserts return them). A row
  skipped for its dedupe key, such as a periodic slot that every worker
  queues and one insert wins, wakes no one.
- With a broker that publishes on commit, the broker wakes the workers,
  and nothing is sent.
- Set `MESSAGING.NOTIFY` to `False` to turn it off.

**Gauges.** `messaging_outbox_messages` and `messaging_outbox_lag_seconds`
count the outbox at most once per `METRICS_TTL` seconds (15) in each
process. Scrapes that arrive during a count share it.

The outbox is not partitioned: see
[ADR 0043](adr/0043-the-outbox-database-decides-when-work-is-due.md).

## The event loop

Every request, every handler, and every background task of a process
shares one event loop. Code that holds the loop holds all of them. This
part explains which loop runs, how blocking code is found, and what is
safe with several threads.

### Event loops: uvloop

`runserver`, and every management command with an `async` handle
(`runworker`, `migrate`, `seed`, ...), run on
[uvloop](https://github.com/MagicStack/uvloop) when it is installed:
`pip install 'domainkit[uvloop]'`. uvicorn's `standard` extra brings it
too, on Linux and macOS. `EVENT_LOOP` chooses:

| `EVENT_LOOP` | Loop |
| --- | --- |
| `"auto"` (default) | uvloop when it can be imported, asyncio's own otherwise |
| `"asyncio"` | always asyncio's own: use it to rule uvloop out when you look for a bug |
| `"uvloop"` | uvloop, or a configuration error when it is missing |

`runserver` follows the setting on uvicorn and on Granian. Production
servers pick their own loop: uvicorn and Granian take `--loop uvloop`.
Both default to `auto`, and Granian's `auto` takes rloop first when it is
installed. Code that starts a loop of its own can follow the setting
with `loop_factory`:

```python
import asyncio

from domainkit.conf import settings
from domainkit.utils.loops import loop_factory

asyncio.run(main(), loop_factory=loop_factory(settings.EVENT_LOOP))
```

`loop_factory` returns `None` for asyncio's own loop, which is what
`asyncio.run` expects. uvloop does not run on Windows: `auto` falls back
to asyncio's loop there, and the extra installs nothing.

`domainkit.utils.loops.PerLoop` keeps one value per event loop, such as a
connection pool or a set of slots. Use it for state that belongs to a
loop, when loops may run on several threads:

```python
from domainkit.utils.loops import PerLoop

pools: PerLoop[Pool] = PerLoop()


async def pool() -> Pool:
    return pools.get(make_pool)  # Made on the loop's first use.
```

`get(make)` returns the running loop's value, and makes it with `make`
the first time. `find()` returns it or `None`, and `pop()` removes it. A
new loop drops the values of closed loops.

### Finding blocking code

A call that blocks inside an `async def` stops every request on the event
loop while it runs. Examples are a sync HTTP client, `time.sleep`,
`open()` on a slow disk, and a loop that uses the CPU for long. domainkit
looks for blocking code in five places, so you find it before
production:

- **While you develop.** With `DEBUG` on, the application runs its loop
  in asyncio's debug mode. Every callback or task step that holds the
  loop for `SLOW_CALLBACK_DURATION` seconds (0.1) or more is logged by
  the `asyncio` logger: `Executing <Task ... coro=<list_orders() running
  at shop/api/routes.py:42>> took 0.250 seconds`. Debug mode also
  reports resources left open, and work handed to the loop from another
  thread without `call_soon_threadsafe`. `SLOW_CALLBACK_DURATION = None`
  turns it off. Staging and production refuse `DEBUG`.
- **In tests.** `pytest --detect-blocking` runs every loop the tests
  start in debug mode. It fails a test whose loop was held that long in
  one step. `--blocking-seconds 0.5` raises the bar (0.1 is the least
  asyncio reports). Fixtures are not timed. Turn it on for one run, or
  always:

  ```toml
  [tool.pytest.ini_options]
  addopts = "-p domainkit.testing --detect-blocking"
  ```

  Debug mode reports resources left open as `ResourceWarning`. With
  `filterwarnings = error`, it finds unclosed connections too
  ([testing.md](testing.md#blocking-the-event-loop)).
- **In `check`, for routes.** `check` warns about the route handlers and
  dependencies of the installed domains written as `def`
  (`blocking.W001`). FastAPI calls each one on its thread pool (40
  threads by default): a hop to a thread per call, and a busy service
  queues on the pool. Make them `async def`. One that must stay sync,
  because it calls a library with no async API, can say so, and `check`
  stops warning about it:

  ```python
  from uuid import UUID

  from domainkit.http.blocking import allow_sync


  @allow_sync
  def render_invoice(invoice_id: UUID) -> bytes: ...
  ```

  Classes used as dependencies are left alone: FastAPI builds every class
  on its thread pool, whatever it does. The dependencies domainkit itself
  provides are `async`: `CurrentUser` (`acurrent_user`; `current_user`
  stays a plain function to call with a request), and a
  `ResourceRouter`'s unit of work and `FilterSet`, which it builds on the
  loop.
- **In `check`, for handlers and tasks.** Event handlers, in-process or
  durable, and messaging tasks are awaited on the event loop itself, with
  no thread pool between. One written as `def` runs all its work there.
  It holds every request and message of the process (and, behind a
  message with an ordering key, the rest of its key), then fails, because
  it returns nothing to await. `check` warns about each one
  (`blocking.W002`), in every environment. Make it `async def`, and hand
  blocking calls to `asyncio.to_thread`. `allow_sync` marks one that
  returns an awaitable on purpose.
- **While you write.** Projects made by `init` turn on Ruff's `ASYNC`
  rules. They catch `time.sleep`, `open()`, `subprocess`, and sync HTTP
  clients inside `async def`.

### TLS contexts

Building a TLS context reads the system's CA store and the certificate
and key files: milliseconds of blocking work. domainkit builds each
context once per process, for each set of TLS settings, and never on the
event loop while it serves:

| Connection | When the context is built |
| --- | --- |
| Databases (`SSLMODE`, `SSLROOTCERT`, `SSLCERT`, `SSLKEY`) | by `get_asgi_application`, before the server's loop serves; by a command, at its first connection |
| RabbitMQ (`TLS_CA`, `TLS_CERT`, `TLS_KEY`) | at the first connection, on a worker thread |
| Redis and Valkey (`rediss://`, `TLS_CA`, `TLS_CERT`, `TLS_KEY`) | at the first connection, on a worker thread, then shared by every connection of every pool with those settings (servers, Sentinel primaries, and Cluster nodes) |
| immudb (`TLS_CA`) | on the ledger's own thread |
| Outgoing calls (`HTTP_CLIENT.CA_BUNDLE`, `domainkit.http.client`) | at a client's first call on each loop, on a worker thread, then shared by every client with that bundle |

A process keeps its contexts until it ends. Restart it to use renewed
certificate files.

### Free-threaded Python

domainkit runs on free-threaded CPython 3.14 (`python3.14t`, without the
GIL). Importing domainkit and its runtime dependencies keeps the GIL off.
The `immudb` extra is the exception, because grpcio publishes no
free-threaded build yet.

CI runs these tests on 3.14t with the GIL off throughout: the tests of the
shared state below, and of the framework's own threads. It also runs the
free-threading tests 30 times more, each in a new random order
(`stress (3.14t)`, not a required check), so a race that shows in some
runs only fails the job.

Known limits:

- On 3.14.6t, some of those runs crash inside SQLAlchemy's compiled
  extensions while loops on threads make engines at the same time (seen
  at 0.9.2 too). This waits for a fix in SQLAlchemy. The required
  `test (3.14t)` job leaves those three tests (marked
  `engines_on_threads`) to the stress job, and a release does not wait on
  that job.
- The whole suite does not run on 3.14t yet. On 3.14.6t it crashes the
  interpreter now and then, at a different test each time, as it did
  before any of this was added. Five runs of it in November 2026
  (SQLAlchemy 2.1.4), with every extra but `immudb`, lost nine test
  processes in three runs to a segmentation fault inside CPython, in
  `PyObject_ClearWeakRefs`, as an object's last reference went. One
  process spun on a test that passes alone, with or without the GIL.
  This waits for a fix in the interpreter. 3.14.6 is still the newest
  3.14.

Most state lives in one event loop and needs no lock. What event loops on
several threads can share (a server with a loop per thread, or threads
that start their own loops) is guarded by a lock. The lock is held only
for plain Python work, never across an `await`:

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
  threads at once (live events, as clients connect) is subscribed once;
- the caches of each loop and the memory stores they share
  (`caches[...]`), so every loop of the process sees one memory store per
  alias, and an application's throttle backend, made once for all loops;
- each loop's slots of `ConcurrencyLimitMiddleware`, each loop's pool of
  an `HttpClient`, and each loop's principal reads under way (`PerLoop`,
  which drops closed loops as a new one comes), and the principal cache's
  claim of who reads an entry again.

Your project's own module-level state needs the same care. A dict that
two threads change at once needs a `threading.Lock` on free-threaded
Python, as it would with threads anywhere.

## Logging and error reports

Writing a log line can block. Standard output is a pipe that a log
collector may stop reading, and a file or socket can stall. A handler
called on the event loop would then stop every request. So domainkit
writes logs and error reports from threads of their own.

### Writing logs off the loop

`build_logging_config` makes a `LOGGING` mapping that writes through a
`domainkit.log.BackgroundHandler`. The project settings that `init`
writes use it:

```python
import logging

from domainkit.log import build_logging_config, configure_logging, flush_logs

configure_logging(build_logging_config(log_format="json", level="INFO"))

logger = logging.getLogger("shop.orders")
logger.info("Order placed")
logger.debug("Not written: below INFO")
flush_logs()
```

```text
{"timestamp": "2026-11-04T18:19:35.911+00:00", "level": "INFO", "logger": "shop.orders", "message": "Order placed"}
```

In a settings module you only assign the mapping:
`LOGGING = build_logging_config(log_format="json", level="INFO")`.
`setup()` applies it with `configure_logging`, which calls
`logging.config.dictConfig`. `log_format="text"` writes lines for people
(`TextFormatter`), with the request id in brackets:

```text
2026-11-04 18:19:36,246 INFO [shop.orders] [-] Order placed
```

`TextFormatter` escapes line breaks in messages, so a value cannot forge
a log line. `JsonFormatter` writes one JSON object per line.

How the handler works:

- The caller, the event loop included, merges the message, adds the
  request id and trace ids (they live in its context), and puts the
  record on a queue of 10,000 places (`queue_size`). One thread,
  `domainkit-logging`, formats and writes the record.
- An exception is formatted on the caller, into `exc_text`, as the
  standard library's `QueueHandler` does. So no queued record keeps a
  traceback, and with it the frames and requests that raised: the record
  behind the queue has no `exc_info`.
- A full queue never makes the caller wait. The record is dropped and
  counted (`handler.dropped`). The next record that fits is preceded by a
  warning from `domainkit.log` that counts the dropped records by level:
  `Dropped 120 log records (118 INFO, 2 WARNING): the logging queue was
  full`.
- Records below `WARNING` fill at most nine tenths of the queue (9,000 of
  10,000). The last tenth is kept for warnings and errors
  (`WARNING_RESERVE`, 0.1). So a flood of access lines while the stream
  is stalled cannot crowd out a security event or an error. A queue of
  fewer than 10 places keeps no reserve.
- At shutdown, the lifespan and management commands wait for the queue to
  drain in the `flush` phase, last of all, after error reports. Python's
  own logging shutdown does the same at exit. A stream that stays blocked
  is given up on, so it cannot hold the process: at exit, the handler's
  `flush()` and `close()` share one `FLUSH_TIMEOUT` (5 seconds), and once
  the flush gives up, the close does not wait again.
- `queue_size=None` writes on the caller's thread, as plain handlers do.
  Use it for scripts that must see each line before they go on.

`flush_logs(timeout=5.0)` waits for the queue to drain, and returns
whether it did. `aflush_logs(seconds=5.0)` does the same without holding
the event loop. The shutdown calls it.

Another `LOGGING` can put the handler in front of any handlers:

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

`ContextFilter` adds the request id and the trace ids to each record.
`BackgroundListener` is the thread that writes.

### Error reports

Error reporters (`ERROR_REPORTERS`) get every unexpected error: a 500, a
dead-lettered message. Each reporter is a `domainkit.reporting.Reporter`,
a function of the exception and a context mapping:

```python
from collections.abc import Mapping

import sentry_sdk


def report(exc: BaseException, context: Mapping[str, object]) -> None:
    sentry_sdk.capture_exception(exc)
```

```python
ERROR_REPORTERS = ["shop.reporting.report"]
```

Reporters run off the loop too, on one `domainkit-reporting` thread:

- `report_error(exc, **context)` queues the report and returns at once.
- Each reporter runs in a copy of the caller's context, so Sentry still
  sees the request's scope, and log lines keep its request id.
- At most 100 reports wait. More are dropped and counted, and the count
  is logged once there is room again.
- A reporter that fails is logged, and never breaks the caller.
- Shutdown waits for waiting reports, within 5 seconds, before the logs.
- `reporting.reporters()` returns the loaded reporters.

In tests, call `domainkit.reporting.flush_reports()` before you check
what was reported ([testing.md](testing.md#fixtures)).
`aflush_reports()` is the same for `async` code.

### What a log line costs

`JsonFormatter` writes the same line, byte for byte, that it wrote before
0.9, in 40% less time (4.5 µs instead of 7.4). Text goes through the
escaping `json.dumps` uses, and the date and time of the timestamp are
worked out once a second.

A line that the level drops (`domainkit.access` below `INFO`, say) costs
the request nothing: the access log checks the level before it builds
the line. A line the level keeps is made with
`domainkit.log.log_enabled(logger, level, msg, *args, extra=...)`. Your
own hot paths can use it too, once they have asked
`logger.isEnabledFor(level)`:

```python
import logging

from domainkit.log import log_enabled

logger = logging.getLogger("shop.orders")

if logger.isEnabledFor(logging.INFO):
    log_enabled(logger, logging.INFO, "Order %s placed", order_id)
```

`log_enabled` makes the record that `logger.log` makes, field for field
(the caller's file, line, and function included). It does not ask the
level again or walk the stack to find the caller: 15 Python calls fewer
a line. A logger of a class of its own (`logging.setLoggerClass`) logs
through its own `log`.

### Paths in logs

An access line, the request middleware's error lines, an error report,
and the request context (`domainkit.context.current_request().path`)
show the matched route's template, never the path the caller sent:

```text
GET /accounts/{account_id}/confirm/{token} 200 3.1ms
```

- An id, a token, or an email address in a path stays out of the logs,
  as metrics already leave it out of their labels.
- A request that no route matched shows `unmatched`. So do requests
  refused before the router ran (a 429, a body refused by its declared
  length, an untrusted host): the status says which.
- The template is read from the scope once the router has matched. Code
  that runs before the router (middleware) sees `unmatched` too.
- Problem details' `instance` still names the path the caller sent,
  because it answers that caller.
- Behind `--root-path /api`, the template leaves the root path out.
- `ACCESS_LOG_PATH = "raw"` shows the path as sent, everywhere, as before
  0.10. The default is `"template"`.

### Sampling the access log

Every request writes an access line. That costs the event loop and the
logging thread about 154,000 instructions between them, and adds a third
to a light request ([performance.md](performance.md#json-responses-and-logs)).
A busy service can write a share of the lines instead:

```python
ACCESS_LOG_SAMPLE_RATE = 0.1  # About one request in ten.
ACCESS_LOG_SLOW_DURATION = 1.0  # Seconds.
```

- These lines are always written, whatever the rate: every response with
  a status of 400 or more, every request that got no response at all,
  and requests that took `ACCESS_LOG_SLOW_DURATION` seconds or more. Only
  successes and redirects are sampled.
- Client errors are kept because an investigation reads them. A scoped
  resource answers 404 for what the caller may not reach. So a caller
  that tries other tenants' ids leaves a 404 line for every id tried,
  and a healthy service answers few of them.
- Each other request is written with probability
  `ACCESS_LOG_SAMPLE_RATE`, drawn for that request alone. This is a fair
  sample, whatever the traffic's pattern, and a caller cannot steer it.
  A line left out costs nothing more than the draw.
- To count requests from a sample, count each sampled line as
  `1 / ACCESS_LOG_SAMPLE_RATE` requests, and each always-written line as
  one. The status and `duration_ms` of the line say which it is.
- Only `domainkit.access` is sampled. Security events
  (`domainkit.security`: rejected hosts, bodies, and credentials),
  warnings, and errors of every other logger are written as before. The
  audit trail (`domainkit.contrib.audit`) is a table, not a log. Sampling
  never touches either.
- `1.0`, the default, writes every line.

Refusals are throttled instead, by caller, with
`domainkit.log.ThrottledLog`:

- The first line of a caller is always written: a rejected host, a body
  (too large or too slow), refused credentials, a refused live
  connection, or an unreadable `X-Forwarded-For` hop. The caller is its
  address's rate-limit key, or the proxy's.
- Its repeats in the next 10 seconds are counted, and the caller's next
  line ends `(and N more like it since the last line)`. A flood from one
  caller writes a line every 10 seconds, not one per request, and cannot
  crowd the logging queue. Each new caller's first line is still written.
- The 1,024 latest callers are remembered per middleware. A caller that
  is let go writes a line for what it held back.
- A caller that stops still has its count written: by the next line of
  any caller once its 10 seconds have passed (`N more
  refused_credentials line(s) for ip:203.0.113.9 were held back`), and at
  the latest by the shutdown's last phase, before the logs are flushed.
  A quiet service that gets no other refusal writes the count at
  shutdown. So read the security log up to the process's last line
  before you decide how large a burst was.
- An address held back for refused credentials (past `RATE_LIMIT`) is
  logged when the hold starts (`Holding back credentials from ... for N
  s: more refused than RATE_LIMIT allows (100/minute)`). The requests the
  hold refuses unread are counted on that line's key
  (`held_credentials`), throttled like the rest.

`ThrottledLog(logger, interval=10.0, keys=1024)` serves your own
refusals the same way. `flush_throttled()` (and `aflush_throttled()`)
writes the counts held back; the shutdown calls it. `reset_throttled()`
forgets every count, for tests.

## Timeouts and load

A busy service stays responsive when each piece of work has a time limit
and the process refuses what it cannot finish. This part explains
request deadlines, load shedding, database timeouts, and outgoing calls.

### Request deadlines

Every HTTP request gets a deadline when it arrives: `REQUEST_TIMEOUT`
seconds (30 unless set), started by `DeadlineMiddleware` before any other
work. The deadline covers everything until the response starts:
authentication, reading the body, the handler, and the database and
outgoing calls it waits on. When the deadline passes, that work is
cancelled, and the request is answered:

```text
HTTP/1.1 503 Service Unavailable
Retry-After: 1

{"type": "about:blank", "title": "Service Unavailable", "status": 503,
 "detail": "The request took too long; try again shortly.",
 "instance": "/exports", "code": "deadline_exceeded",
 "request_id": "0349df9e1f174fd67d8a83f40c5344db"}
```

One warning is logged: `POST /exports ran past its deadline of 30s`. The
cancellation unwinds the request as any other error does. An open unit
of work rolls back and gives its connection back. An `Idempotency-Key`
that committed nothing is released, so the client can try again with
the same key.

A route that needs more time, or less, says so with `RequestTimeout`.
The time counts from when the request arrived, and `None` removes the
deadline:

```python
from fastapi import APIRouter, Depends

from domainkit.contrib.auth import IsAdminUser
from domainkit.http.access import Require
from domainkit.http.deadlines import RequestTimeout

router = APIRouter()


@router.post(
    "/exports", dependencies=[Depends(Require(IsAdminUser())), Depends(RequestTimeout(300))]
)
async def export() -> None: ...
```

The body is read before route dependencies run, so it is read under
`REQUEST_TIMEOUT`.

**Deadlines and idempotency keys.** With `domainkit.contrib.idempotency`
installed, a keyed request must end before its key's lease does.
Otherwise a retry may take the key over while the request runs:

- `REQUEST_TIMEOUT` must be below `IDEMPOTENCY.LEASE_SECONDS`. Startup
  fails otherwise.
- `check` reports a `POST` or `PATCH` route whose own deadline is at the
  lease or past it, or removed. The export above is one, with the
  default lease of 60 seconds. Staging and production refuse to start
  with one. Raise the lease above the longest such deadline, or shorten
  the route's.
- With no deadline at all (`REQUEST_TIMEOUT = None`, or no
  `DeadlineMiddleware`), keyed requests can outlive the lease, so staging
  and production refuse to start (`idempotency.E002`).
  `IDEMPOTENCY.REQUIRE_DEADLINE = False` only warns.
- A keyed response's background tasks run after its response is stored,
  so they run once, however long they take
  ([idempotency.md](contrib/idempotency.md#idempotency-safe-retries-of-unsafe-requests)).

**What has no deadline.** The deadline is lifted as soon as the response
starts:

- A streamed response (an export, Server-Sent Events) runs as long as it
  streams. This covers every long-lived response, whether or not it
  marks its scope `domainkit.long_lived`, as an event stream does.
- Background tasks that run after the response are not cut off.
- WebSocket connections never get a deadline. Their limits are their
  own ([live.md](live.md)).
- A long-lived endpoint that does slow work before it starts answering
  uses `RequestTimeout(None)`, or a longer deadline.
- Code can lift the deadline of the work it is doing with
  `domainkit.deadlines.current_deadline().clear()`.
- Tasks a request starts share its deadline while the request runs, and
  are free of it once the request ends.

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

- `within` and `timeout` bound their work by the smaller of their own
  seconds and the time left. They make that the deadline inside, so what
  they call sees the tighter one.
- When their own seconds run out, they raise `TimeoutError`. Your code
  may catch it and work around it.
- When the deadline runs out, the work is cancelled up to the block that
  set the deadline, and that block raises. The request answers 503, and
  a `timeout` block raises `DeadlineExceeded` (a `TimeoutError` too).
- A block entered with no time left raises `DeadlineExceeded` at once,
  without running.
- Seconds that are not a finite number (NaN, infinity) raise
  `ValueError`. `None` means the block has no bound of its own.
- `within` replaces `asyncio.wait_for`. With no time left, it closes the
  coroutine unawaited.

Workers and commands bound their own work the same way:
`async with deadlines.timeout(60): await handle(message)`. Outside a
request there is no deadline until a block sets one.

The deadline itself is a `deadlines.Deadline`:

- `deadlines.current_deadline()` returns the running work's deadline, or
  `None`.
- `deadline.remaining()` returns the seconds left (never below 0), and
  `deadline.expired()` whether it has passed.
- `deadline.reset(seconds)` moves it to `seconds` after the work
  started, and `deadline.clear()` removes it. The timer that enforces
  the deadline follows.
- `deadlines.enforced()` says whether a timer cuts the running task off
  when the deadline passes: true in the task that entered a `timeout`
  block (an HTTP request included), false in a task that only shares a
  deadline. Code that bounds a wait by the deadline itself can skip that
  when the timer already does.
- `deadlines.Timeout` is the class `timeout()` returns. After the block,
  `expired()` says whether its own timer ran out.

`remaining()` reads `domainkit.clock`, so a frozen clock stops it. The
timers that cancel work run on the event loop's own clock.

### What keeps to it

- **The database.** On PostgreSQL, a transaction begun with less time
  left than `STATEMENT_TIMEOUT` (by a second or more, `DEADLINE_SLACK`)
  sets `statement_timeout` to the time left, with `SET LOCAL`, so the
  server stops the work too. SQLite has no such setting: the
  cancellation stops the wait. A transaction begun after the deadline
  passed (cleanup) keeps `STATEMENT_TIMEOUT`. The `SET LOCAL` statement
  is not counted by `assert_num_queries`.
- **Reads without a transaction.** A read that runs without a
  transaction (`Reads.STATEMENTS`,
  [queries.md](queries.md#how-read-only-work-reads)) has no `SET LOCAL`.
  Each of its statements is cut off at the deadline by the request's
  timer, or by a timer of its own in work that shares a deadline without
  enforcing it. The driver then cancels the statement on the server.
- **A statement cut off.** With asyncpg, outside PgBouncer, a statement
  that is cut off keeps its connection once it has settled, within 2
  seconds. asyncpg has finished the cancelled exchange, and the
  transaction is rolled back, with its locks. What the statement may have
  left in the session that outlives a rollback (advisory locks, cursors,
  temporary tables, `LISTEN`) is undone. Settings it changed stay, as
  after any statement. Code that goes on after the cut gets
  `PendingRollbackError` until it rolls back. A connection that does not
  settle in time, one cut off under an anyio cancel scope (which cancels
  that wait too), and the other drivers' connections are closed instead,
  and the pool replaces them. The framework's own short writes, such as
  an idempotency key's claim, run that way too, within
  `STATEMENT_TIMEOUT`.
- **Outgoing HTTP calls** through `domainkit.http.client` are bounded by
  the deadline ([below](#outgoing-http-calls)).
- **Worker tasks.** The messaging worker runs each message under a
  deadline of its timeout (the task's `timeout`, or `TASK_TIMEOUT`). So a
  task's transactions and outgoing calls keep to it as a request's do.
- **Cache computations** run under the cache's `COMPUTE_TIMEOUT`, or,
  when they borrow their caller's resources, under that caller's
  deadline ([cache.md](cache.md#what-a-factory-may-use)).
- **Ledgers and caches** keep their own `TIMEOUT`. A request's call to
  either is cancelled at the deadline, like anything else it awaits, and
  the request answers 503. Your own code can bound one await by both, and
  refuse at once when no time is left: `await deadlines.within(call(), 5)`.

Choose `REQUEST_TIMEOUT` below the load balancer's idle timeout (60
seconds on most), so a slow request gets the application's 503 rather
than the balancer's 504. Choose `STATEMENT_TIMEOUT` no longer than
`REQUEST_TIMEOUT`, so transactions rarely need to set their own.
[ADR 0048](adr/0048-request-deadlines.md) explains the choices.

### Load shedding

`ConcurrencyLimitMiddleware` lets each event loop work on at most
`MAX_CONCURRENT_REQUESTS` HTTP requests at once (1,000 unless set; `None`
turns it off). A process serves on one loop, so that is the cap per
process. A server that runs several loops in one process (free-threaded
Python, a loop per thread) admits the cap on each loop.

A request past the cap waits for a slot, first come first served, for up
to `REQUEST_QUEUE_TIMEOUT` seconds (1), behind at most
`MAX_QUEUED_REQUESTS` others (100). When the queue is full, or the wait
runs out, the request is answered at once:

```text
HTTP/1.1 503 Service Unavailable
Retry-After: 1

{"detail": "The server is busy; try again shortly.", "code": "overloaded", ...}
```

- **Probes are never shed.** The health and readiness probes and the
  metrics scrape (`CONCURRENCY_EXEMPT_PATHS`: `/health`, `/ready`,
  `/metrics`) are never counted or refused, so a busy replica is not
  taken for a dead one. Behind `--root-path`, the paths are compared
  without it.
- **A slot is held until the response starts.** A request holds its slot
  until its response starts, or until it ends. A streamed response, an
  event stream among them, gives its slot back as it starts, and
  WebSocket connections never take one. A request that is cancelled,
  while it waits or while it runs (its deadline passed, or the server
  cancelled it when the client went away), gives its slot or its place
  back.
- **Time spent waiting on the client is not counted.** While the
  application waits for a chunk of the request's body, the request's
  slot is lent to the next request. When the chunk arrives, the request
  takes a slot back through the queue, as a new request would. It is
  answered 503 `overloaded` if no slot frees in time. So clients that
  send their headers, and then their body slowly or never, hold no slot.
  `RequestBodyLimitMiddleware` answers them 408 `request_timeout` once
  the body has taken `REQUEST_BODY_TIMEOUT` seconds (10) in all, and
  closes the connection. A buffering proxy in front (nginx's
  `proxy_request_buffering on`) still reads slow bodies before the
  application sees them ([reference/servers.md](reference/servers.md)).
- **Shedding is logged** at most every 10 seconds, with a count:
  `Shed N request(s) past MAX_CONCURRENT_REQUESTS ...`. With
  `domainkit[metrics]`, `http_requests_admitted` and
  `http_requests_queued` show the slots and the queue, and
  `http_requests_shed_total` counts refusals by `reason` (`queue_full`,
  `queue_timeout`).
- **The order matters.** List the middleware right after
  `DeadlineMiddleware` (the wait counts against the deadline) and before
  `AuthenticationMiddleware`, so a refused request costs no database
  lookup. A request whose deadline passes while it waits in the queue is
  answered 503 `deadline_exceeded`. It is counted and logged as shed
  (`queue_timeout`) too, because it never got a slot. Queueing, refusing,
  and leaving the queue cost the same however long the queue is.
- Both middleware are in `DEFAULT_MIDDLEWARE` and in the settings `init`
  writes. `check` warns in staging and production when either is missing
  (`deploy.W001`, `deploy.W002`).

Set the cap from what the process can finish. With a database pool of
`POOL_SIZE` plus `MAX_OVERFLOW` connections, requests past a few times
that number only queue for connections. Keep uvicorn's
`--limit-concurrency` above the cap plus the queue, as a backstop
([reference/servers.md](reference/servers.md)).
[ADR 0049](adr/0049-shed-load-past-a-concurrency-cap.md) explains the
choices.

### Database timeouts and PgBouncer

PostgreSQL connections get three kinds of bounds:

- **Server-side:** `STATEMENT_TIMEOUT`, `LOCK_TIMEOUT`, and
  `IDLE_IN_TRANSACTION_TIMEOUT`, set for the session when it connects
  (migrations are exempt), and `statement_timeout` lowered per
  transaction to a request's deadline.
- **Client-side:** asyncpg waits at most `CONNECT_TIMEOUT` (10 seconds)
  to connect, instead of its own 60, which `POOL_TIMEOUT` does not cover.
  It waits at most `COMMAND_TIMEOUT` for one statement
  (`STATEMENT_TIMEOUT` plus 5 seconds unless set). So a network or a
  server that stops answering cannot hold a request. The margin lets the
  server's timeout end a slow statement first, with an error that keeps
  the connection. psycopg gets `connect_timeout`.
- **The pool:** `POOL_TIMEOUT` (10 seconds for network databases).

**Behind PgBouncer** in transaction pooling, a client's statements may
run on a different server connection in each transaction, so nothing can
be kept in the session. Set `PGBOUNCER = True`:

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

Then:

- asyncpg keeps no statement cache (`statement_cache_size=0`, and
  SQLAlchemy's `prepared_statement_cache_size=0`), and each prepared
  statement gets a unique name. So two clients never collide on one
  server connection.
- The timeouts are not sent at connect time, because PgBouncer refuses
  unknown startup parameters. They are set at the start of each
  transaction with `set_config(..., true)`, which is `SET LOCAL`. Each
  transaction pays one more round trip for its timeouts.
- psycopg gets `prepare_threshold=None`.
- This is the setup SQLAlchemy documents for asyncpg behind PgBouncer.
- domainkit itself takes only transaction-scoped advisory locks, which
  transaction pooling allows.
- `runworker` does not listen for new messages through PgBouncer
  (`LISTEN` needs a session of its own). Its workers poll every
  `POLL_INTERVAL`, and `NOTIFY` from commits is not heard.
- Reads that would run without a transaction (`Reads.STATEMENTS`) and the
  framework's own short writes run in a transaction through PgBouncer,
  which sets the timeouts.
- A pool tests a connection (`POOL_PRE_PING`) with SQLAlchemy's own ping,
  in the transaction its prepared statement needs. Elsewhere, domainkit
  sends the ping without one.

Point `MIGRATION_URL` at PostgreSQL directly: migrations may take
longer, and need the schema owner.

`check` warns in staging and production about a database that seems to
go through PgBouncer without the mode (`deploy.W003`): one on port 6432,
or one whose `CONNECT_ARGS` turn the statement cache off by hand.
`PGBOUNCER` on anything but PostgreSQL is refused.

### Outgoing HTTP calls

`domainkit.http.client` (the `httpclient` extra, on httpx2) calls other
services from a request, a task, or a command, within the deadline of
the work it serves:

```python
from decimal import Decimal

from domainkit.http.client import HttpClient

rates = HttpClient(base_url="https://rates.example.com/v1", timeout=3.0)


async def euro_rate() -> Decimal:
    response = await rates.get("/rates/EUR")
    response.raise_for_status()
    return Decimal(response.json()["rate"])
```

Make one client per service, at module level. Nothing opens until the
first call.

- Each client keeps one pool of connections per process (per event
  loop), opened by its first call. The pool closes with the other
  resources when the application or a command ends.
  `domainkit.http.client.close_all()` closes every client's connections
  on the running loop.
- `domainkit.http.client.client` is a ready client for one-off calls.
- A client has `get`, `head`, `options`, `post`, `put`, `patch`,
  `delete`, and `request(method, url, ...)`. Each takes `params`,
  `headers`, `content`, `data`, `json`, `timeout`, and `retry`.
- `HttpClient(...)` takes `base_url`, `headers` (sent with every call),
  `timeout` and `retries` (in place of `HTTP_CLIENT`'s), `guard`,
  `allow_http`, `allowed_networks`, `propagate`, `breaker`, `transport`,
  and `http2`. `http2=True` speaks HTTP/2 where the server does (it needs
  `httpx2[http2]`), as Apple's push service requires.
- A relative URL is appended to `base_url`, and may not climb out of its
  path. A `.` or `..` segment, spelled out or percent-encoded (`%2e%2e`,
  `..%2f`), raises `UnsafeURL`. A URL that names a host must keep to the
  `base_url`'s origin. So a path built from someone's input cannot send
  the call elsewhere.

A client keeps one circuit per host for all its callers (below), so make
one client per trust domain. A client that calls URLs your users chose
should not share its circuits with your own services' client, and one
user's failing URLs should not refuse another's calls. Give such a
client `breaker=False`:

```python
from domainkit.http.client import HttpClient

callbacks = HttpClient(guard=True, breaker=False, retries=0)
```

Every call:

- is bounded by its `timeout` (`HTTP_CLIENT.TIMEOUT`, 10 seconds),
  retries included, and by the deadline. Past `timeout` it raises
  `CallTimeout`. Past the deadline the work is cut off, as above.
  httpx2's own timeouts get the time left, and connecting gets at most
  `HTTP_CLIENT.CONNECT_TIMEOUT` (5 seconds).
- is retried up to `retries` times (`HTTP_CLIENT.RETRIES`, 2) after a
  network error, a timeout, or a 429, 502, 503, or 504 answer, when its
  method is idempotent (GET, HEAD, OPTIONS, PUT, DELETE, TRACE) or it
  passes `retry=True`. A call that could not connect is retried whatever
  its method, because nothing was sent. `retry=False` retries nothing.
  The waits are jittered ("full jitter": random, below `BACKOFF` doubled
  per retry, at most `MAX_BACKOFF`), and a `Retry-After` is honoured up
  to `MAX_BACKOFF`. No retry starts that could not finish in time. Out of
  retries, the last answer is returned, or the last error raised.
- fails at once with `CircuitOpen` while its host's circuit is open:
  after `BREAKER_FAILURES` failures in a row (network errors, timeouts,
  5xx answers), for `BREAKER_COOLDOWN` seconds. After that, one call at a
  time tries the host again. A call cut off by the work's deadline is not
  held against the host. Opening is logged once. A client made with
  `breaker=False` keeps no circuits. The circuits are a
  `CircuitBreaker`, shared by the event loops of every thread.
- sends the request id (`X-Request-ID`) and the trace context
  (`traceparent`, `tracestate`) of the work it serves.
- reads at most `MAX_RESPONSE_BYTES` of the answer (10 MiB, counted as it
  comes and after decompression), or raises `ResponseTooLarge`. It
  follows no redirects. httpx2 inflates a compressed answer a piece of
  at most 1 MiB at a time, and each piece is counted before the next. So
  a small answer that inflates a thousandfold costs the limit and a
  piece, never its whole inflated size. Bytes that inflate to nothing
  still count.
- is logged by method, origin, and path: never a query string, a header,
  or a body. Keep secrets out of paths, or use a guarded client, whose
  logs show only the origin.

#### URLs someone else chose

URLs someone else chose (a callback, a link to fetch) go through a
client made with `guard=True`. It calls only public addresses, with the
checks `domainkit.contrib.webhooks` uses (`domainkit.http.guard`):

- `https` only (`allow_http=True` adds `http`);
- no credentials in the URL;
- not `localhost`, and not a cloud metadata name;
- every address the host resolves to is public, unless it is in
  `allowed_networks`. Metadata addresses never are.

The host is resolved, and every address checked, as each connection
opens. The connection goes to an address that was checked, so DNS
rebinding gains nothing. TLS still checks the certificate against the
host name. A guarded client:

- ignores proxy settings from the environment, and the CAs it names
  (`SSL_CERT_FILE`, `SSL_CERT_DIR`). It trusts `CA_BUNDLE`, or the
  system's CAs.
- asks for uncompressed answers, and refuses a compressed one anyway
  (`httpx2.DecodingError`), unless the call's own `Accept-Encoding` took
  it.
- sends no request id or trace context unless `propagate=True`.
- takes no `transport=`, which would skip its address checks. Tests use
  `stub`.

A refused URL raises `UnsafeURL`, which answers 422 `unsafe_url` when the
request does not catch it.

**Lookups.** Host names are looked up on threads of the guard's own,
never the event loop's default executor, which `asyncio.to_thread` and
new database connections need:

- At most `GUARD_LOOKUP_THREADS` (16) lookups run at once, 2 of one host,
  and a quarter of the threads for one owner. The owner is the request's
  user, or its address (an IPv6 network), or whom
  `guard.lookups_for(owner)` names around the calls (each webhook
  delivery's subscription, `webhook:<id>`).
- A host whose lookup failed fails at once for 5 seconds, or for a
  minute if the lookup took 5 seconds or more to fail (the system's
  resolver timed out: its DNS does not answer).
- A lookup past those bounds fails at once, as a connection error. So
  hosts that one owner chose and that never answer cost the other owners
  nothing.
- A lookup that succeeds after 5 seconds or more is not held against its
  host. The system's resolver tries the next name server after 5
  seconds, so a slow success is what one name server down looks like,
  and the host resolves. Its answer is kept for a minute instead, for
  that host and port, and lookups meanwhile get it at once, without a
  thread. Callers check its addresses every time, as they check a fresh
  answer's. At most 1,024 such answers are kept, the least recently used
  let go first. A kept answer can be up to a minute older than the host's
  DNS says.
- A host whose DNS fails in under 5 seconds takes a thread on each new
  lookup, at most 2 at once.
- The share is per owner. For a webhook subscription, the owner is the
  subscription's owner, so one customer's many subscriptions share one
  quarter ([webhooks.md](contrib/webhooks.md#webhooks-signed-events-for-other-services)).
  Otherwise it is a user. Whoever controls many owners (many users, many
  staff subscriptions) can still hold many threads, for the first round
  of slow lookups at least. A lookup cannot be interrupted; an
  asynchronous resolver will change this.

#### What a call raises

| Raised | When | A request that lets it go answers |
| --- | --- | --- |
| `CallTimeout` (an `httpx2.TimeoutException`) | `timeout` passed, retries included | 504 `upstream_timeout` |
| `DeadlineExceeded` | no time was left when the call began | 503 `deadline_exceeded` |
| `CircuitOpen` (an `httpx2.TransportError`) | the host's circuit is open | 503 `upstream_unavailable`, `Retry-After` the cooldown left |
| `ResponseTooLarge` (an `httpx2.RequestError`) | the answer was over `MAX_RESPONSE_BYTES` | 502 `upstream_response_too_large` |
| `UnsafeURL` | a guarded client may not call the URL, or it leaves `base_url` | 422 `unsafe_url` |
| `httpx2.HTTPError` | the call failed on its last try | 500 `internal_error`, reported |

`CallTimeout`, `CircuitOpen`, and `ResponseTooLarge` are
`domainkit.exceptions.UpstreamError`s too. The client logged them, so
they are answered without a report to `ERROR_REPORTERS`, with a detail
that names no host. Catch them to degrade instead.

#### Outgoing calls in tests

`stub` sends every call made in its block, from any client, to a handler,
through httpx2's `MockTransport`:

```python
from decimal import Decimal

import httpx2

from domainkit.http.client import stub


def rates_api(request: httpx2.Request) -> httpx2.Response:
    assert request.url.path == "/v1/rates/EUR"
    return httpx2.Response(200, json={"rate": "1.08"})


async def test_euro_rate() -> None:
    with stub(rates_api):
        assert await euro_rate() == Decimal("1.08")
```

The handler may be `async`, or a transport of its own. A guarded
client's URL checks still apply. The address checks need the network, so
a stub skips them. A client made with `transport=` always uses that
transport. [ADR 0050](adr/0050-outgoing-http-client.md) explains the
choices.

## Options

Each setting has its full row on
[reference/settings.md](reference/settings.md#core) or
[reference/http.md](reference/http.md#http).

| Setting | Default | What it does |
| --- | --- | --- |
| `SHUTDOWN_TIMEOUT` | `15.0` | Seconds a shutdown may take in all, from the domains' hooks to the logs. |
| `EVENT_LOOP` | `"auto"` | The loop of `runserver` and of `async` commands: `"auto"`, `"asyncio"`, or `"uvloop"`. |
| `SLOW_CALLBACK_DURATION` | `0.1` | With `DEBUG`, log each loop step that holds the loop this long. `None` turns it off. |
| `LOGGING` | minimal | The `dictConfig` mapping `setup()` applies. `build_logging_config` makes one. |
| `ERROR_REPORTERS` | `()` | Reporters by dotted path, run on the reporting thread. |
| `EVENT_HANDLER_TIMEOUT` | `10.0` | Seconds an in-process event handler may run. |
| `ACCESS_LOG_SAMPLE_RATE` | `1.0` | The share of successful requests that write an access line. |
| `ACCESS_LOG_SLOW_DURATION` | `1.0` | Seconds after which a request always writes its line. |
| `ACCESS_LOG_PATH` | `"template"` | `"raw"` logs the path as sent. |
| `READINESS_TIMEOUT` | `5.0` | Seconds each readiness check may take. |
| `READINESS_CACHE_SECONDS` | `1.0` | Seconds a round's result is reused. |
| `READINESS_DETAILS` | `False` | List the failing checks in a 503 from `/ready`. |
| `REQUEST_TIMEOUT` | `30.0` | Seconds a request may take until its response starts. `None` removes the deadline. |
| `MAX_CONCURRENT_REQUESTS` | `1000` | Requests each event loop works on at once. `None` turns shedding off. |
| `MAX_QUEUED_REQUESTS` | `100` | Requests that may wait for a slot. |
| `REQUEST_QUEUE_TIMEOUT` | `1.0` | Seconds a request may wait for a slot. |
| `CONCURRENCY_EXEMPT_PATHS` | `("/health", "/ready", "/metrics")` | Paths never counted or shed. |
| `REQUEST_BODY_TIMEOUT` | `10.0` | Seconds a request body may take to arrive (408 after). |
| `HTTP_CLIENT` | see below | The defaults of every `HttpClient`. |
| `GUARD_LOOKUP_THREADS` | `16` | Threads for a guarded client's host lookups. |

`HTTP_CLIENT` holds `TIMEOUT` (10.0), `CONNECT_TIMEOUT` (5.0), `RETRIES`
(2), `BACKOFF` (0.1), `MAX_BACKOFF` (2.0), `BREAKER_FAILURES` (5),
`BREAKER_COOLDOWN` (30.0), `MAX_CONNECTIONS` (100),
`MAX_KEEPALIVE_CONNECTIONS` (20), `MAX_RESPONSE_BYTES` (10 MiB),
`USER_AGENT` (`"domainkit"`), and `CA_BUNDLE` (`None`). The database
timeouts (`STATEMENT_TIMEOUT`, `LOCK_TIMEOUT`,
`IDLE_IN_TRANSACTION_TIMEOUT`, `CONNECT_TIMEOUT`, `COMMAND_TIMEOUT`,
`POOL_TIMEOUT`, `PGBOUNCER`) are keys of `DATABASES`
([reference/backends.md](reference/backends.md#databases-and-migrations)).
The worker's settings are keys of `MESSAGING`
([messaging.md](contrib/messaging.md)).

## Errors and refusals

| What you see | Why | What to do |
| --- | --- | --- |
| `503 deadline_exceeded`, and `... ran past its deadline of 30s` in the log | The request took longer than its deadline. | Make the work faster, or give the route a `RequestTimeout` of its own. |
| `503 overloaded`, and `Shed N request(s) past MAX_CONCURRENT_REQUESTS` | The process was full and the queue was full, or the wait ran out. | Add processes, or find the slow route ([runbook.md](runbook.md#requests-are-shed)). |
| `408 request_timeout` | The body took longer than `REQUEST_BODY_TIMEOUT` to arrive. | A slow or stalled client. Put a buffering proxy in front. |
| `CheckFailed: The application has problems` at startup | A route without a rule, clashing operation ids, or a domain's own check, in staging or production. | Fix each listed problem. |
| `CheckFailed: The application has problems since it was built` | A route added after `get_asgi_application` returned has a problem. | Give the late route its rule. |
| `TimeoutError` from `within` or `timeout` | Their own seconds ran out. | Catch it, or give more time. |
| `DeadlineExceeded` | The deadline ran out, or no time was left on entry. | Let it go: the request answers 503. |
| `RuntimeError: spawn('...') needs a running event loop.` | `spawn` was called outside a running loop. | Call it from `async` code. |
| `ValueError` from `supervise` | `backoff` is not positive, or is above `max_backoff`. | Pass `0 < backoff <= max_backoff`. |
| `ValueError` from `lifecycle.register` | The name is one of the framework's kinds, or the phase is `domains` or `tasks`. | Choose another name, or the `drain`, `resources`, or `flush` phase. |
| `Background tasks still running at the end of their shutdown phase: ...` | Owned tasks ignored cancellation. | Make the named tasks end when cancelled. |
| `HandlerTimedOut` in the error reports | An in-process handler ran past its timeout. | Make it faster, give it a `timeout`, or make it durable. |
| `CallTimeout`, `CircuitOpen`, `ResponseTooLarge`, `UnsafeURL` | See [What a call raises](#what-a-call-raises). | Catch them to degrade. |

## Common mistakes

- **Starting tasks with `asyncio.create_task`.** The task copies the
  request's context and may be collected half-way. Use `spawn`, with a
  fixed name.
- **A grace period shorter than the shutdown.** uvicorn's graceful
  timeout plus `SHUTDOWN_TIMEOUT` must fit in the orchestrator's grace
  period, or the stop ends in `SIGKILL`. Workers need more: up to 40
  seconds with the defaults.
- **Blocking calls in `async def`.** One `time.sleep` or sync HTTP call
  holds every request of the process. Turn on `--detect-blocking` in
  tests, and use `asyncio.to_thread` for what must block.
- **`asyncio.wait_for` for a bounded wait.** It ignores the request's
  deadline. Use `deadlines.within`, which takes the smaller of the two.
- **One `HttpClient` for your services and your users' URLs.** They then
  share circuits, and user-chosen URLs are not guarded. Make a guarded
  client with `breaker=False` for user-chosen URLs.
- **Raising `MAX_CONCURRENT_REQUESTS` to stop 503s.** The requests then
  queue for database connections instead, and time out after they hold
  memory. Size the cap from the pool.

## See also

- [Operations](operations.md): how the processes run in production.
- [Performance](performance.md): what each piece of work costs.
- [Server limits](reference/servers.md): production commands for uvicorn
  and Granian.
- [HTTP reference](reference/http.md#middleware): the middleware order.
- [Messaging](contrib/messaging.md): the outbox and the worker.
- [Testing](testing.md#blocking-the-event-loop): finding blocking code in
  tests.
- [The glossary](learning.md#words).
