# The request flow

One path, for every change a client asks for. This page follows
`POST /products/{product_id}/restock` from the socket to the answer, and
after it. Each stage names the code that runs it and what can refuse the
request there.

```text
 client
   │  POST /products/7f3c…/restock   If-Match: W/"4-…"   {"quantity": 3}
   ▼
 1  middleware (settings.MIDDLEWARE, outermost first)
   │  proxy headers → locale → security headers → request context
   │  → deadline → concurrency limit → allowed hosts → authentication
   │  → rate limit → WebSocket limits → body size limit
   ▼
 2  route matching           first path segment indexes the routes
   ▼
 3  parameters (FastAPI)     unit of work (made, not opened), path id,
   │                         If-Match, body schema, provided fields
   ▼
 4  access rules             Require(*rules): 401 / 403
   ▼
 5  the command              Restock(product_id=…, quantity=3)
   ▼
 6  reach                    scope, parent, object rules, If-Match
   ▼
 7  the use case             async with uow: load → aggregate method → return
   ▼
 8  commit                   clean() → collect events → stage outbox rows
   │                         → SQL COMMIT → publish events in-process
   ▼
 9  the answer               schema, ETag, Location; 200 / 201 / 204
   ▼
 client
   ┊
 10 after the response       the worker runs durable handlers and tasks
                             from the outbox, each in a transaction of its own
```

## 1. Middleware

Pure ASGI middleware, in the order `MIDDLEWARE` lists it; each sees the
request before the next and the response after it.

| Middleware | Does | Refuses with |
| --- | --- | --- |
| `ProxyHeadersMiddleware` | Trusts `X-Forwarded-*` only from `TRUSTED_PROXIES` | |
| `LocaleMiddleware` | Picks the answer's language from `Accept-Language` | |
| `SecurityHeadersMiddleware` | Adds HSTS, CSP, and the other security headers | |
| `RequestContextMiddleware` | Gives the request its id, logging context, and trace | |
| `DeadlineMiddleware` | Starts the `REQUEST_TIMEOUT` clock; work still running when it passes is cancelled | 503 `deadline_exceeded` |
| `ConcurrencyLimitMiddleware` | Sheds requests past `MAX_CONCURRENT_REQUESTS` | 503 `overloaded` |
| `AllowedHostsMiddleware` | Checks `Host` against `ALLOWED_HOSTS` | 400 `invalid_host` |
| `AuthenticationMiddleware` | Runs the authentication classes; sets the user and the actor | 401 (a bad credential) |
| `RateLimitMiddleware` | `RATE_LIMIT` per caller | 429 |
| `WebSocketLimitsMiddleware` | Caps and clocks for WebSockets; passes HTTP through | |
| `RequestBodyLimitMiddleware` | Holds the body to the route's limit as it streams | 413 |

## 2. Route matching

`FLATTEN_ROUTERS` puts every route at the top and indexes them by their
first path segment, so a request tries only the routes that segment can
reach, not every route of every router.

## 3. Parameters

FastAPI resolves what the endpoint declares (`resources/routing/declaring.py`):

- `uow`: the resource's `unit_of_work` dependency. The unit of work is
  made here and not opened: no connection is taken yet.
- the path's id (`product_id`), typed as the aggregate's ids: a malformed
  one is 422.
- `If-Match` and `If-None-Match`, read from the request itself, at most
  4,096 characters each (422 past that), and documented in OpenAPI.
- the body, through the schema `ResourceRouter.use_case` made from the use
  case's parameters, with their markers: `{"quantity": 0}` is 422 here,
  before anything else runs ([validation.md](validation.md)).
- each `provide=` field, from its dependency (as the caller's id).

## 4. Access rules

`Require(*rules)` runs the route's rules (`ModelActions` asks
`catalog.change_product`). Without a user, 401; without the permission,
403. A route with no rule does not start in staging or production (deny by
default).

## 5. The command

`CommandRoute.handle` builds the use case's command from the body, the
provided fields, and the path's ids; the path wins over both. The command
checks its own markers when made, so a body schema written by hand cannot
let a value through that the use case's parameters rule out (422
`invalid_field`).

## 6. Reach

`reach.on_one` decides what the caller may touch before the use case runs:

- a `scope` (`VisibleTo`) or a parent: the aggregate is read with the
  caller's criteria first; outside them is 404, so its existence does not
  leak. The use case then runs in the same transaction.
- a rule that looks at objects (object permissions): the aggregate is read
  and the rule asked in a transaction of its own, so a rule that queries
  the database never waits for a second connection (WEB-16). The write is
  then held to the version the rule saw: a change in between is 409
  `concurrent_update` (SEC-16a-1).
- `If-Match`: the unit of work expects the aggregate at that version when
  the use case loads it; another version is 412, and a resource with
  `require_if_match` refuses a write without one (428).

A route with none of these goes straight to the use case: nothing is read
twice.

## 7. The use case

`@use_case` opens the unit of work (`async with uow`): the session opens
on the database `DATABASE_ROUTING` names for the domain. The function
loads the aggregate through its repository, calls its methods, and
returns it. Every field it sets is checked as it is set
([validation.md](validation.md)); a business rule refuses with a
`DomainError` subclass: `NotFound` is 404, `BusinessRuleViolation` 422,
`Conflict` 409. A refusal rolls the transaction back.

## 8. Commit

`uow.commit()`, in this order, all or nothing:

1. `clean()` on each aggregate the repositories saw: rules across fields
   hold for what is stored.
2. The aggregates' recorded events are collected, and durable handlers'
   and deferred tasks' outbox rows are staged in the same transaction.
3. SQL `COMMIT`. A version that moved since the load (optimistic locking)
   is 409 `concurrent_update`; a unique constraint is 409
   `duplicate_value`, naming the fields.
4. After the commit, the events are published in-process to their
   handlers. Nothing is published for work that did not commit.

## 9. The answer

`presenting.answer` shows the aggregate through the resource's schema,
hiding the fields a `FieldPolicy` keeps from the caller. A change answers
200 with an `ETag`; a create on the collection 201 with `Location`; a use
case declared `-> None` 204. Every refusal on the way is a JSON error
with a stable `code` (RFC 9457 `application/problem+json` with
`ERROR_FORMAT = "problem+json"`).

## 10. After the response

`runworker` takes the committed outbox rows: durable event handlers,
`uow.defer(...)` tasks, mail, notifications, and webhooks, each run in a
transaction of its own, retried with backoff, and dead-lettered after its
attempts. A task the request deferred never runs if the request rolled
back.

## What it costs

Measured by the benchmark ladder (`python -m benchmarks`, PostgreSQL 16),
per request:

| Request | SQL statements | Transactions | Python calls |
| --- | --- | --- | --- |
| A write through a hand-written route | 1 | 1 | 2,141 |
| A read through `ResourceRouter` | 1 | 1 | 2,282 |
| A command through `ResourceRouter` | 1 | 1 | 3,008 |

Of a command's calls, domainkit's resource routing (stages 3 to 6 and 9)
makes about 40, SQLAlchemy about 690, and FastAPI most of the rest,
resolving the endpoint's parameters on every request (stage 3). A resource
route reads `If-Match` and `If-None-Match` from the request itself rather
than as FastAPI parameters, which saved 255 calls a command and 235 a read,
a setting read once is kept on `settings`, which saved about 90 more, and
the domain that owns a unit of work is worked out once, not per request;
the budgets in `benchmarks/budgets.toml` hold the gain.
