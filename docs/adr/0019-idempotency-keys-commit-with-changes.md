# 0019. Mark idempotency keys committed in the transaction of the changes

- Status: Accepted; leases added by [0020](0020-lease-idempotency-keys.md)
- Date: 2026-10-18

## Context

`IdempotencyMiddleware` claims a key in its own transaction, runs the
request, and then either stores the response or releases the key so the
client may retry. It released the key whenever the response was not stored:
a 5xx, an unhandled exception, or a body over `MAX_RESPONSE_BYTES`. None of
these says whether the request's changes were saved. A handler that
commits a payment and then fails, or whose response is too large to keep,
released its key, and the client's retry paid again.

The middleware cannot tell from the response whether anything committed.
The unit of work can: every SQL unit of work already lets installed apps
add rows to its transaction before the commit (`DomainConfig.stage`) and
follow up after it (`DomainConfig.committed`).

Options considered:

- **Store every response, 5xx included, and never release.** Nothing runs
  twice, but a request that failed before changing anything could never be
  retried with its key, and a replayed 500 tells the client nothing about
  whether its payment went through.
- **Release only on some status codes.** Still a guess: a 503 can follow a
  commit as easily as a 500.
- **Let the unit of work record the commit with the key.** The key's row is
  updated in the same transaction as the changes, so the database knows
  whether they committed, even when the reply to the commit is lost.

## Decision

- The middleware runs each keyed request as an operation, held in a
  context variable.
- When a SQL unit of work on `IDEMPOTENCY.DATABASE` commits during the
  operation, the idempotency app sets the key's `committed_at` in the same
  transaction. A unit of work on another database cannot share that
  transaction, so the key is marked right after its commit.
- A key is released only if no commit was seen and its row has no
  `committed_at`. Otherwise the response is stored, or, when it cannot be
  kept, the row is finished without a body.
- A retry of a finished row without a body gets a 409
  `idempotency_response_unavailable`, naming the first status, and the
  request does not run again.

## Consequences

- A keyed request's changes commit at most once, for changes made through
  units of work on `IDEMPOTENCY.DATABASE`.
- Changes on another database have a short window between their commit and
  the mark. A crash there leaves the key in progress until it expires.
- Work outside a unit of work, such as a call to a payment provider from
  the handler, is not seen. Such calls belong in deferred tasks, which the
  outbox runs once.
- Clients must handle the new 409: the request ran, so they should look up
  its result rather than retry with a new key.
- The idempotency table has a new `committed_at` column, so projects run
  `makemigrations` and `migrate`.
- A key still in progress after a crash keeps answering 409 until it
  expires. Leases for such keys are a separate change.
