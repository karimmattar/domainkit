# The request flow

Every change a client asks for takes the same path through domainkit.
This page follows one request, `POST /products/{product_id}/restock`,
from the socket to the answer, and then after the answer. Each stage
names the code that runs it, and what can refuse the request there.
When an answer surprises you, this page tells you which stage gave it.

On this page you learn:

- the 10 stages of a request, in order;
- what each stage checks, and the status and `code` it refuses with;
- when the [unit of work](learning.md#words) opens, commits, and
  publishes events;
- what runs after the answer, in the worker;
- what a request costs, in SQL statements and Python calls.

## The request

The route comes from a `@use_case` and a resource
([resources.md](resources.md#use-cases)):

```python
@use_case
async def restock(
    uow: CatalogUnitOfWork,
    product_id: UUID,
    quantity: Annotated[int, Range(min=1, max=1000)],
) -> Product:
    """Add items to a product's stock."""
    product = await uow.products.get(ProductId(product_id))
    product.restock(quantity)
    return product


products.use_case(restock, path="/restock")
```

A client sends it a request, and the route answers with the product and
its new `ETag`:

```text
POST /products/7f3c.../restock
If-Match: W/"4-..."
{"quantity": 3}

200 OK
ETag: W/"5-..."
{"id": "7f3c...", "version": 5, "name": "Lamp", "stock": 3}
```

## The stages

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

The middleware is pure ASGI. It runs in the order `MIDDLEWARE` lists it.
Each one sees the request before the next one does, and the response
after it. These are the 11 that the project template installs:

| Middleware | What it does | Refuses with |
| --- | --- | --- |
| `ProxyHeadersMiddleware` | Trusts `X-Forwarded-*` only from `TRUSTED_PROXIES`. | |
| `LocaleMiddleware` | Picks the answer's language from `Accept-Language`. | |
| `SecurityHeadersMiddleware` | Adds HSTS, CSP, and the other security headers. | |
| `RequestContextMiddleware` | Gives the request its id, its logging context, and its trace. | |
| `DeadlineMiddleware` | Starts the `REQUEST_TIMEOUT` clock. Work still running when it passes is cancelled. | 503 `deadline_exceeded` |
| `ConcurrencyLimitMiddleware` | Sheds requests past `MAX_CONCURRENT_REQUESTS`. | 503 `overloaded` |
| `AllowedHostsMiddleware` | Checks `Host` against `ALLOWED_HOSTS`. | 400 `invalid_host` |
| `AuthenticationMiddleware` | Runs the authentication classes, and sets the user and the actor. | 401 (a bad credential) |
| `RateLimitMiddleware` | Counts requests per caller, against `RATE_LIMIT`. | 429 |
| `WebSocketLimitsMiddleware` | Caps and clocks WebSockets. HTTP passes through. | |
| `RequestBodyLimitMiddleware` | Holds the body to the route's limit as it streams in. | 413; 408 for a body slower than `REQUEST_BODY_TIMEOUT` |

`DEFAULT_MIDDLEWARE`, used when a project sets no `MIDDLEWARE`, has 8 of
them: no locale, no authentication, and no rate limit.
[The HTTP reference](reference/http.md#middleware) explains each one, and
the optional ones (metrics, CORS, compression, idempotency, the debug
toolbar).

## 2. Route matching

`FLATTEN_ROUTERS` puts every route at the top level, and indexes the
routes by the first segment of their path. A request then tries only the
routes that its first segment can reach, not every route of every
router.

## 3. Parameters

FastAPI resolves what the endpoint declares
(`resources/routing/declaring.py`):

- `uow`: the resource's `unit_of_work` dependency. The unit of work is
  made here, but not opened: no connection is taken yet.
- The id in the path (`product_id`), typed as the aggregate's ids are. A
  malformed id is 422.
- `If-Match` and `If-None-Match`, read from the request itself, at most
  4,096 characters each (422 past that). OpenAPI documents them.
- The body, through the schema that `ResourceRouter.use_case` made from
  the use case's parameters, with their markers. `{"quantity": 0}` is 422
  here, before anything else runs ([validation](validation.md)). A body
  that also sends `product_id` is 422: the path gives it.
- Each `provide=` field, from its dependency (the caller's id, say).

```json
{
  "type": "about:blank",
  "title": "Unprocessable Content",
  "status": 422,
  "detail": "The request is invalid.",
  "code": "validation_error",
  "errors": [
    {
      "loc": ["body", "quantity"],
      "msg": "Input should be greater than or equal to 1",
      "type": "greater_than_equal"
    }
  ]
}
```

## 4. Access rules

`Require(*rules)` runs the route's rules. `ModelActions` asks for
`catalog.change_product` here. Without a user, the answer is 401; without
the permission, 403. A route with no rule does not start in staging or
production: access is denied by default
([who may call it](resources.md#who-may-call-it)).

## 5. The command

`CommandRoute.handle` builds the use case's command from the body, the
provided fields, and the ids in the path. The path wins over both. The
command checks its own markers when it is made. So a body schema that you
write by hand cannot let through a value that the use case's parameters
rule out: that is 422 `invalid_field`.

## 6. Reach

`reach.on_one` decides what the caller may touch, before the use case
runs:

- **A `scope` (such as `VisibleTo`) or a parent.** The aggregate is read with the caller's
  criteria first. An aggregate outside them is 404, so its existence does
  not leak. The use case then runs in the same transaction.
- **A rule that looks at objects** (object permissions). The aggregate is
  read, and the rule asked, in a transaction of its own. So a rule that
  queries the database never waits for a second connection (WEB-16). The
  write is then held to the version the rule saw: a change in between is
  409 `concurrent_update` (SEC-16a-1).
- **`If-Match`.** The unit of work expects the aggregate at that version
  when the use case loads it. Another version is 412
  `precondition_failed`. A resource with `require_if_match` refuses a
  write without `If-Match` with 428 `precondition_required`.

A route with none of these goes straight to the use case. Nothing is
read twice.

## 7. The use case

`@use_case` opens the unit of work (`async with uow`). The session opens
on the database that `DATABASE_ROUTING` names for the domain. The
function loads the aggregate through its repository, calls its methods,
and returns it.

- Every field it sets is checked as it is set
  ([validation](validation.md)).
- A business rule refuses with a `DomainError` subclass: `NotFound` is
  404, `BusinessRuleViolation` is 422, and `Conflict` is 409. The `code`
  comes from the class name: `ProductNotFound` is `product_not_found`.
- A refusal rolls the transaction back.

## 8. Commit

`uow.commit()` does these steps, in this order, all or nothing:

1. `before_save()`, `clean()`, and the `Meta.checks` of each aggregate
   the repositories saw. So the rules across fields hold for what is
   stored.
2. The recorded events of the aggregates are collected. The outbox rows
   of durable handlers and deferred tasks are staged in the same
   transaction.
3. SQL `COMMIT`. A version that moved since the load (optimistic locking)
   is 409 `concurrent_update`. A unique constraint is 409
   `duplicate_value`, and names the fields.
4. After the commit, the events are published in the process, to their
   handlers. Nothing is published for work that did not commit.

## 9. The answer

`presenting.answer` shows the aggregate through the resource's schema. It
hides the fields that a `FieldPolicy` keeps from the caller.

| The route | Answers |
| --- | --- |
| A change on one aggregate | 200, with an `ETag` |
| A create on the collection | 201, with `Location` and `ETag` |
| A use case declared `-> None` | 204 |

Every refusal on the way is a JSON error with a stable `code`. By default
(`ERROR_FORMAT = "problem+json"`, since 1.0), it is an RFC 9457
`application/problem+json` body, as above.
[Problems](reference/problems.md) lists every code.

## 10. After the response

`runworker` takes the committed outbox rows: durable event handlers,
`uow.defer(...)` tasks, mail, notifications, and webhooks. It runs each
one in a transaction of its own, retries it with backoff, and moves it to
the dead letters after its last attempt. A task that the request deferred
never runs if the request rolled back ([messaging](contrib/messaging.md)).

## What it costs

The benchmark ladder (`python -m benchmarks`, on PostgreSQL 16) measures
each request:

| Request | SQL statements | Transactions | Python calls |
| --- | --- | --- | --- |
| A write through a hand-written route | 1 | 1 | 2,139 |
| A read through `ResourceRouter` | 1 | 1 | 2,359 |
| A command through `ResourceRouter` | 1 | 1 | 3,083 |

Of a command's calls, domainkit's resource routing (stages 3 to 6, and 9)
makes about 40, and SQLAlchemy about 690. FastAPI makes most of the
rest: it resolves the endpoint's parameters on every request (stage 3).

Two changes made the resource routes cheaper. A resource route reads
`If-Match` and `If-None-Match` from the request itself, not as FastAPI
parameters: that saved 255 calls for a command and 235 for a read. Each
setting is kept once read, and a unit of work works out its domain once:
that saved about 30 more. The budgets in `benchmarks/budgets.toml` keep
these gains.

## Refusals by stage

| Stage | Status and `code` | Why |
| --- | --- | --- |
| 1 | 503 `deadline_exceeded`, 503 `overloaded` | The request took too long, or the process is full. |
| 1 | 400 `invalid_host`, 401, 429 `rate_limited`, 413 `content_too_large`, 408 `request_timeout` | A bad host, a bad credential, too many requests, a body too large or too slow. |
| 3 | 422 `validation_error` | A malformed id, header, or body. |
| 4 | 401 `not_authenticated`, 403 `permission_denied` | No user, or no permission. |
| 5 | 422 `invalid_field` | A command field breaks its markers. |
| 6 | 404, 409 `concurrent_update`, 412 `precondition_failed`, 428 `precondition_required` | Out of reach, changed since the rule looked, a stale `If-Match`, or no `If-Match`. |
| 7 | 404, 409, 422 | The domain refused. |
| 8 | 409 `concurrent_update`, 409 `duplicate_value`, 422 `check_violation` | A lost race, a taken value, or a broken check. |

## Common mistakes

- **Committing twice.** A `@use_case` commits for you. Do not call
  `uow.commit()` inside it.
- **Calling an outside service inside the use case.** If the commit then
  fails, the call has still happened. Defer it to a task
  (`uow.defer(...)`), which runs only after the commit.
- **Expecting an event handler to see uncommitted work.** In-process
  handlers run after the commit. Durable ones run later, in the worker.
- **Reading a stale `ETag`.** Each change moves the version. Send the
  `ETag` of the last answer in `If-Match`, not the one from the first
  read.

## See also

- [Resources](resources.md): the routes this page follows.
- [Validation](validation.md): the markers checked at stages 3, 5, and 7.
- [Databases: transactions](databases.md#transactions): isolation, locks,
  and work run again.
- [Messaging](contrib/messaging.md): the outbox and the worker of
  stage 10.
- [Runtime](runtime.md): deadlines and load shedding.
- [HTTP reference](reference/http.md#middleware): every middleware.
