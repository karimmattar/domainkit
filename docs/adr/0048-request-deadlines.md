# 0048. Give every request a deadline, answer 503 when it passes, and pass it on

- Status: Accepted; amended 2026-10-25 and 2026-10-26 (at the end)
- Date: 2026-10-20

## Context

Nothing bounded how long a request ran. uvicorn has no request timeout; a
load balancer gives up after its idle timeout (60 seconds on most) and
answers 504 itself, while the application keeps working for a client that
is gone. `STATEMENT_TIMEOUT` bounds one statement, not a request of many,
and asyncpg waited 60 seconds to connect, which `POOL_TIMEOUT` does not
cover. Under load, requests that should have been abandoned keep their
connections and their memory, and make the next ones slower.

The questions were what a deadline covers, how it ends the work, what the
client is told, how routes change it, and how it reaches what the request
waits on.

- **Status code.** 504 says a gateway's upstream was too slow: load
  balancers send it themselves, and dashboards count it against the
  gateway. 408 says the client was too slow to send. 503 says the server
  cannot finish now, and RFC 9110 defines `Retry-After` for it, which
  clients and retry libraries honour; domainkit already answers a busy
  database that way (`database_busy`).
- **What it covers.** The whole response would cut off streamed exports,
  Server-Sent Events, and the background tasks Starlette runs after the
  response. The time until the response starts covers the work a request
  does before it can answer, which is what piles up under load.
- **Per route.** A path list in settings, or matching routes in the
  middleware, duplicates the router and costs every request. A FastAPI
  dependency is where a route already declares what it needs, but runs
  after the body is read.
- **Nesting.** A `timeout` block inside a request has a deadline of its
  own. If each block runs its own timer, two timers set for the same
  moment race, and which block raises depends on the event loop.
- **Propagation.** Cancelling the request's task stops every await, but
  not the work a server is doing for it. PostgreSQL can bound a
  transaction with `SET LOCAL statement_timeout`, at one more round trip;
  behind PgBouncer's transaction pooling no session setting survives, so
  every timeout must be set that way, and asyncpg's statement cache and
  numbered statement names break.

## Decision

- `domainkit.deadlines` holds the deadline of the work being done in a
  context variable, read with `remaining()`, and bounds work with
  `within(awaitable, seconds)` and `async with timeout(seconds)`: the
  smaller of their seconds and the time left, which becomes the deadline
  inside. Time is read from `domainkit.clock`; the cancelling timers are
  the event loop's.
- `DeadlineMiddleware`, listed right after `RequestContextMiddleware` and
  in `DEFAULT_MIDDLEWARE`, gives each HTTP request `REQUEST_TIMEOUT`
  seconds (30, below common balancer timeouts and equal to
  `STATEMENT_TIMEOUT`) until its response starts. When they pass, the
  request's task is cancelled and answered 503 `deadline_exceeded` with
  `Retry-After: 1`, logged once. The deadline is lifted when the response
  starts and when the request ends; WebSockets get none.
- A route changes its deadline with the `RequestTimeout(seconds)`
  dependency, counted from arrival; `None` removes it. The body is read
  under `REQUEST_TIMEOUT`.
- The block that set a deadline enforces it in its task: an inner block
  bounded by the same deadline starts no timer, so the cancellation always
  surfaces where the deadline was set. A task started inside shares the
  deadline but not the timer, so its blocks time themselves and raise
  `DeadlineExceeded`. A block entered with no time left raises
  `DeadlineExceeded` without running.
- PostgreSQL transactions set `statement_timeout` to the time left when
  they begin (an engine `begin` event, `set_config(..., true)` with bound
  values, so each connection prepares it once), unless the deadline is
  within `DEADLINE_SLACK` (1 second) of `STATEMENT_TIMEOUT`: young
  requests, most of them, skip the round trip, and the server's own
  timeout then ends their statements at most that late. A transaction
  begun after the deadline passed keeps `STATEMENT_TIMEOUT`, so cleanup
  runs. The statement is marked internal, and `assert_num_queries` skips
  it.
- asyncpg gets `timeout=CONNECT_TIMEOUT` (10) and
  `command_timeout=COMMAND_TIMEOUT`, by default `STATEMENT_TIMEOUT` plus 5
  seconds, so the server ends a slow statement first with an error that
  keeps the connection; psycopg gets `connect_timeout`. `CONNECT_ARGS`
  may still set these, deprecated, until 0.9.
- `PGBOUNCER = True` turns off asyncpg's and SQLAlchemy's statement
  caches, names each prepared statement uniquely, and sets every timeout
  per transaction instead of at connect time. `check` warns in
  deployments about a database on port 6432, or with its statement cache
  turned off by hand, without it.
- `domainkit.http.client` bounds its calls by the deadline (ADR 0050).
  Ledgers and caches use `within`; the immudb adapter is wired when the
  branches merge.

## Consequences

- A request can no longer outlive its usefulness by much: past the
  deadline its connection, its slot (ADR 0049), and its memory are given
  back, and the client hears 503 in time to retry elsewhere.
- Cancellation unwinds a request as an exception does: units of work roll
  back and idempotency keys that committed nothing are released, which the
  tests check with both in the path. That relies on cleanup running after
  one cancellation, as asyncio's is; middleware that cancels repeatedly
  (anyio's cancel scopes, `BaseHTTPMiddleware`) needs that cleanup
  shielded.
- A statement cut off by the deadline costs its connection: SQLAlchemy
  invalidates a connection whose statement was cancelled, and the pool
  opens another. The server-side timeout counts from each statement's
  start, so the client's cancellation usually comes first; it still makes
  PostgreSQL stop the work if the cancel request is lost.
- Old requests, and every transaction behind PgBouncer, pay one round
  trip for their timeouts. Setting `STATEMENT_TIMEOUT` no longer than
  `REQUEST_TIMEOUT` keeps that rare.
- Code that swallows `CancelledError` keeps running; its answer is sent as
  it is. A frozen clock stops `remaining()`, not the timers.
- Long-lived endpoints that work before answering must say so with
  `RequestTimeout`; streams that answer at once need nothing.

## Addendum (2026-10-25): worker tasks and cache computations

The worker bounded a message with `asyncio.timeout`, so nothing a task did
saw the time it had left, as this ADR and the `deadlines` module said it
would (audit CONC-03). It now runs each message in `deadlines.timeout` of
its timeout: a task's transactions lower `statement_timeout` to the time
left and its outgoing calls keep to it, and a timeout still raises
`TimeoutError`, retried as before. A shared cache computation works under
a deadline of its own, the cache's `COMPUTE_TIMEOUT`, so no caller's
deadline fails the others; one that borrows its caller's resources works
under that caller's deadline, whose failures reach only that caller
(ADR 0071).

## Amendment (2026-10-26, 0.10): a statement the deadline cuts off keeps its connection

The consequence above, measured: ten requests with a 1 s deadline whose
second statement would take 2 s opened ten connections and discarded ten,
in a transaction and with `Reads.STATEMENTS` alike (CONC-08 of the 0.9.0
audit). SQLAlchemy discards a connection when an exception that ends a
program reaches it, as a cancellation does. asyncpg cancels the statement
on the server when its caller is cancelled, and makes the connection's
next use (the rollback that follows) wait until the server confirms; a
server that does not confirm in time fails that use, and the connection
is discarded then. A `handle_error` listener on asyncpg engines now keeps
a connection asyncpg still holds open: the same ten requests open one
connection and discard none, and the server runs nothing after them.

- Rejected: lowering `statement_timeout` before each statement to the time
  left, so the server ends it first. The server's timeout counts from
  each statement's start; bounding the second statement of a transaction
  by the deadline takes a `SET LOCAL` round trip before it, and before
  nearly every statement of a request.
- psycopg's connections are judged as before.
