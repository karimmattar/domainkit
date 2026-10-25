# 0045. Own background tasks per event loop, started in clean contexts

- Status: Accepted
- Date: 2026-10-20

## Context

The framework started background tasks in three places with
`asyncio.ensure_future`: the shared `get_or_set` computation, the coalesced
readiness round, and the background publisher. Each copied the context of
whichever request came first, so the task carried that request's id,
actor, span, and `Idempotency-Key` operation for as long as it ran: for the
publisher, the life of the loop. Logs and traces named the wrong request,
and a unit of work committed by such a task could mark a key it had nothing
to do with. Nothing kept the tasks, logged their failures, or stopped them
in an order; a dead publisher went unnoticed.

Options considered:

- **`asyncio.TaskGroup` per component.** Structured, but a group belongs to
  one awaiting coroutine. These tasks outlive the request that starts them
  and are shared by later ones, so there is no coroutine to hold the group.
- **A module-level set of tasks** (the pattern the asyncio documentation
  shows). Tests and management commands run several loops in one process;
  one set mixes their tasks, and a task left pending on a closed loop keeps
  that loop alive.
- **One owner per loop, and one way to start a task.**

## Decision

- `domainkit.tasks.spawn(coro, name=...)` is how the framework starts a
  background task. It runs the task in a new `contextvars.Context()`,
  keeps a strong reference until the task ends, logs an unexpected
  exception once with the name (`tasks_failed_total`), and records the task
  as owned by the running loop. `log_errors=False` is for tasks whose
  callers await them and handle their errors.
- Only a frozen clock (`freeze_time`) is carried into the new context: it
  belongs to a test, not a request (ADR 0037).
- Work that must keep running uses `supervise(factory, name=...)`: each run
  is a new task in a clean context, and a crashed run is logged, counted
  (`tasks_restarted_total`), and restarted after a doubling, jittered
  backoff bounded by `max_backoff`. A run that returns ends it; cancelling
  the supervisor stops it.
- Owned tasks are kept per loop, in a `WeakKeyDictionary` keyed by the
  loop whose values hold no reference to it. When a new loop starts its
  first task, the entries of closed loops are dropped, so a loop closed
  with tasks pending is not kept alive.
- The lifecycle's `tasks` phase cancels the running loop's owned tasks and
  waits for them within its share of the shutdown budget (ADR 0046).
- `@cache_page`, whose keys hold the user and language, runs its endpoint
  in a copy of the caller's context, inside the clean computation. Other
  `get_or_set` factories see no request.

## Consequences

- Background work names no request it does not serve, and cannot touch a
  request's idempotency key or span.
- A cached factory that read `current_request()` or the active language
  now sees none; it must take what it needs as arguments and make it part
  of the key. This is called out in the changelog.
- Every framework task can be found (`owned()`), is stopped at shutdown,
  and fails loudly once. Projects can start theirs the same way.
- Task names are metric labels, so they must be fixed strings.

## Addendum (2026-10-25): a caller's deadline, and work left behind

An audit of 0.9.0 found that a shared cache computation ran without the
deadline of the request it served, and kept using that request's session
after the request was cut off (CONC-02); and that a relay's give-back and
the closes a shutdown phase gave up on were plain tasks, so the `tasks`
phase did not wait for them and the `resources` phase closed the database
under them (CONC-05).

- `spawn(..., deadline=...)` carries a deadline into the clean context,
  besides the frozen clock: the task's transactions and outgoing calls are
  bounded by it, and no timer of the task's own enforces it. Cache
  computations that borrow their caller's resources pass that caller's
  (ADR 0071).
- `adopt(task)` owns a task started some other way. A close cut off at the
  end of its phase (`lifecycle`, `close_each`, `close_in_turn`) is
  adopted, so the `tasks` phase waits for it to end.
- A relay's give-back runs as an owned task (`domainkit.messaging.settle`)
  that keeps its work running through the `tasks` phase's cancellation,
  bounded by `RELEASE_TIMEOUT`, so the phase waits for the leases to go
  back.
