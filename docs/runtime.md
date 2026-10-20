# Running under load

How domainkit keeps a service responsive when it is busy: every request has
a deadline, and what it waits on keeps to it; the process refuses work it
cannot take, at once and with `Retry-After`; and the database driver and
outgoing calls never wait longer than they are allowed to.

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
cut off. WebSocket connections never get one; their limits are their own.
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
- **Ledgers and caches** use `deadlines.within`: an immudb call becomes
  `await deadlines.within(future, self.config.TIMEOUT)` in place of
  `asyncio.wait_for(future, self.config.TIMEOUT)`, letting
  `DeadlineExceeded` through before turning a `TimeoutError` into
  `LedgerUnavailable`; a cache's `call` does the same around its store
  operation. Anything a request awaits is cancelled at the deadline
  regardless; `within` also refuses at once when no time is left.

Choose `REQUEST_TIMEOUT` below the load balancer's idle timeout (60 seconds
on most), so a slow request gets the application's 503 rather than the
balancer's 504; and `STATEMENT_TIMEOUT` no longer than `REQUEST_TIMEOUT`, so
transactions rarely need to set their own. ADR 0048 explains the choices.

## Load shedding

`ConcurrencyLimitMiddleware` lets each process (each event loop) work on at
most `MAX_CONCURRENT_REQUESTS` HTTP requests at once (1000 unless set;
`None` turns it off). A request past the cap waits for a slot, first come
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
  replica is not taken for a dead one.
- A request holds its slot until its response starts, or until it ends:
  a streamed response gives its slot back as it starts. A request that is
  cancelled, while it waits or while it runs (its deadline passed, or the
  server cancelled it when the client went away), gives its slot or its
  place back.
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
locks, which transaction pooling allows.
Point `MIGRATION_URL` at PostgreSQL directly: migrations may take longer,
and need the schema owner.

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
appended to `base_url`; a URL naming a host must keep to its origin, so a
path built from someone's input cannot send the call elsewhere.

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
  against the host. Opening is logged once.
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
A guarded client ignores proxy settings from the environment, asks for
uncompressed answers, and sends no request id or trace context unless
`propagate=True`. A refused URL raises `UnsafeURL`, a 422 `unsafe_url`
when the request does not catch it.

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
