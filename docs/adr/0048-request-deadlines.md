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

## Amendment (2026-10-26, 0.10): a statement the deadline cuts off still costs its connection

The consequence above, measured: ten requests with a 1 s deadline whose
second statement would take 2 s opened ten connections and discarded ten,
in a transaction and with `Reads.STATEMENTS` alike (CONC-08 of the 0.9.0
audit). A `handle_error` listener that kept a cancelled asyncpg connection
open brought that to one connection and none discarded, and was reverted
before release: the 0.10 security review (SEC-10b-1) showed a deadline
that fires while `BEGIN`'s answer is in flight leaves the kept connection
inside a server transaction SQLAlchemy never recorded. The next request's
`COMMIT` then only released a savepoint in it, so an acknowledged write
was lost and its locks held; under an anyio cancel scope, which cancels the
rollback again, the next request failed instead. Keeping a connection
safely needs proof that driver and SQLAlchemy agree on its transaction
state, which asyncpg does not expose publicly: a reconnect per request cut
off under a slow database costs less than a lost write. The decision of
this record stands; keeping such connections waits for 0.11.

- Rejected: lowering `statement_timeout` before each statement to the time
  left, so the server ends it first. The server's timeout counts from
  each statement's start; bounding the second statement of a transaction
  by the deadline takes a `SET LOCAL` round trip before it, and before
  nearly every statement of a request.
- `test_db_reads.py` keeps the reviewers' two cases: a write after a
  deadline cut during `BEGIN` must commit, and the request after an anyio
  scope cut must work.

## Amendment (2026-10-26, 0.11): a statement the deadline cuts off keeps its connection once it has settled

The 0.10 amendment left CONC-08 open until something proved that asyncpg
and SQLAlchemy agree with the server on a cancelled connection's
transaction. What each knows, read from asyncpg 0.32 and SQLAlchemy 2.1:

- asyncpg begins every exchange by waiting for a cancelled one to end: the
  cancel request sent, and the server's answer to the cancelled statement
  read up to its ReadyForQuery. If that takes longer than the exchange's
  timeout, it closes the connection. The status byte of the last
  ReadyForQuery is public (`Connection.is_in_transaction()`); nothing
  public waits for the cancelled exchange alone.
- asyncpg tracks the transaction it began (`Connection._top_xact`) from
  before `BEGIN` is sent, and keeps it when `BEGIN` is cancelled.
  SQLAlchemy's adapter tracks it (`_transaction`) only once `BEGIN` has
  answered. After a cut during `BEGIN`, asyncpg tracked a transaction
  SQLAlchemy did not, and its next `BEGIN` became a `SAVEPOINT`: SEC-10b-1.
  Neither tracker is public. asyncpg's public `Connection.reset()` clears
  its own, but also resets the whole session, and reports the transaction
  to the event loop's exception handler, as an error, each time.
- SQLAlchemy discards a connection whose statement was cancelled. A kept
  one leaves the cut transaction active for code that goes on.

`test_db_cut_off.py` holds the cases: a write after a cut `BEGIN` (under
`asyncio` and an anyio scope), a write after a statement cut in a
transaction (both), the transaction refusing to go on, a read on its own
(`Reads.STATEMENTS`) whose session must be reset, a settle that takes too
long, and the cost of ten cut requests. Each checks the write on another
connection, and that no lock or open transaction is left. Ten requests
with a 1 s deadline whose second statement takes 2 s, on PostgreSQL 18:

| Option | Opened, discarded | The SEC-10b-1 cases |
| --- | --- | --- |
| Discard it (0.10) | 10, 10 | Pass; with `POOL_PRE_PING` off, the request after an anyio scope's cut fails ("connection is closed") |
| Keep it as it is (0.10's reverted listener) | 0, 0 | Fail: three acknowledged writes after a cut `BEGIN` lost (one under an anyio scope), a transaction left open after an anyio scope's cut, a cut transaction that goes on to the server's errors, not `PendingRollbackError` |
| Settle it where it was cut, then keep it | 0, 0 | Pass |

Decision:

- With asyncpg outside PgBouncer, a `handle_error` listener settles a
  connection whose statement a cancellation cut off, in a task of its own
  that the cancellation does not reach, while the cut-off work waits, for
  at most `SETTLE_TIMEOUT` (2 seconds, as long as SQLAlchemy gives a
  connection to close gracefully). It sends `ROLLBACK` where asyncpg or
  SQLAlchemy began a transaction, and the ping's prepared `;` otherwise;
  asyncpg sends either once the cancelled exchange has ended. The server
  must then report no transaction. Both trackers are cleared, SQLAlchemy's
  transaction is deactivated (code that goes on gets `PendingRollbackError`
  until it rolls back, as with a discarded connection), and the
  connection is kept: none opened, none discarded, in a transaction and
  with `Reads.STATEMENTS` alike, and the 503 comes as soon after the
  deadline as before (a median of 1,018 to 1,029 ms against 1,025 to
  1,029 ms). Settling a statement cut in a transaction took a median of
  13 ms, nearly all of it the cancelled exchange, which asyncpg waits for
  before any next statement and SQLAlchemy's close waited for too:
  `ROLLBACK` alone takes 0.1 ms, the ping 0.24 ms. Opening the connection
  that replaced a discarded one took 20 ms, and a server process.
- Anything else discards it, as before: an error, the time running out, a
  transaction begun in SQL text, which nothing tracks, or a second
  cancellation. An anyio cancel scope cancels every wait until it exits,
  so it cannot wait for the settling; settling on after the cut-off task
  moved on would share the connection with that task's shielded cleanup.
- Settling reads and clears state asyncpg and SQLAlchemy keep privately,
  so it runs only on the releases it was proven on (`_PROVEN`: asyncpg
  0.32, SQLAlchemy 2.1); with others, a cut-off connection is discarded,
  and a test fails until the cases pass on them and `_PROVEN` admits them.
- PgBouncer, psycopg and SQLite discard it, as before: nothing proves them.
- An asyncpg pool that does not ping replaces, as it hands it out, a
  connection asyncpg knows is closed: under an anyio scope, SQLAlchemy's
  close of a cut connection is cancelled too, and the closed connection
  went back to the pool.

Rejected:

- Settling at the connection's next checkout, so the cut-off work need
  not wait: its transaction's locks and snapshot would stay held while
  the connection waits in the pool, up to `IDLE_IN_TRANSACTION_TIMEOUT`.
  Settled where it was cut, the locks go before the 503 is sent.
- `Connection.reset()` for asyncpg's tracker: it resets the session too
  (`RESET ALL`, `UNLISTEN *`, advisory locks), which a pooled connection
  of SQLAlchemy's never had, takes two round trips (0.5 ms), and called
  the event loop's exception handler for 30 cut transactions in 30.
- asyncpg's private wait for the cancelled exchange in place of the ping:
  it saves one round trip, only for statements outside a transaction, and
  ties the proof to asyncpg's protocol rather than to what the server
  answers.
