# 0062. Claim idempotency keys in one statement, and commit the framework's short writes as they run

- Status: Accepted; amended in place 2026-10-22, and 2026-10-25 (at the end)
- Date: 2026-10-21; amended 2026-10-22 (the sweep comes last), 2026-10-25 (the database's clock decides leases; the sweep once a minute)

## Context

A first request with an `Idempotency-Key` made five transactions, each on a
checkout of its own:

1. a sweep that deleted the key's expired row (and, without messaging, up to
   100 other expired keys);
2. the claim, an `INSERT` of the in-progress row;
3. authentication;
4. the unit of work, which also marks the key committed (ADR 0019);
5. the store of the response.

On a conflict, the claim's failed insert was followed by a third
connection that read the row. A retry therefore took four checkouts. At
0.8.0 on PostgreSQL, a keyed `POST` made 34 round trips and a replay 27.
The commit mark added an `UPDATE`, and with it 11% of CPU, over 0.6.1
(N3).

The guarantees to keep:

- A committed operation never runs twice, and the mark is part of the
  business transaction (ADR 0019).
- A key pinned for a commit on another database is never released or
  taken over: its retries get 409 (0.8.0).
- A cancelled request settles its key in a shielded task within
  `SETTLE_TIMEOUT` (R03).
- Of requests racing with one key, exactly one runs.
- Fingerprint mismatches, expiry, take-over of abandoned keys, and a
  request that died after committing behave as before.

Options considered:

- **Fold the sweep into the claim.** `INSERT … ON CONFLICT (key_hash) DO
  UPDATE … WHERE expires_at < now RETURNING token` either inserts the row,
  replaces an expired one, or leaves a live one alone. PostgreSQL and
  SQLite both take it. Under `READ COMMITTED`, PostgreSQL checks the
  `WHERE` again on the newest version of a row it waited for, so of two
  claims of one expired key only one replaces it.
- **Run the framework's single-statement writes without `BEGIN` and
  `COMMIT`.** The claim, take-over, store or release, pin, and late mark
  each stand on their own. The release-or-store pair depends only on the
  order of its two statements, which `READ COMMITTED` did not make atomic
  either. Through PgBouncer, the timeouts are set with `SET LOCAL`, which
  needs a transaction.
- **Fold the store into the unit of work.** Impossible: the response does
  not exist yet when the unit of work commits.
- **Fold the mark into the outbox insert, with a CTE.** It saves one round
  trip, only when messaging writes in the same commit, at the cost of
  coupling two apps' SQL.
- **Skip pre-ping on these checkouts.** Their writes cannot be safely run
  again after a lost connection: a claim whose reply was lost has
  committed.

## Decision

- The claim is one upsert on a connection whose statements commit as they
  run (`connections.autocommit()`). On a conflict it reads the live row on
  the same connection.
- The take-over, the store or release, and the pin and late mark of a
  commit on another database also run on `connections.autocommit()`.
  Through PgBouncer, that is one transaction, which sets the timeouts.
- Without messaging, the sweep of expired keys is one more statement on
  the claim's connection, the last one. It still skips rows another claim
  is deleting. Through PgBouncer the claim's statements share one
  transaction: a sweep run first held the locks of the keys it deleted
  while the upsert waited for another claim's row, and two claims whose
  keys lay in each other's sweep deadlocked (SQLSTATE 40P01, a 503).
  Run last, the sweep never waits, and nothing waits while it holds its
  locks. (Amended 2026-10-22.)
- The mark stays in the business transaction. Pre-ping stays on these
  checkouts, with ADR 0061's ping of one round trip.

## Consequences

Measured on PostgreSQL, on the wire:

- A first keyed request takes 16 round trips instead of 34, of which the
  cheaper ping saves 10 and this change 8.
- It makes 2 transactions instead of 5, and 4 checkouts instead of 5.
- A replay takes 10 round trips instead of 27, and 2 checkouts instead of
  4.

The framework's own claim, store, pin and late mark do not lower
`statement_timeout` to the request's deadline any more: `SET LOCAL` needs
a transaction. They are still bounded:

- by `STATEMENT_TIMEOUT`, `LOCK_TIMEOUT` and `COMMAND_TIMEOUT`, which the
  session keeps outside PgBouncer;
- the claim by the request deadline's timer;
- the store or release by `SETTLE_TIMEOUT`;
- the pin before a commit on another database, and the mark after it, by
  the deadline: the request's timer cuts them off, and in a task that only
  shares the deadline (`deadlines.enforced()` is false), a
  `deadlines.timeout` block of their own does, which costs no round trip.
  A deadline that has passed changes nothing, as for a transaction's
  timeouts. A pin cut off may still have landed on the server; the key is
  then pinned, and its retries get 409, as when the commit after a pin
  fails. (Amended 2026-10-22.)

`connections.autocommit()` is for statements that each make sense alone:
code that needs several to commit together must use a transaction.

The guarantees above are tested unchanged. New tests count the
statements, transactions and checkouts of a keyed write, race five claims
of one key, and run the claim through PgBouncer's mode.

## Amendment (2026-10-25): the database's clock decides leases

The claim, the take-over and the checks of a held key compared
`locked_until` and `expires_at` with the host's clock, while the outbox
(ADR 0043) and the sealer (ADR 0047) had moved to the database's. Two
hosts whose clocks differ by more than a lease's slack disagree on whether
it ran out: a host ahead took a key from a request still running (whose
commit then failed with 409 `idempotency_key_taken_over`, after anything
it did outside its transaction), replaced a committed key before its TTL
and ran it again, and its purge deleted live keys. Nothing checked either
that a request ends before its lease does (audit 0.9.0, FIN-79).

- On PostgreSQL, the claim writes `created_at`, `expires_at` and
  `locked_until` from `statement_timestamp()`, and replaces only a row
  whose `expires_at` is before it. On a conflict, the read of the live row
  returns `locked_until < statement_timestamp()` with it, and the
  take-over and the check of a request that died after committing use
  that. The take-over writes its lease, and the purge (hourly, or the
  claim's sweep) compares, on the same clock. `domainkit.db.dbclock`, the
  outbox's clock moved where both apps use it, decides: SQLite and a
  frozen clock read `domainkit.clock`, as before.
- No statement is added: the comparisons are columns and conditions of
  the statements above. The keyed write still makes 4 statements, 3
  transactions and 3 checkouts in the ladder.
- The commit mark (`committed_at`) keeps the host's clock: only whether it
  is set is ever read.
- `IDEMPOTENCY.LEASE_SECONDS` must be above `REQUEST_TIMEOUT`, or startup
  fails naming both. A `POST` or `PATCH` route whose own `RequestTimeout`
  is at the lease or past it, or `None`, is a problem `check` reports, and
  staging and production refuse to start with it; elsewhere it is logged.
  A deadline was considered as the bound instead of a refusal, by
  extending the lease to a route's deadline when its dependency runs; it
  would put an `UPDATE` on every such request, and a lease that silently
  outgrows the setting.

## Amendment (2026-10-25): the response, then what runs after it

A security review of 0.9.1 (SEC-r2-1, SEC-r2-4) found the lease bound
short in two places. `DeadlineMiddleware` lifts the deadline when the
response starts, but the middleware stored the response only once the
application returned, after the body and after Starlette's
`BackgroundTasks`. A provider call made in a background task could
outlive the lease, a retry then took the key over and called the
provider again, and a background task that raised released the key. And
with no deadline at all (`REQUEST_TIMEOUT = None`, or no
`DeadlineMiddleware`), nothing bounded a keyed request.

- A response the key keeps (a success, or a refusal after a commit), or
  whose status it keeps (a commit or a pin, with no body to store), is
  settled when its last body part (`more_body` false) has been sent,
  before the application goes on. The settle is the one it was, moved:
  the keyed write still makes 4 statements, 3 transactions and 3
  checkouts in the ladder. A failure to store reaches the application
  where it sent the response, so the work after it does not run with the
  key unsettled; a cancellation there leaves the settle running, as
  before, and is not settled twice.
- A response the key does not keep is settled when the application
  returns, as before: a commit made by work after the response must still
  keep the key, and without one the key is released, so a retry runs the
  request and its background work again, as for any refused request.
- Renewing `locked_until` until the application returns was considered:
  it keeps the bound but adds a write per lease interval and a timer per
  keyed request, and a retry would still wait on work the client is not
  waiting for. Storing the response first answers the retry at once.
- If the application raises after that early settle (a background task
  that failed), the key is settled again, as for any request that raised:
  released when nothing committed or pinned it, so a retry runs the work
  that failed, or else kept with its status alone, a 409. Replaying the
  stored `202` would tell every retry for `TTL_SECONDS` that work that
  never happened was accepted (SEC-r2-7). This settle is on the failure
  path only; a settle that itself failed or was cancelled is not repeated.
  A retry that arrives while the work runs still gets the stored
  response, which says nothing of how the work ends.
- A body sent by path or by file descriptor (`http.response.pathsend`,
  `http.response.zerocopysend`) never passes through the middleware: such
  a response is one the key does not keep. It was stored as an empty
  body, and replayed as an empty `200` (SEC-r2-8).
- The settle comes before the last body part is sent, not after it. Sent
  first, the whole response could reach a client that retried at once
  while the key was still in progress, and got a 409 for a request that
  had succeeded; settling after it made that window wider than at 0.9.0,
  which settled as soon as the application returned. A client now never
  holds a whole response its key does not hold too, as an API that
  records a payment before it answers. If the store fails, the last part
  is not sent: the client sees a broken response and retries, rather than
  holding an answer the key cannot stand behind, and the error reaches
  the application at that `send`, as before. A response the key does not
  keep is still released when the application returns, so a retry in
  that moment can still get the 409 `Retry-After: 1`, as at 0.9.0.
- `messaging.enqueue` commits its task in a transaction of its own,
  which no unit of work saw: a keyed request that queued work and then
  failed was released, and its retry queued the work again (SEC-r2-9; at
  0.9.0 too). Two hooks on `DomainConfig`, `stage_transaction(connection,
  database)` and `transaction_committed(database)`, let installed domains
  join such a transaction as they join a unit of work's; `enqueue` calls
  them, and idempotency marks the key in that transaction on its own
  database, or pins it before the commit and marks it after elsewhere,
  with the same code as for a unit of work. A neutral hook in
  `domainkit.apps` keeps messaging from importing idempotency, and serves
  any later commit made outside a unit of work. Rebuilding `enqueue` on
  a unit of work was considered: it would change its statements and every
  domain's staging for one row. A task deferred with `uow.defer` commits
  with its unit of work and was always seen; an `enqueue` inside a unit of
  work's block is its own commit, and holds the key on its own. Writes
  through `connections.autocommit()` or a raw `engine.begin()` stay
  outside the key.
- A keyed response that streams is held by its lease alone while it
  streams. With no deadline at all, `check` warns in staging and
  production; startup does not refuse in a patch release, since such
  deployments run today. 0.10 refuses to start without a deadline where
  idempotency is installed.

## Amendment (2026-10-25): the sweep runs once a minute, not on every claim

Without messaging, every claim swept up to 100 expired keys: a fourth
statement, and up to 100 deletes, on each keyed request, however few keys
had expired (PERF-10).

- A claim sweeps when `SWEEP_EVERY` (60 seconds, on `domainkit.clock`)
  has passed since the middleware's last sweep, or when that sweep
  deleted a whole `PURGE_BATCH`, since more may wait. The sweep is the
  same statement, still the last on the claim's connection. The schedule
  is the middleware's, behind a lock: loops on several threads share it.
- Measured on PostgreSQL, 3,000 claims at 20 a second with 5,000 keys
  expired beforehand and a 30-second TTL: sweeping on every claim made
  3,000 sweeps; sweeping with probability 1/10, the audit's option, 307;
  this schedule, 71. All three deleted the backlog. Expired keys left
  waiting stay below what expires in a minute, plus a batch; a claim of
  an expired key replaces it whatever the sweep does. The schedule is
  deterministic, so its tests need no seeded randomness.
- Moving the sweep off the request path, into a background task, was
  considered: an application served without its lifespan would never
  purge, and the sweep needs no request's time once it runs a minute
  apart.
- With messaging installed nothing changes: the hourly task purges, and
  claims never sweep. The keyed write of the ladder makes 4 statements,
  3 transactions and 3 checkouts, as before.
