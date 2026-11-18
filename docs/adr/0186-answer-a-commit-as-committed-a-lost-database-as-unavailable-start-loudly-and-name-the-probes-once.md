# 0186. Answer a commit as committed, a lost database as unavailable, start loudly, and name the probes once

- Status: Accepted; amended 2026-11-18 (at the end)
- Date: 2026-11-18
- Amends: [0048](0048-request-deadlines.md), [0078](0078-deliver-events-in-process-after-commit-or-durably-through-the-outbox.md)

## Context

The 2.8 audit found seven defects at the edges of a commit and at
startup. Each has a decision that is costly to reverse: what a deadline
means once work committed, a problem code, two check codes, a setting,
and the server options `runserver` and `serve` pass.

- **A committed write answered 503** (A28-REL-01). `UnitOfWork.commit`
  ran the `on_committed` hooks and the in-process handlers inside the
  request, under its deadline. A handler still running at the deadline
  was cancelled, the handlers after it were skipped with nothing logged
  (`CancelledError` is not an `Exception`, so `EventBus.publish` did not
  catch it), and the request was answered `503 deadline_exceeded` with
  `Retry-After: 1`. A client that retried without an `Idempotency-Key`
  applied the change twice. ADR 0078 promised that "a handler that raises
  or overruns is logged, reported, and skipped, and the next one runs. A
  handler never undoes the commit." The worker had the same flaw under
  `TASK_TIMEOUT`: a task whose handlers ran past it was recorded failed,
  and retried, so a task without `@inbox` committed twice.
- **A database out of reach was a bug** (A28-REL-03). A refused
  connection, one lost mid-request, and a server shutting down, starting
  up or failing over all answered `500 internal_error`, with a traceback
  and an error report per request, and no `Retry-After`.
- **An upload deleted though a commit kept it** (A28-REL-04). The upload
  route deleted its file when the unit of work's commit count had not
  moved. A `COMMIT` whose answer was lost, or one the deadline cut off
  after the server applied it, does not move the count: the committed row
  then named a file that was gone. The line that said a file could not
  be deleted did not name it (A28-REL-07).
- **A contrib app's middleware without the app** (A28-DX-04) passed
  `check`. uvicorn's default lifespan mode, `auto`, logged the lifespan's
  exception at `INFO` as "lifespan unsupported", reported "Application
  startup complete", skipped the domains' startup, and served every
  request a 500.
- **Unapplied migrations went unsaid** (A28-DX-08): `check` and
  `runserver` said nothing, and the first request was a raw SQL error.
- **Five lists named the probes, with three defaults** (A28-ARCH-01).
  `TENANCY.EXEMPT_PATHS` exempted `/health` alone, so with tenancy on,
  `/ready` and `/metrics` answered `400 tenant_not_named`: no pod became
  ready, and the scrape went dark. `ACCESS_LOG_QUIET_PATHS` left out
  `/metrics`.

The release is a minor one: nothing a project relies on may break.

## Decision

### What follows a commit is free of the deadline

- `domainkit.deadlines.after_commit()` is a `with` block for what follows
  a commit. The work's deadlines are lifted for the block, so nothing it
  runs is cut off, and it sees no deadline.
  - A deadline made `until_commit` (`deadlines.timeout(seconds,
    until_commit=True)`, `Deadline.until_commit`) stays lifted, and so
    does every block it bounds, in any task. `DeadlineMiddleware` makes
    each request's so: once a request commits, it is answered as
    committed, never 503, as when its response starts. It is lifted from
    whichever task commits: the request's own, or one that only shares
    its deadline (`asyncio.gather`, `create_task`, a task group, or the
    task a `BaseHTTPMiddleware` after `DeadlineMiddleware` serves the
    route in). The request committed either way.
  - A block bounded by such a deadline is lifted with it. With a timer of
    its own (in a task that shares the deadline without enforcing it) or
    without, it inherits `until_commit`, and the deadline keeps it while
    it is open, so a commit in one task lifts the bound of a sibling's
    outgoing call too.
  - The other deadlines the committing task enforces, a worker task's
    `TASK_TIMEOUT` among them, are put back when the block ends, later
    by the time it took: what follows a commit does not count against
    the work's time.
  - Any other deadline, one that another task enforces and no commit
    ends, is left alone.
- `UnitOfWork.commit` runs its `_committed()` hooks and its events'
  in-process handlers in it, right after `_commit()` returns, before any
  `await`: no timer can fire between. Each handler keeps its own
  `EVENT_HANDLER_TIMEOUT`. `enqueue`, which commits outside a unit of
  work, runs its `on_transaction_committed` hooks the same way.
- The domains' after-commit hooks (`on_committed`,
  `on_transaction_committed`) were bounded by the request's deadline,
  which the commit now lifts: each runs within `EVENT_HANDLER_TIMEOUT`,
  as a handler does, and one still running then is cut off, logged, and
  reported as `HandlerTimedOut`. A setting of their own was not added:
  they are follow-ups of the commit, as handlers are, and a hook that
  needs longer belongs in the outbox.
- A commit that fails in doubt marks its error (`failed_in_doubt`, which
  follows the errors raised from it). `DeadlineMiddleware` answers a
  request cut off as it committed `503 deadline_exceeded` without
  `Retry-After`, and with a detail that says the change may have been
  made; so does `database_refusal` for a connection lost then
  (`database_unavailable`). The code stays the same, so clients that
  read it keep working, and nothing invites a retry of a write that may
  have landed. Rejected: a code of its own, which every client would
  have to learn for a rare case.
- When the work itself is cancelled (a shutdown, a task cancelled by its
  owner), `EventBus.publish` logs the handler cut off and those after it,
  and `publish_all` those of the events it did not reach, at `ERROR`, and
  reports each event's as `HandlerCancelled`. The cancellation goes on.
- The worker's code is unchanged: the unit of work's commit holds its
  timeout. `TASK_TIMEOUT` then no longer bounds a run's whole time: what
  follows each commit, up to `EVENT_HANDLER_TIMEOUT` per handler and
  hook, adds to it, and `VISIBILITY_TIMEOUT` must stay above it too.

Options weighed:

- **Clear the deadline for good after any commit**, the audit's first
  option. One rule for requests and tasks, but a task that commits in a
  loop, a purge or an import, would run without `TASK_TIMEOUT` after its
  first commit, and hold a worker's slot if it then hung.
- **Pause the deadline for the follow-up only, everywhere.** It keeps
  every bound, but a request whose deadline lands between its commit and
  its response, in a `session.close()` or the answer's serialization,
  is still answered 503 for a change that was made.
- **Run the follow-up in an owned task, waited for under `shield`**, the
  audit's second option. It needs the cancellation to be swallowed, or
  re-raised after the wait: swallowed, a shutdown's cancellation is lost
  too; re-raised, the request is still answered 503.
- **Answer a request cut off after its commit with another code.** The
  client would learn that its change was made, but not the answer it
  needs (the resource, its `ETag`), and a 503 is still a failure to most
  clients.
- **Lift a request's deadline for good and hold a task's**, chosen. An
  HTTP answer is the only record a client gets of its change; a worker's
  task is retried by contract, and must be idempotent or `@inbox`. gRPC
  says the same of its deadlines: a `DEADLINE_EXCEEDED` for an operation
  that changes state "may be returned even if the operation has completed
  successfully" [1], so a client must not take it as a failure. Go 1.21
  added `context.WithoutCancel` for work that must finish after its
  request is cancelled [2].

### A database out of reach is 503 `database_unavailable`

- `database_refusal` answers `503 database_unavailable`, with
  `Retry-After: 5`, for: a `DBAPIError` SQLAlchemy found disconnected
  (`connection_invalidated`) [3]; SQLSTATE class `08`, and `57P01`,
  `57P02`, `57P03`, `53300` [4]; and an `OSError` raised while SQLAlchemy's
  pool opened a connection (asyncpg raises the socket's error as it is,
  a refused connection, an unknown host, its connect timeout). 5 seconds,
  not `database_busy`'s 1: a restart or a failover takes seconds, and a
  retry every second from every client adds to it [5]. A login refused (a
  wrong password, a database that does not exist) stays a 500, reported:
  it is a mistake in the settings. The connect path is asyncpg's: an
  `OSError` raised under the pool's `__connect`, by the driver or the
  engine's creator, and not by a listener (a project's pool `connect` or
  `checkout` listener that fails with a `PermissionError` of its own
  stays a reported 500). psycopg's connection failures carry no SQLSTATE
  and are no disconnect to SQLAlchemy at connect, so they may stay 500;
  it is not installed here to prove more.
- `RequestContextMiddleware` writes one warning per reason every 10
  seconds (`ThrottledLog`), with a count, and reports none.
- Rejected: replacing asyncpg's `OSError` at connect with a SQLAlchemy
  error, in a `do_connect` listener. A listener that returns the
  connection stops the listeners after it, such as a project's that adds
  a cloud provider's token to each connection, and the framework itself
  catches `OSError` around connections in places. Reading the
  traceback's frames for the pool's `__connect` is done on the error path
  only.

### A commit in doubt keeps its upload

- `UnitOfWork.in_doubt` counts commits that failed without the
  database's refusal: a cancellation, a timeout, a lost connection
  (`connection_invalidated`, SQLSTATE class `08`), any error but a
  domain's refusal or one the server answered with. `_refused(exc)` says
  which; the SQL unit of work adds SQLAlchemy's own errors that come
  before a `COMMIT` is sent, listed one by one (`REFUSED_BEFORE_COMMIT`:
  `StaleDataError`, `FlushError`, `PendingRollbackError`,
  `ResourceClosedError`, its pool's timeout, `DisconnectionError`), a
  `StatementError` that is no driver's (a value it could not bind), and
  every `DBAPIError` with the server's answer outside class `08`. Its
  other errors (`InvalidRequestError`, `MissingGreenlet`) stay in doubt:
  a session's `after_commit` listener of a project's can raise them once
  the `COMMIT` landed.
- `upload_route` keeps the file when the commit is in doubt, and warns:
  `Kept the upload <name> on the <storage> storage: its request failed
  as it committed, and the commit may have kept it`. It deletes it, as
  before, when nothing was sent to commit. The lines that say a file
  could not be deleted, or took too long, name it and its storage
  (A28-REL-07): a name is random, not a secret (ADR 0040).
- A cut during the flush, before the `COMMIT`, counts in doubt too: the
  unit of work cannot tell it from a cut during the `COMMIT` without a
  flush of its own, which every write would pay for. Such a file stays,
  named in the log; a kept file costs storage, a deleted one loses data.
- Rejected for now: deferring the check to `remove_unreferenced`. Asked
  at once, the primary may not have applied a `COMMIT` still in flight,
  and the file would be deleted under it; asked later, it needs the
  outbox, on a database that may be the one away. An orphan sweep
  (`Storage.names`, the audit's E2) is left for a later release.

### Startup fails loudly, and says what is missing

- `runserver` and `serve` give uvicorn `lifespan="on"` [6]: a lifespan
  that raises stops the server, which exits 3, where `auto` served every
  request a 500. Under Granian with `EXIT_ON_FAILED_START`, a lifespan
  that raises before its startup completes signals a failed start too,
  as a startup that says it failed does (ADR 0169).
- `apps.E003`: a `MIDDLEWARE` entry under `domainkit.contrib.<app>`
  whose app is not installed, from `check` and as the application starts
  (refused in staging and production, logged elsewhere, as every
  `application_problems` finding).
- The registry's error names the package to install:
  `No installed domain declares IdempotencySettings: install
  'domainkit.contrib.idempotency' in INSTALLED_DOMAINS.`
- `migrations.W001`, outside staging and production: from `check`, and
  from `runserver` before it serves (on stderr, unless silenced), each
  database that lacks revisions of its target, with how many and which,
  as Django's `runserver` says "You have N unapplied migration(s)" [7].
  A database that cannot be reached, one in memory, and databases the
  test plugin refuses say nothing; a SQLite file not made yet lacks all,
  and asking does not make it. One whose revisions cannot be read (a file
  that is no database, a revision the project does not have) is said to
  be so, never a traceback. Each database gets `CHECK_CONNECT_TIMEOUT`
  (2 seconds) at most, under its `CONNECT_TIMEOUT`, so a host that never
  answers does not hold `check` for 10 seconds per alias; Alembic's INFO
  lines stay out of the output. Not where deployed: a rollout migrates
  in a job of its own, after `check --deploy` has passed.
- The `IdempotencyMiddleware` still reads its settings before it looks
  at the scope's type, as the audit proposed changing: with the lifespan
  on, that is what stops the server at once.

### The probes are named once

- `PROBE_PATHS` (default `/health`, `/ready`, `/metrics`) names the
  probes and the scrape. Each concern exempts them, and adds its own
  list: `RATE_LIMIT_EXEMPT_PATHS`, `CONCURRENCY_EXEMPT_PATHS`,
  `ACCESS_LOG_QUIET_PATHS`, `AUTH.AUTHENTICATION_EXEMPT_PATHS` (exact
  paths), and `TENANCY.EXEMPT_PATHS` (prefixes, still `("/health",)`).
  The four exact lists default to `()`: what they exempt by default is
  the same, and `/metrics` is logged quietly too.
- The paths a list a project set names stay exempt, and the probes are
  added to it. A list that left a probe out on purpose (a
  `RATE_LIMIT_EXEMPT_PATHS` without `/metrics`, to count the scrape) no
  longer does: that probe is exempt from it too. To keep it out, the
  project takes it out of `PROBE_PATHS`, and adds it to each list that
  should still exempt it. `probes.W001` warns of a list that names some
  probes of `PROBE_PATHS`, not all, in every environment. A project that
  moves its probes changes one setting.
- Options weighed: only changing `TENANCY.EXEMPT_PATHS`'s default, the
  audit's short-term fix, leaves the project that followed the tenancy
  page's example (`["/health", "/orgs/"]`) refused, and five lists to
  keep in step. Defaulting each list to `PROBE_PATHS` when unset would
  keep every list as written, but `override_settings` copies every value
  it does not change, so an override of `PROBE_PATHS` would not reach
  the lists: tests and projects would see two behaviours. A flag per
  list to leave the probes out (`include_probes`) adds five settings for
  a need no one reported; a scrape that sends `METRICS_TOKEN` must be
  exempt from authentication anyway, or its token is read as a user's.
- Not done: a check that the health and metrics routes are mounted at
  `PROBE_PATHS`. It would read the routes' endpoints; it is a follow-up.

## Consequences

- A request that commits is answered as committed, however long its
  handlers take. It may run past `REQUEST_TIMEOUT`, holding its slot of
  `MAX_CONCURRENT_REQUESTS` and its memory; the handlers' own timeouts
  bound it. A request that commits and then calls out, or commits
  again, is not cut off: such work bounds its own waits
  (`deadlines.within`, the outgoing client's `timeout`), and a
  transaction begun then keeps `STATEMENT_TIMEOUT`. Its keyed response is
  the real one, stored: an `Idempotency-Key` committed with the change
  cannot be taken over by a retry once its lease runs out.
- A worker's task keeps its `TASK_TIMEOUT` for what it does itself; slow
  handlers after its commit no longer fail it, and add to its run: a
  `VISIBILITY_TIMEOUT` close to `TASK_TIMEOUT` may let another worker
  claim a run still in its handlers.
- A domain's after-commit hook that took longer than
  `EVENT_HANDLER_TIMEOUT` under a long `REQUEST_TIMEOUT` is now cut off.
- `HandlerCancelled` reports appear where cancellations cut handlers
  off, at shutdown above all: the reactions they stand for were lost
  before too, silently.
- During a database outage, operators see `503 database_unavailable`
  and one line per reason every 10 seconds, not a traceback and a report
  per request; alerts on the error reporter or the 500 rate for an
  outage move to `/ready` and the 503 rate.
- A commit in doubt can leave a file no row names; the warning names
  it. Under a slow database, cuts during the flush do too.
- A project whose server served despite a lifespan that raised now fails
  to start, with the error: the fix is the error's. `check` fails on a
  contrib middleware without its app, and staging and production refuse
  to start with one.
- `check` connects to each database outside staging and production. A
  database out of reach costs it up to 2 seconds.
- Readers of `RATE_LIMIT_EXEMPT_PATHS`, `CONCURRENCY_EXEMPT_PATHS`,
  `ACCESS_LOG_QUIET_PATHS`, or `AUTH.AUTHENTICATION_EXEMPT_PATHS` get
  `()` by default, and `PROBE_PATHS` beside them. Successful scrapes of
  `/metrics` are logged at DEBUG.
- Each commit pays for `after_commit()`, and each hook for its timeout:
  a few calls and a timer moved; the commit rungs' budgets were raised
  to it. A request's deadline keeps the blocks it bounds while they are
  open, each leaving as it ends.

## References

1. gRPC, *Status codes and their use in gRPC*, `DEADLINE_EXCEEDED`:
   <https://grpc.io/docs/guides/status-codes/>
2. Go, package `context`, `WithoutCancel`:
   <https://pkg.go.dev/context#WithoutCancel>
3. SQLAlchemy, *Connection Pooling*, *Dealing with Disconnects*:
   <https://docs.sqlalchemy.org/en/20/core/pooling.html#dealing-with-disconnects>
4. PostgreSQL, *Appendix A. PostgreSQL Error Codes*:
   <https://www.postgresql.org/docs/current/errcodes-appendix.html>
5. RFC 9110, *HTTP Semantics*, 15.6.4 *503 Service Unavailable* and
   10.2.3 *Retry-After*: <https://www.rfc-editor.org/rfc/rfc9110>
6. uvicorn, *Settings*, `--lifespan`:
   <https://www.uvicorn.org/settings/>
7. Django, *django-admin and manage.py*, `runserver`:
   <https://docs.djangoproject.com/en/5.2/ref/django-admin/#runserver>

## Amendment (2026-11-18): a commit in doubt in any task, `enqueue`'s, and the auth app's list

The review of this work closed four gaps before release:

- **The mark is the error's own.** An error that takes no attribute (a
  frozen dataclass's) was kept in a `WeakSet`, which compares by value:
  an equal error raised elsewhere read as in doubt while the marked one
  lived, and lost its `Retry-After`. It is kept by its identity now
  (`id`, in a `WeakValueDictionary`), so an error that has no hash is
  marked too. One that takes neither an attribute nor a weak reference
  (a frozen dataclass's with `slots`) stays unmarked, as before; its
  commit still counts in `in_doubt`.
- **In any task.** A commit cut off in a task the request started marked
  that task's error. Under a `BaseHTTPMiddleware`, the route runs in a
  task of an anyio task group, and a cancellation of it is not the
  `Exception` that `call_next` passes on [8]; under `asyncio.TaskGroup`
  the request's own cancellation is raised. Either way
  `DeadlineMiddleware` read another error, and answered `Retry-After: 1`.
  `mark_in_doubt` now notes the commit on the work's deadlines too
  (`deadlines.commit_in_doubt()`, `Deadline.in_doubt`), the objects the
  tasks share, as `after_commit()` lifts them from any task, and the
  middleware reads its deadline's note as well as the error. Rejected: a
  context variable, whose changes in the task do not reach the request's;
  and leaving it as a documented limit, as the deadline already crosses
  the task boundary.
- **`enqueue`'s commit.** It commits outside a unit of work, so a
  `COMMIT` cut off or lost was not marked. It marks the error of its
  commit as a unit of work does, unless the database refused it
  (`refused_commit`, the SQL unit of work's rule, now a function both
  use). A failure before the `COMMIT` is not in doubt.
- **`AUTH.AUTHENTICATION_EXEMPT_PATHS` in `probes.W001`.** The decision
  named it among the four lists, but `check` read the framework's three
  only: the core reads no app's settings. The auth app warns of its own
  list through `on_deployment_warnings`, in every environment, with the
  message the others get (`probes_left_out`).

8. Starlette 1.8.0, `starlette/middleware/base.py`, `call_next`: the
   route runs in `task_group.start_soon(coro)`, which keeps an
   `Exception` it raises for the caller, not a cancellation.
