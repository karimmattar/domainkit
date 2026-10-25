# 0071. Detach shared cache computations; bind those that borrow to their owner

- Status: Accepted
- Date: 2026-10-25

## Context

`get_or_set` (and so `@cached` and `@cache_page`) shares one computation of
a key among concurrent callers. Since ADR 0045 the computation is an owned
task in a clean context. In 0.9.0 it went on after the caller that started
it was cancelled, for the callers still waiting, and with no deadline.

That was wrong for factories that use what their caller holds.
`@cache_page` passes the endpoint the dependencies FastAPI resolved for
the request, a unit of work among them; a `get_or_set` factory may close
over the caller's session. A request cut off during a miss closed its unit
of work on the way out while the computation still ran; SQLAlchemy lets a
closed session open a new transaction, so the factory's next statement
checked out a connection that nothing closed again (audit CONC-02).

The first fix bound every computation to its owner: cancelled with it,
under its deadline. A security review of it found two regressions, both
reproduced:

- **One caller's deadline failed the others** (SEC-r3-1). Calls inside
  the factory bounded by the owner's deadline (`domainkit.http.client`,
  `deadlines.within`) start timers that fire in the same loop turn as the
  owner's own; the computation then ended with `DeadlineExceeded`, and
  every waiter with seconds left got it, a 503 `deadline_exceeded`. The
  owner also took the factory's own cancellation for shutdown's, through
  `task.cancelling()`, and waited for the factory to finish (SEC-r3-4).
- **A slow factory never filled the cache** (SEC-r3-2). Each computation
  died with its request, so a factory slower than one request's deadline
  ran, and was thrown away, once per request, and the route answered 503
  for ever. In 0.9.0 the first computation finished after its 503 and the
  cache healed the route.

The two failure modes pull apart. A computation that outlives its caller
is wrong when it uses the caller's resources; one that dies with its
caller is wrong for everyone else when it does not. Whether a factory
borrows cannot be seen from a closure in general, but the framework knows
it where it creates the borrowing: a `@cache_page` endpoint borrows
exactly when one of its parameters is a dependency.

Options considered:

- **Bind every computation to its owner** (the first fix): leaks nothing,
  but fails correct code, the common case, as above.
- **Detach every computation** (0.9.0): heals and isolates callers, but
  `@cache_page` with a unit of work leaks a connection per cut-off request.
- **Detach by default; bind where the factory borrows**, detected for
  `@cache_page`, declared with `borrows=True` for `get_or_set`.

## Decision

- **A computation is detached unless its factory borrows.** It runs in a
  clean context, under a deadline of its own, the cache's
  `COMPUTE_TIMEOUT` (at most an hour; `None`, unbounded, by default). No
  caller's deadline reaches it: callers that stop waiting stop waiting,
  and it goes on, stores its value, and later callers are served. When it
  runs out, every caller waiting gets the same `TimeoutError`.
  The default stays unbounded in 0.9.1, as in 0.9.0: a detached
  computation holds none of its caller's resources, so running unbounded
  is not the CONC-02 bug, and a bounded default would fail, after a patch
  upgrade, any computation that takes longer.
- **A factory that uses what its caller holds borrows**: `@cache_page`
  passes `borrows=True` when the endpoint takes a dependency (`Depends`,
  `Security`, also through `Annotated`); `get_or_set(..., borrows=True)`
  says so for other factories. A computation that borrows is its owner's:
  - it runs under the owner's deadline;
  - when the owner is cancelled, it is marked abandoned, no longer shared,
    and cancelled, and the owner waits for it to stop before its own
    cancellation goes on (a second cancellation stops the wait);
  - a failure that is the owner's (a `TimeoutError`, or any error once the
    owner's deadline has passed, noted by the computation as it fails)
    reaches only the owner. Callers still waiting share a new computation,
    started by one of them with its own factory, under its own deadline.
    Other errors reach every waiting caller.
- **Shutdown and `close()` say so.** They mark the computations they stop
  (`Cache.stop_computations`, which the lifecycle's `tasks` phase calls
  through `caches.stop_computations()` before cancelling the loop's
  tasks); callers still waiting then compute their own value, as since
  0.6. Nothing is inferred from a task's `cancelling()`.
- Single flight holds throughout: one computation per key at a time; a
  new one starts only when one is abandoned or failed for its owner.

The default is detached because its failure needs code that breaks the
documented contract (a factory closing over its caller's session without
`borrows=True`), while binding's failures hit correct code and every
caller of a key. The case the framework creates itself, `@cache_page`
with dependencies, is detected.

## Consequences

- A request cut off during a `@cache_page` miss whose endpoint takes a
  unit of work returns its connection, on SQLite and PostgreSQL
  (`test_a_request_cut_off_during_a_miss_returns_its_connection`), as does
  a cancelled `get_or_set(..., borrows=True)`.
- One short request never answers 503 for those waiting with time left,
  and a slow upstream heals a cached route (the SEC-r3 tests).
- A detached computation is unbounded unless the project sets
  `COMPUTE_TIMEOUT`, as in 0.9.0. The two defaults were tried on PoCs:
  with a 30-second default, a healthy 31-second computation failed with
  `TimeoutError` at 30.0 seconds and never filled the cache, bringing
  SEC-r3-2 back for any factory over the bound; with `None`, one hung
  computation held its key for good (0.9.0's behaviour), and setting
  `COMPUTE_TIMEOUT = 30` healed the key at 30.4 seconds. The patch release
  keeps `None`: no regression from 0.9.0, and the hang is fixed by opting
  in. `check` warns where deployed for each cache without it, and the
  `init` template sets it. 0.10 plans a bounded default.
- A `@cache_page` endpoint with dependencies fills the cache only within
  its requests' deadlines: when that is too short, open a unit of work in
  the endpoint instead of depending on one.
- A factory that uses its caller's resources without saying so still
  leaks as in 0.9.0; `docs/cache.md` states the contract.
