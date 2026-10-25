# 0071. Bind a shared cache computation to the caller that starts it

- Status: Accepted
- Date: 2026-10-25

## Context

`get_or_set` (and so `@cached` and `@cache_page`) shares one computation of
a key among concurrent callers. Since ADR 0045 the computation is an owned
task in a clean context, and it went on after the caller that started it
was cancelled, for the callers still waiting.

That computation runs the starting caller's factory, and factories use
what their caller holds. `@cache_page` passes the endpoint the
dependencies FastAPI resolved for the request, a unit of work among them;
a plain `get_or_set` factory often closes over the caller's session. When
the caller was cut off (its deadline passed, its client went away), it
closed its unit of work on the way out while the computation still ran.
SQLAlchemy lets a closed session open a new transaction, so the factory's
next statement checked out a connection that nothing closed again: one
pooled connection lost per request cut off during a miss. Under a slow
database, deadlines fire more, each firing lost a connection, and the
pool ran dry (audit CONC-02). The computation also ran with no deadline:
its clean context had none, so `statement_timeout` stayed at
`STATEMENT_TIMEOUT`.

Options considered:

- **Resolve `@cache_page`'s dependencies again inside the computation.**
  The computation would own what it uses. It needs FastAPI's internal
  `solve_dependencies`, whose signature changes between releases, runs
  every dependency (rules, throttles) twice, and leaves plain `get_or_set`
  factories as they were.
- **Count the waiters; cancel the computation when none is left.** Fixes
  the case of one caller. With two, the computation goes on with the
  first caller's session after the first caller closed it, and under the
  first caller's deadline, which has passed: the second caller gets a
  statement timeout it did not cause.
- **Bind the computation to the caller that starts it (its owner).** The
  owner's cancellation cancels the computation, and waits for it to stop
  before going on; the callers still waiting share a new computation,
  started by one of them with its own factory.

## Decision

- The caller that starts a computation owns it. The computation runs the
  owner's factory under the owner's deadline (`tasks.spawn(...,
  deadline=...)` carries `deadlines.current_deadline()` into its clean
  context; no timer of its own enforces it: the owner's does, through the
  owner).
- When the owner is cancelled while the computation runs, the computation
  is marked abandoned, no longer shared, and cancelled, and the owner
  waits for it to end before its own cancellation goes on. A second
  cancellation of the owner stops that wait.
- Callers still waiting for an abandoned computation share a new one,
  started by the first of them to resume. A caller that did not start the
  computation leaves it running when it stops waiting.
- A computation stopped by `close()` or by shutdown (already being
  cancelled when its owner is) is not abandoned: the owner waits for it,
  and the callers still waiting compute their own value, as since 0.6.
- The contract for factories, in `docs/cache.md`: a factory may use what
  its caller holds for the length of the call, and must not hand it to
  anything that outlives the call.

## Consequences

- No statement runs on a session after its caller closed it, and a miss
  cut off by a deadline returns its connection
  (`test_a_cancelled_miss_leaves_no_connection_checked_out`, and the
  `@cache_page` test with a unit of work, on SQLite and PostgreSQL).
- A computation's transactions and outgoing calls keep to the deadline of
  the request it runs for.
- Work is thrown away when an owner is cancelled while others wait: one of
  them computes again, under its own deadline. Owners are cancelled mostly
  by their deadline, which the computation now keeps to, so it would have
  failed for the others anyway.
- A cancelled owner returns only once its factory has stopped, which takes
  as long as the factory's own cancellation (a statement the driver
  cancels on the server).
