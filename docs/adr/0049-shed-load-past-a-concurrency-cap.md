# 0049. Shed load in the application: a cap per process, a short queue, and a fast 503

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
- The cap is per process: a host running eight workers admits eight
  times the cap. Spreading load across processes stays the balancer's job.
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
