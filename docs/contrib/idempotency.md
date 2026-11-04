# Idempotency: safe retries of unsafe requests

A client that sends a payment and gets no answer does not know if the
payment ran. If it sends the request again, the payment may run twice.
With the idempotency app, the client adds a header, `Idempotency-Key:
<random UUID>`, to its `POST` or `PATCH`. The first request runs, and
its response is stored. A retry with the same key and the same request
gets the stored response, and nothing runs twice.

On this page you learn how to:

- install the middleware and send a keyed request;
- read every answer a retry can get;
- know what a key protects (commits through a
  [unit of work](../learning.md#words)) and what it does not;
- set the lease, the deadline, and how long keys are kept;
- choose whose key it is when the caller is not logged in;
- keep the keys on the right database.

## Install

Add the app, and add `IdempotencyMiddleware` last in `MIDDLEWARE`. It
reads whole request bodies, so it must come after
`RequestBodyLimitMiddleware`, which limits them. It must also come after
`AuthenticationMiddleware`, which sets the actor:

```python
INSTALLED_DOMAINS = [
    "domainkit.contrib.messaging",  # purges old keys every hour
    "domainkit.contrib.idempotency",
]
MIDDLEWARE = [
    "domainkit.http.middleware.ProxyHeadersMiddleware",
    "domainkit.http.middleware.SecurityHeadersMiddleware",
    "domainkit.http.middleware.RequestContextMiddleware",
    "domainkit.http.deadlines.DeadlineMiddleware",
    "domainkit.http.shedding.ConcurrencyLimitMiddleware",
    "domainkit.http.middleware.AllowedHostsMiddleware",
    "domainkit.http.websockets.WebSocketLimitsMiddleware",
    "domainkit.http.middleware.RequestBodyLimitMiddleware",
    "domainkit.contrib.auth.middleware.AuthenticationMiddleware",
    "domainkit.contrib.idempotency.middleware.IdempotencyMiddleware",
]
```

```bash
python manage.py makemigrations -m "idempotency keys"
python manage.py migrate
```

The app brings one table, `domainkit_idempotency_keys`, on
`IDEMPOTENCY.DATABASE`.

## A first example

Here `POST /orders` makes an order and answers how many orders exist. A
client sends the same order twice with the same key, then a different
order with that key:

```bash
curl -X POST https://shop.example.com/orders \
  -H "Authorization: Bearer $TOKEN" \
  -H "Idempotency-Key: 4f0c3c6e-0d55-4a6e-9a3f-1b2c3d4e5f60" \
  -H "Content-Type: application/json" -d '{"item": "lamp"}'
```

The three answers, each with its status:

```text
201 {"count":1}
201 Idempotent-Replayed: true {"count":1}
422 {"detail":"This Idempotency-Key was used for a different request.","code":"idempotency_key_reused"}
```

The first request ran and answered 201. The retry got the same answer,
with the header `Idempotent-Replayed: true`, and the route did not run
again. The third request had the same key but another body, so it was
refused.

Make a new random key for each new operation, and send the same key
again only for a retry of that operation.

## What a retry gets

The key, with the caller, names a request. Its fingerprint covers the
method, the path, the query, and the body.

| Situation | Answer |
| --- | --- |
| Same key, same method, path, query, and body | The stored response, replayed |
| Same key, different request | 422 `idempotency_key_reused`, past the TTL too while the key is a tombstone |
| Same key while the first is still running, within its lease | 409 `idempotency_key_in_use`, `Retry-After: 1` |
| Key not 1 to 255 printable ASCII characters | 400 `invalid_idempotency_key` |
| Key from a caller with no actor and no credential header (`ANONYMOUS = "refuse"`, the default) | 400 `idempotency_key_needs_caller`: nothing runs |
| First request failed (a 5xx or an exception) or was refused (a 4xx) before committing anything | Not stored: the retry runs again |
| First response marked `Cache-Control: no-store` (new tokens, from `/auth/login` and `/auth/refresh`) | Never stored: a retry runs again if nothing committed, else gets the 409 below |
| First response larger than `MAX_RESPONSE_BYTES`, nothing committed | Not stored: the retry runs again |
| First request died before committing, and its lease has ended | The retry takes the key over and runs |
| First request committed, then failed, died, or answered over `MAX_RESPONSE_BYTES` | 409 `idempotency_response_unavailable`: it never runs again |
| First request's commit on another database raised (its reply lost, refused, or cancelled while it ran) | 409 `idempotency_response_unavailable`: it may have committed, so it never runs again |
| First request ran past its lease and a retry took its key | The first gets 409 `idempotency_key_taken_over`; its changes are not saved |
| Client disconnected before sending the whole body | Nothing runs and no key is claimed: retry with the same key |
| Same key after `TTL_SECONDS`, the first request committed (or pinned its key) | 409 `idempotency_response_unavailable`: the response is gone, but the key is kept as a tombstone for `TOMBSTONE_SECONDS` more, and the request never runs again |
| Same key after `TTL_SECONDS`, the first request committed nothing | A new request: it runs again |
| Same key after `TTL_SECONDS` and then `TOMBSTONE_SECONDS` | A new request: it runs again, even if the first committed |

Only `POST` and `PATCH` honour the header (`METHODS`). Other methods
ignore it.

The 409 `idempotency_response_unavailable` tells the client: "the
request already ran, but its response was not kept; look up the result
instead of retrying". Give clients a resource where they can find it.

## What a key protects

Within its TTL, and its tombstone's after it, a key's request runs twice
only if nothing it did was saved. When a SQL unit of work commits during
a keyed request, the key is marked committed in the same transaction. A
committed key is never released, even if the reply to the commit was
lost. A retry then gets the stored response or, when the response could
not be kept, the 409.

The key sees only two kinds of commit:

- a unit of work's commit;
- `enqueue`, which commits its task on its own and holds the key as a
  unit of work's commit does.

!!! warning
    Writes made through `connections.autocommit()` or a raw
    `engine.begin()` inside a keyed request are outside the key. A request
    that made them and then failed is released, and its retry makes them
    again. Call payment providers and other services from a deferred task
    (`uow.defer` or `enqueue`), which the [outbox](messaging.md) runs once,
    not from the route.

Keys replay recent requests. They are not a record of everything that
was ever done. For duplicates that your domain must never accept, such
as a payment reference, add a unique column of their own.

## After the TTL: tombstones

A response is replayed for `TTL_SECONDS` (24 hours) from the first
request. After that, a key whose request committed, or pinned the key
for a commit on another database, is kept as a tombstone for
`TOMBSTONE_SECONDS` (7 days) more. The purge drops its response, its
headers, and whom it was about. It keeps a hash of the caller and the
key, the request's fingerprint, its status, and when it committed.

A retry with the same key and request then gets 409
`idempotency_response_unavailable`, and never runs again. Another request
with that key gets the 422. Until the purge comes, the key's response is
past its TTL: it is not replayed, and retries get the same 409. A key
that nothing committed or pinned is forgotten at the TTL: a request with
it is new.

A week covers the times when clients often retry after a day: a queue
replayed after an outage over a long weekend, a broker's default
retention (Kafka keeps 7 days, SQS 4), and messaging's own 7 days of
finished messages. Past `TTL_SECONDS` and `TOMBSTONE_SECONDS` (at most
30 and 90 days), a key is forgotten even if it committed. A tombstone
costs about 390 bytes of table and index per key (ADR
[0096](../adr/0096-keep-committed-idempotency-keys-as-tombstones.md)).
`TOMBSTONE_SECONDS = 0` forgets a committed key with its response, as
0.10 did.

## Leases and deadlines

A request holds its key for `LEASE_SECONDS`. If the process dies before
the request commits anything, a retry after the lease takes the key over
and runs. Each holder has a random token, and the commit mark is written
only under that token. So a request that ran past its lease and lost its
key to a retry cannot commit, and the changes are made once.

Set `LEASE_SECONDS` above the longest time a request may run:

- It must be above `REQUEST_TIMEOUT`, or startup fails and names both
  settings.
- A `POST` or `PATCH` route that gives itself a deadline at the lease or
  past it (`Depends(RequestTimeout(300))`), or no deadline
  (`RequestTimeout(None)`), is reported by `check` as
  `idempotency.E001`. Staging and production do not start with it.
- With no deadline for any request (`REQUEST_TIMEOUT = None`, or no
  `DeadlineMiddleware` in `MIDDLEWARE`), nothing limits a keyed request
  to the lease. Staging and production refuse to start, and `check`
  fails with `idempotency.E002`, which `SILENCED_CHECKS` cannot name.
  0.9 only warned; `IDEMPOTENCY.REQUIRE_DEADLINE = False` keeps that
  warning (`idempotency.W001`).
- Your server's or proxy's timeout counts too, if it is longer.

`TTL_SECONDS` must be at least `LEASE_SECONDS`. Otherwise a key could
expire while its request still held it, and a retry could run the
request a second time.

## The response and the work after it

The key holds the request until its response is sent: the route, its
commits, and the response's body, streamed or not.

Before the last part of a response that the key keeps is sent (a
success, or a refusal after a commit), the response, or its status, is
stored. So a client that has the whole response and retries at once gets
the replay. If storing fails, that last part is not sent, and the client
retries. Because this happens before anything that runs after the
response, the work a route does after it (Starlette's `BackgroundTasks`,
which no deadline limits) runs once, however long it takes. A retry
meanwhile gets the stored response, or a 409
`idempotency_response_unavailable`.

A response that the key does not keep (a refusal or a server error
before any commit) is settled once the application returns. A commit
made after that still keeps the key. Otherwise the key is released, and a
retry runs the request, and its background work, again.

The key answers for the response, not for the work after it. A `202`
stored before its background work runs is what a retry gets while that
work runs, whether it then succeeds or not. So give the client a
resource where it can see how the work ended. If the work raises, the
key is settled again as for any request that raised: released when
nothing committed or pinned it, else kept with its status alone, a 409.

A streamed response is held only by the lease while it streams, because
its deadline is lifted when it starts. Keep a keyed response's streaming
within `LEASE_SECONDS`.

A body that the application sends by path or by file descriptor
(`FileResponse` on a server with the ASGI `http.response.pathsend`
extension, or `http.response.zerocopysend`) never passes through the
middleware. The key cannot keep it, so it counts as a response that the
key does not keep.

A request that raises, or is cancelled (its client gone, a deadline, an
anyio cancel scope), still releases its key at once. The response is
stored, or the key released, in a task of its own that the request's
cancellation cannot stop, within 5 seconds. If that fails, the failure
is logged and the lease applies.

## Bodies in memory and on disk

The middleware reads a keyed request's whole body before the route runs,
because the fingerprint covers it. It also keeps what a response sends
until the response is stored. Both are held in memory up to
`SPOOL_BYTES` (1 MiB), and past that in a temporary file. The file is
unlinked from the start, written and read on a thread, and closed when
the request ends. When `Content-Length` gives the body's length, the
body is fingerprinted as it arrives.

A body held in memory reaches the application in one message; one from
a file, in parts of 64 KiB. An 8 MiB keyed body held 16 MiB in 0.11, and
holds about 1 MiB now. A kept response is held once, as the value
stored.

Files go to the temporary directory (`TMPDIR`), so a body past the
threshold reaches the disk unless that directory is a `tmpfs`. Set
`SPOOL_BYTES` at or above your limits to keep bodies in memory. Where no
temporary file can be made, the body stays in memory, with a warning. A
response that can never be kept is not held at all: one marked
`Cache-Control: no-store` is decided as it starts, and one that grows
past `MAX_RESPONSE_BYTES`, or sends its body by path, is let go then
(SEC-12b-2, [ADR 0105](../adr/0105-fingerprint-keyed-bodies-as-they-arrive-and-spool-them-past-a-threshold.md)).

## Whose key it is

Keys belong to the actor that `AuthenticationMiddleware` sets, so one
user cannot replay another's response.

Credentials limited to scopes (a personal token made for an integration)
are a caller apart from their user's other credentials, one caller for
each set of scopes. A retry is answered before the route's rules run, so
such a token is never replayed what the user's login got on a route that
refuses the token. A login and a refreshed token, which carry no limit,
share the user's keys (SEC-11a-2).

Without an actor, keys belong to the client address and a hash of the
request's `CREDENTIAL_HEADERS` (`Authorization`, `Proxy-Authorization`,
`Cookie`, `X-API-Key`). If your project authenticates with another
header, list it. Such a header names a caller only where no
authentication middleware judged the request, or one accepted it. Once
one found no one (its principal in `scope["user"]` is not
authenticated), the header named nobody. Examples are `Authorization:
Bearer null`, which logged-out browser applications send, or a gateway's
fixed `X-API-Key` (SEC-11a-1). Your own layer that authenticates by a
header names its caller by binding the actor, or by putting an
authenticated principal in `scope["user"]`.

A caller with neither has only its address, which every caller behind
one NAT or proxy shares. Keyed by the address alone, one caller that
sends the same key and body as another would get the other's response:
its status, `Location`, `ETag`, and body. A different body would show
that the key is taken. Random keys make this unlikely, but predictable
ones (counters, order numbers) do not. So by default (`ANONYMOUS =
"refuse"`, since 0.11) such a key gets 400 `idempotency_key_needs_caller`:

```json
{"detail": "An Idempotency-Key needs a caller this service knows: authenticate, or send the request without the key.", "code": "idempotency_key_needs_caller"}
```

Nothing runs. The client authenticates, or sends the request without
the key.

A `Cookie` names a caller only if it carries one of `SESSION_COOKIES`
(none by default, because auth's sessions set the actor). A consent or
locale cookie, which many callers send alike, does not (SEC-10b-7).
`ANONYMOUS = "per-address"` keeps 0.10's default, keys by address, for a
service whose anonymous callers each have their own address (DATA-09):

```python
IDEMPOTENCY = {"ANONYMOUS": "per-address"}
```

## Keys on another database

Keep `IDEMPOTENCY.DATABASE` on the database that your writes use where
you can. Then the commit mark is written in the same transaction as your
changes.

A unit of work on another database cannot carry the mark in its
transaction. Before it commits, the key is pinned until it expires, and
right after, it is marked committed. A pinned key is never taken over by
a retry, nor released by its request, because its changes may have
committed whatever the commit reported. So if that commit raises (its
reply lost, the database refusing it, the request cancelled while it
ran), retries get the 409 `idempotency_response_unavailable`, even when
nothing was saved. A request that fails before its unit of work commits
pinned nothing, and its key is released as usual. A crash after the pin
leaves the key in progress until it expires.

`IDEMPOTENCY.DATABASE` must name one database that is not part of a
shard group.

## Purging old keys

With messaging installed, a worker purges expired keys every hour, and
makes tombstones of those that may have committed. Without messaging, a
new key deletes up to 100 expired ones once it has claimed its own, then
makes tombstones of up to 100 more. It skips those that another request
is purging (`SKIP LOCKED` on PostgreSQL), so requests never wait for one
another's purge, nor deadlock through PgBouncer. A process does this once
a minute, and again on the next key while a sweep deletes or buries a
full 100, so a backlog goes down fast. A tombstone's expiry moves forward
when it is made, so a purge reads only the keys that are due.

## Personal data

A kept response may hold personal data. `privacy erase` drops the kept
responses about a person, and keeps their keys, so a retry gets 409 and
never runs again. `privacy export` lists them. A response is about the
caller who got it, and about the people whose events its request
committed. Only its caller gets its body in an export; others see it
withheld ([privacy.md](../privacy.md)).

## What a key costs

A request claims its key with one statement: an upsert that inserts the
in-progress row or replaces an expired one. It stores its response with
one more. Each commits as it runs, without `BEGIN` and `COMMIT`; through
PgBouncer each is a transaction of its own. The commit mark is one
`UPDATE` in the request's own transaction.

With authentication and one unit of work on PostgreSQL, a first request
makes 2 transactions and 4 pool checkouts (16 round trips), and a replay
1 transaction and 2 checkouts (10 round trips). ADR
[0062](../adr/0062-claim-idempotency-keys-in-one-statement.md) explains
the choices. The statements are built once per dialect and clock
(`domainkit.contrib.idempotency.statements`), and each request binds its
values to them: about 1,850 fewer Python calls per keyed write.

## Settings

Every key is optional. [Settings](../reference/settings.md) lists the
other settings.

| `IDEMPOTENCY` key | Default | Meaning |
| --- | --- | --- |
| `DATABASE` | `"default"` | The alias that holds the keys. |
| `TTL_SECONDS` | `86400.0` | How long a key's response is replayed (24 hours; at most 30 days, and at least `LEASE_SECONDS`, or startup fails). |
| `TOMBSTONE_SECONDS` | `604800.0` | How long a key whose request committed, or pinned it, is kept past `TTL_SECONDS` without its response, so retries get 409 instead of running again (7 days; at most 90 days). `0` forgets it at the TTL, as 0.10 did (FIN-84). |
| `LEASE_SECONDS` | `60.0` | How long a request holds its key before a retry may take it over (at most a day, and above `REQUEST_TIMEOUT`, or startup fails). |
| `REQUIRE_DEADLINE` | `True` | Staging and production refuse to start while keyed requests have no deadline (no `REQUEST_TIMEOUT`, or no `DeadlineMiddleware`). `False` only warns, as 0.9 did. |
| `METHODS` | `POST`, `PATCH` | Methods that honour the header; others ignore it. |
| `MAX_RESPONSE_BYTES` | `1048576` | Larger responses are passed through but not stored. |
| `SPOOL_BYTES` | `1048576` | Bytes of a keyed request's body, and of a response being kept, held in memory (at most 1 GiB; `0` holds none). Past them, the rest goes to a temporary file, unlinked from the start and closed when the request ends. With `MAX_REQUEST_BODY_SIZE` and `MAX_RESPONSE_BYTES` at their defaults, nothing reaches the disk (PERF-11). |
| `CREDENTIAL_HEADERS` | `authorization`, `proxy-authorization`, `cookie`, `x-api-key` | Headers that tell callers without an actor apart; not once an authentication middleware found no one in the request (SEC-11a-1). |
| `ANONYMOUS` | `"refuse"` | Keys from callers with no actor and no credential header. `"refuse"` answers them 400 `idempotency_key_needs_caller`, and nothing runs. `"per-address"` keys them by the client address, which callers behind one NAT share, as 0.10 did by default (DATA-09). |
| `SESSION_COOKIES` | `()` | For `ANONYMOUS = "refuse"`: the cookies that name a caller the actor does not (a session another layer reads), at most 16 names of 1 to 64 letters, digits, `_`, `.`, or `-`. Any other cookie names nobody (SEC-10b-7). |

## Checks

| Id | When | What to do |
| --- | --- | --- |
| `idempotency.E001` | A keyed route's own `RequestTimeout` is at the lease or past it, or `None`. | Raise `LEASE_SECONDS` above the route's timeout, or shorten the timeout. |
| `idempotency.E002` | Staging or production with no `REQUEST_TIMEOUT`, or no `DeadlineMiddleware`. | Set `REQUEST_TIMEOUT` below `LEASE_SECONDS` and list `DeadlineMiddleware`. |
| `idempotency.W001` | The same, with `REQUIRE_DEADLINE = False`: a warning only. | The same. |
| (startup) | `LEASE_SECONDS` is not above `REQUEST_TIMEOUT`, `TTL_SECONDS` is below `LEASE_SECONDS`, or `DATABASE` is in a shard group. | Change the settings that the message names. |

`idempotency.W002` warned in 0.10 while `ANONYMOUS` was left to
`"per-address"`. It went with that default in 0.11; listed in
`SILENCED_CHECKS`, it silences nothing.

## On PostgreSQL and SQLite

On PostgreSQL, the claim, the take-over, and the purge compare and write
the database's `statement_timestamp()`, as the outbox does. So hosts
whose clocks disagree never take a key from a request that is still
running, nor replace or purge a key that has not expired. The purge
skips rows that another request locked (`SKIP LOCKED`).

SQLite's clock is the host's. Under `freeze_time`, the frozen clock
decides on every database, so tests move leases and expiry with it.

## Upgrading

The keys table got a `pinned` column in 0.9.1, and a `tombstone` column
in 0.11. Run `makemigrations` and `migrate` in each project that
installs idempotency. Since 0.11, a token limited to scopes has keys of
its own: after the upgrade, a retry of a keyed request that it sent
before is a new request, and runs.

## Common mistakes

- **Calling a payment provider from the route.** The key does not see
  that call, so a failed request's retry calls it again. Call it from a
  deferred task.
- **Putting `IdempotencyMiddleware` before `AuthenticationMiddleware`.**
  The key then has no actor. List it last.
- **Using a counter or an order number as the key.** Make a new random
  UUID for each operation.
- **Treating keys as a record of what was done.** Keys are forgotten
  after `TTL_SECONDS` and `TOMBSTONE_SECONDS`. Use a unique column for
  duplicates your domain must refuse.
- **A long route with a short lease.** A retry can take the key over
  while the route still runs. Keep every keyed route's deadline under
  `LEASE_SECONDS`.

## See also

- [Request deadlines](../runtime.md#request-deadlines): `REQUEST_TIMEOUT` and `RequestTimeout`.
- [Messaging](messaging.md): deferred tasks, which run once.
- [A payment that runs again](../recipes/payments.md): a how-to that
  uses keys.
- [Problems](../reference/problems.md): every error code.
- [Privacy](../privacy.md): erasure and export of kept responses.
