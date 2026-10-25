# 0062. Claim idempotency keys in one statement, and commit the framework's short writes as they run

- Status: Accepted
- Date: 2026-10-21; amended 2026-10-22 (the sweep comes last), 2026-10-25 (the database's clock decides leases)

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
