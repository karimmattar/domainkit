# 0020. Lease idempotency keys, and write them only under the holder's token

- Status: Accepted
- Date: 2026-10-18; amended 2026-10-21 (a pinned key is never released)
- Amends: [0019](0019-idempotency-keys-commit-with-changes.md)

## Context

A keyed request claims its key with an in-progress row. If the process dies
while the request runs, nothing finishes or releases that row, and every
retry gets 409 until the key expires, 24 hours later by default.

Letting a retry take over a stale row needs care. A request is not dead
just because it is slow: if the first request is still running when the
retry takes over, both may commit. [0019](0019-idempotency-keys-commit-with-changes.md)
marks the key in the transaction of the changes, but that mark alone does
not tell two holders apart.

Options considered:

- **Heartbeats.** The holder renews its lease while it runs. A live request
  never loses its key, but every keyed request needs a background task, and
  a stalled event loop still lets the lease lapse.
- **Leases with fencing tokens.** Each holder writes under its own random
  token, and a takeover changes the token. A holder that lost its key finds
  its writes refused.

## Decision

- A claimed key gets a random token and `locked_until`, `LEASE_SECONDS`
  (60 by default) from now.
- A retry may take over a key only when its lease has ended, its request
  matches, and nothing committed. The takeover is one conditional update,
  so only one retry wins, and it fails if the holder finished or committed
  meanwhile.
- The commit mark, the stored response, and the release are written only
  under the holder's token. On `IDEMPOTENCY.DATABASE`, a unit of work whose
  request lost its key fails with 409 `idempotency_key_taken_over`, and
  nothing is saved.
- A unit of work on another database cannot share the mark's transaction.
  Before it commits, its key is pinned (held until it expires) under the
  holder's token, so a lost key refuses that commit too, and no retry can
  take the key over afterwards.
- A key whose request committed and then died answers 409
  `idempotency_response_unavailable` once its lease ends.

## Consequences

- A request that dies before committing can be retried after the lease,
  not a day later.
- `LEASE_SECONDS` must exceed the longest a request runs. A slower request
  loses its key to a retry and fails, but its changes are never made twice.
- On another database, a crash after the pin leaves the key in progress
  until it expires, as a crash after the commit did before. A lost reply to
  that commit still releases the key.
- The table has two new columns, `token` and `locked_until`, so projects
  run `makemigrations` and `migrate`.

## Addendum (2026-10-21): a pinned key is never released

The pin kept retries from taking a key over, but not its own request from
releasing it. The request knew its changes committed only once the commit
returned. When the COMMIT on the other database landed and the call still
raised (its reply lost, or the request cancelled while it waited), the
request saw no commit and deleted its key, and the retry ran the changes
again: a second payment.

- The pin is written to the row (`pinned`, a new column) along with the
  lease, and noted on the operation, both before the commit.
- The release deletes a key only if it is neither committed nor pinned, in
  the row and on the operation; a takeover needs it unpinned too.
- A pinned key the request could not answer keeps its status, without a
  body: retries get 409 `idempotency_response_unavailable`, as after a
  commit on `IDEMPOTENCY.DATABASE` whose reply was lost.

The guarantee is now the same on every database: once a unit of work may
have committed, its keyed request never runs again with that key. It
costs a false alarm where the commit on the other database raised and
nothing was saved (refused, or cancelled before it landed): the key is
kept, and the client looks up the result instead of retrying. A request
that fails before its unit of work commits pinned nothing and is released
as before. Projects run `makemigrations` and `migrate` for the column.
