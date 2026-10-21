# 0049. Shed load in the application: a cap per event loop, a short queue, and a fast 503

- Status: Accepted
- Date: 2026-10-20

## Context

A process took every request it was given. Past what it could finish,
requests queued for database connections for up to `POOL_TIMEOUT` each,
latency grew for everyone, and memory with it. The only bound was
uvicorn's `--limit-concurrency`, which answers a plain 503 without
`Retry-After`, counts WebSockets, and refuses health and readiness probes
too, so a busy replica looks dead and is taken out of rotation, which
moves its load onto the others.

The questions were where to count, what to do with requests past the cap,
what to answer, which requests are never refused, and whether to adapt
the cap to measured delay.

- **Answer.** 429 tells one client it sent too much; overload is the
  server's state, which 503 with `Retry-After` describes.
- **Queue.** Refusing every request past the cap at once turns short
  bursts into errors; an unbounded queue is the problem itself. A queue
  bounded in length and in waiting time absorbs bursts and still answers
  fast.
- **What holds a slot.** Counting a request until it ends counts a
  streamed export or an event stream for as long as it stays open. The
  work a cap protects is done before the response starts.
- **Adaptive shedding** (CoDel's queue delay target, adaptive LIFO) trades
  more settings and timing-dependent behaviour for gains under sustained
  overload, where the bounded queue and the request deadline (ADR 0048)
  already refuse fast.

## Decision

- `ConcurrencyLimitMiddleware`, listed after `DeadlineMiddleware` (the wait
  counts against the deadline) and before authentication (a refused
  request costs no database lookup), and in `DEFAULT_MIDDLEWARE`, lets each
  event loop work on at most `MAX_CONCURRENT_REQUESTS` HTTP requests.
- The default is generous, 1000: a backstop that bounds a pile-up without
  ever refusing a service working normally. Projects set it from what
  their pool can serve; `None` turns it off.
- Past the cap, a request waits first come first served for up to
  `REQUEST_QUEUE_TIMEOUT` (1 second, at most 60), behind at most
  `MAX_QUEUED_REQUESTS` others (100). Otherwise it is answered at once 503
  `overloaded` with `Retry-After: 1`.
- A request holds its slot until its response starts or it ends. A slot
  freed goes to the longest waiter; a waiter cancelled after being handed
  one passes it on, and one cancelled while waiting leaves the queue.
- `CONCURRENCY_EXEMPT_PATHS` (`/health`, `/ready`, `/metrics`) are never
  counted or refused; WebSockets are not counted.
- Shedding is logged at most every 10 seconds with a count, and measured:
  `http_requests_admitted`, `http_requests_queued`, and
  `http_requests_shed_total` by reason.
- No adaptive shedding: the cap, the queue, and the deadline are fixed
  numbers an operator can reason about and tests can check exactly. It
  can be added behind the same middleware later.

## Consequences

- Under overload a process answers quickly: either it works on a request,
  or it says within a second that it cannot, and its probes keep passing.
- The cap is per event loop, since a slot's waiters belong to one loop. A
  process serves on one loop (uvicorn, Granian's asyncio workers), so a
  host running eight workers admits eight times the cap; a server that
  runs several loops in one process (free-threaded Python, a loop per
  thread) admits the cap on each. Spreading load across processes stays
  the balancer's job.
- Streams and requests whose response has started are not counted, so the
  cap bounds the work before answering, not open connections; uvicorn's
  `--limit-concurrency` stays the backstop for those, set above the cap
  plus the queue.
- A constant `Retry-After` can bring refused clients back together;
  clients should add jitter, and a later release may scale it with the
  queue.
- The default only protects against pile-ups; a project whose database
  pool holds 15 connections is better served by a cap near a few times
  that.

## Addendum (2026-10-21): time spent waiting on the client is not counted

A security review found that a request held its slot while the
application waited in `receive()` for the body. A few connections that
send their headers and never their body held every slot until
`REQUEST_TIMEOUT`, and every other request was answered 503, without any
credentials.

Two ways out were weighed:

- **Take the slot only once the body has arrived**, read whole by the
  middleware first, up to `MAX_REQUEST_BODY_SIZE`. Simple, and no slot
  changes hands mid-request; but the body is held in memory before the
  request is counted, so a project that raises the limit for uploads
  would buffer each upload whole, where Starlette spools forms to disk,
  and the server's own flow control would be bypassed.
- **Lend the slot while the application waits for a body chunk.** A
  receive that has to wait (it does not return at once) gives the slot to
  the next request; when the chunk arrives, the request takes a slot back
  through the queue, as a new request would, and is refused 503
  `overloaded` (`Overloaded`) when none frees in time. Streaming uploads
  are unchanged, and a request works only while it holds a slot.

The second was chosen. Details:

- Lending stops once the body is in or the response has started, so a
  task waiting for a disconnect never lends the slot of a request at
  work. A `receive()` that is being cancelled (a deadline, or Starlette's
  `is_disconnected()`, which cancels a receive that would wait) lends
  nothing. A wait that ends in an error takes nothing back: the request
  is ending.
- `REQUEST_BODY_TIMEOUT` (10 seconds, at most an hour) bounds the time the
  application waits for the body, all chunks together:
  `RequestBodyLimitMiddleware`, which already bounded its size, answers
  408 `request_timeout` and closes the connection. So a slow body costs a
  connection and some memory for at most that long, never a slot.
- A buffering proxy (nginx's `proxy_request_buffering on`, the default)
  stays the first line: it reads slow bodies before the application sees
  them.

Paths in `CONCURRENCY_EXEMPT_PATHS` are compared without the root path
the application is mounted at (`--root-path`), so the probes stay exempt
behind one.
