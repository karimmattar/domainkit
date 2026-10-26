# Idempotency: safe retries of unsafe requests

A client sends `Idempotency-Key: <random UUID>` with a `POST` or `PATCH`. The
first request runs and its response is stored. A retry with the same key and
the same request gets the stored response, with `Idempotent-Replayed: true`.

| Situation | Answer |
| --- | --- |
| Same key, same method, path, query and body | The stored response, replayed |
| Same key, different request | 422 `idempotency_key_reused` |
| Same key while the first is still running, within its lease | 409 `idempotency_key_in_use`, `Retry-After: 1` |
| Key not 1 to 255 printable ASCII characters | 400 `invalid_idempotency_key` |
| Key from a caller with no actor and no credential header, with `ANONYMOUS = "refuse"` | 400 `idempotency_key_needs_caller`: nothing runs |
| First request failed (a 5xx or an exception) or was refused (a 4xx) before committing anything | Not stored: the retry runs again |
| First response marked `Cache-Control: no-store` (new tokens, from `/auth/login` and `/auth/refresh`) | Never stored: a retry runs again if nothing committed, else gets the 409 below |
| First response larger than `MAX_RESPONSE_BYTES`, nothing committed | Not stored: the retry runs again |
| First request died before committing, and its lease has ended | The retry takes the key over and runs |
| First request committed, then failed, died, or answered over `MAX_RESPONSE_BYTES` | 409 `idempotency_response_unavailable`: it never runs again |
| First request's commit on another database raised (its reply lost, refused, or cancelled while it ran) | 409 `idempotency_response_unavailable`: it may have committed, so it never runs again |
| First request ran past its lease and a retry took its key | The first gets 409 `idempotency_key_taken_over`; its changes are not saved |
| Client disconnected before sending the whole body | Nothing runs and no key is claimed: retry with the same key |
| Same key after `TTL_SECONDS`, even if the first request committed | A new request: it runs again (below) |

Within its TTL, a key's request runs twice only if nothing it did was
saved. When a SQL unit of work commits during a keyed request, the key is
marked committed in the same transaction, and a committed key is never released, even if the reply
to the commit was lost. A retry then gets the stored response or, when the
response could not be kept, a 409 telling the client to look up the result
instead. Only units of work, and `enqueue` (which commits its task on its
own, and holds the key as a unit of work's commit does), are seen: call
payment providers and other services from a deferred task (`uow.defer` or
`enqueue`), which the outbox runs once, not from the handler. Writes made
through `connections.autocommit()` or a raw `engine.begin()` inside a keyed
request are outside the key: a request that made them and then failed is
released, and its retry makes them again.

**After the TTL, a key is forgotten.** A key is remembered for
`TTL_SECONDS` (24 hours) from its first request. After that, a request
with the same key is a new request: it runs again, even if the first one
committed, and its response replaces the old one. A client that retries a
payment with a key older than the TTL (after a long outage, from a queue
it replays) is charged twice. Keys are replays of recent requests, not a
record of what was ever done: keep the TTL above the longest your clients
retry for (at most 30 days), and refuse duplicates your domain must never
accept, such as a payment reference, with a unique column of their own.

A request holds its key for `LEASE_SECONDS`. If the process dies before the
request commits anything, a retry after the lease takes the key over and
runs. Each holder has a random token, and the mark is written only under
it: a request that ran past its lease and lost its key to a retry cannot
commit, so the changes are made once. Set `LEASE_SECONDS` above the
longest a request may run:

- it must be above `REQUEST_TIMEOUT`, or startup fails naming both
  settings;
- a `POST` or `PATCH` route that gives itself a deadline at the lease or
  past it (`Depends(RequestTimeout(300))`), or none (`RequestTimeout(None)`),
  is reported by `check`, and staging and production do not start with it;
- with no deadline for any request (`REQUEST_TIMEOUT = None`, or no
  `DeadlineMiddleware` in `MIDDLEWARE`) nothing bounds a keyed request by
  the lease: `check` warns in staging and production, and the
  application still starts; 0.10 refuses to start. A route without a
  deadline is a refusal already because it is one route's exception,
  made in code; the whole application without one is a deployment
  choice that 0.9.1 leaves running;
- your server's or proxy's timeout, if it is longer, counts too.

**What the key covers.** The key holds the request until its response is
sent: the handler, its commits (through units of work and `enqueue`; not
through `connections.autocommit()` or a raw `engine.begin()`), and the
response's body, streamed or not.
Before the last part of a response the key keeps is sent (a success, or
a refusal after a commit), the response, or its status, is stored, so a
client that has the whole response and retries at once gets the replay;
if storing fails, that last part is not sent, and the client retries.
Since this is before anything runs after the response, the work a route
does after it (Starlette's `BackgroundTasks`, which no deadline bounds)
runs once however long it takes: a retry meanwhile gets the stored
response, or a 409 `idempotency_response_unavailable`. A response the key does not keep
(a refusal or a server error before any commit) is settled once the
application returns, as before: a commit made after it still keeps the
key, and otherwise the key is released and a retry runs the request,
and its background work, again. A streamed response is held only by
the lease while it streams, since its deadline is lifted when it starts:
keep a keyed response's streaming within `LEASE_SECONDS`.

The key answers for the response, not for the work after it. A `202`
stored before its background work runs is what a retry gets while that
work runs, whether it then succeeds or not: the key does not tell the
client how the work ended, so give it a resource to look that up. If the
work raises, the key is settled again as for any request that raised:
released when nothing committed or pinned it, so a later retry runs the
request and the work again, or else kept with its status alone, a 409
`idempotency_response_unavailable`.

A body the application sends by path or by file descriptor
(`FileResponse` on a server with the ASGI `http.response.pathsend`
extension, or `http.response.zerocopysend`) never passes through the
middleware, so the key cannot keep it: it is treated as a response the
key does not keep, and a retry runs the request again, or gets the 409
after a commit.

The keys' database decides when a lease or a key runs out. On PostgreSQL,
the claim, the take-over and the purge compare and write
`statement_timestamp()`, as the outbox does, so hosts whose clocks
disagree never take a key from a request still running, nor replace or
purge a key that has not expired. SQLite's clock is the host's, and under
`freeze_time` the frozen clock decides on every database, so tests move
leases and expiry with it.

A request that raises, or is cancelled (its client gone, a deadline, an
anyio cancel scope that cancels every wait inside it), still releases its
key at once: the response is stored, or the key released, in a task of its
own that the request's cancellation cannot stop, within 5 seconds. If that
fails, the failure is logged and the lease applies.

Keep `IDEMPOTENCY.DATABASE` on the database your writes use where you can.
A unit of work on another database cannot carry the mark in its
transaction: before it commits, the key is pinned until it expires, and
right after, it is marked committed. A pinned key is never taken over by a
retry, nor released by its request, since its changes may have committed
whatever the commit reported. So if that commit raises (its reply lost, the
database refusing it, the request cancelled while it ran), retries get the
409 `idempotency_response_unavailable`, as after a commit on the keys'
database, even when nothing was in fact saved: look up the result. A
request that fails before its unit of work commits pinned nothing, and its
key is released as usual. A crash after the pin leaves the key in progress
until it expires.

**Upgrading.** The keys table has a new `pinned` column: run
`makemigrations` and `migrate` in each project that installs idempotency.

Keys are scoped to the actor, which `AuthenticationMiddleware` sets (list
it first), so one user cannot replay another's response. Without an
actor, keys are scoped to the client address and a hash of the request's
`CREDENTIAL_HEADERS` (`Authorization`, `Proxy-Authorization`, `Cookie`,
`X-API-Key`): list the header your project authenticates with if it is
another. A caller with neither has only its address: every caller behind
one NAT or proxy shares its keys, so one that sends the same key and body
as another is replayed the other's response (its status, `Location`, and
body), and a different body learns that the key is taken. Clients make
this unlikely with random keys, but not impossible with predictable ones
(counters, order numbers). Where keyed routes serve anonymous callers
(sign-up, a contact form, a payment link before login), set `ANONYMOUS =
"refuse"`: such a key is answered 400 `idempotency_key_needs_caller`, and
the client authenticates or sends the request without it. The default,
`"per-address"`, keeps the behaviour of 0.9. Add `IdempotencyMiddleware` last in `MIDDLEWARE`: it reads whole
bodies, which `RequestBodyLimitMiddleware` has already bounded. With
messaging installed, expired keys are purged hourly; without it, a new
key deletes up to 100 expired ones once it has claimed its own, skipping
those another request is deleting (`SKIP LOCKED` on PostgreSQL), so
concurrent requests never queue behind one another's purge, nor deadlock
through PgBouncer. A process does this once a minute, and again on the
next key while each sweep deletes a full 100, so a backlog drains as
fast as before; until 0.9.2 every keyed request did it, one more
statement each.

**What a key costs.** A request claims its key with one statement, an
upsert that inserts the in-progress row or replaces an expired one, and
stores its response with one more. Each commits as it runs, without
`BEGIN` and `COMMIT`; through PgBouncer each is a transaction of its own,
which sets the timeouts. The commit mark is one `UPDATE` in the request's
own transaction. With authentication and one unit of work on PostgreSQL,
a first request makes 2 transactions and 4 pool checkouts (16 round
trips), and a replay 1 transaction and 2 checkouts (10 round trips). ADR
[0062](../adr/0062-claim-idempotency-keys-in-one-statement.md) explains the
choices. The statements are built once per dialect and clock
(`domainkit.contrib.idempotency.statements`), and each request binds its
values to them: they send the SQL and parameters that statements built for
each request sent, for about 1,850 fewer Python calls a keyed write.

| `IDEMPOTENCY` key | Default | Meaning |
| --- | --- | --- |
| `DATABASE` | `"default"` | Alias holding the keys. |
| `TTL_SECONDS` | `86400.0` | How long a key is remembered (24 hours; at most 30 days, and at least `LEASE_SECONDS`, or startup fails). |
| `LEASE_SECONDS` | `60.0` | How long a request holds its key before a retry may take it over (at most a day, and above `REQUEST_TIMEOUT`, or startup fails). |
| `METHODS` | `POST`, `PATCH` | Methods that honour the header; others ignore it. |
| `MAX_RESPONSE_BYTES` | `1048576` | Larger responses are passed through but not stored. |
| `CREDENTIAL_HEADERS` | `authorization`, `proxy-authorization`, `cookie`, `x-api-key` | Headers that tell callers without an actor apart. |
| `ANONYMOUS` | `"per-address"` | Keys from callers with no actor and no credential header: `"per-address"` scopes them to the client address, which callers behind one NAT share; `"refuse"` answers 400 `idempotency_key_needs_caller`. |

