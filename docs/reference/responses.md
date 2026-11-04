# Errors, JSON responses, and the API surface

This page is about what a client sees: the body of every error and its
code, how JSON answers are written, and the parts of the OpenAPI schema
that clients build on (operation ids, versions, and routes). Clients
depend on all of these, so each one is stable across releases.

On this page you find:

- the error body, in both formats, and which exception gives which
  status and code;
- how `JSONResponse` writes JSON, and when to use it;
- operation ids, and how `openapi --diff` finds breaking changes;
- versioned routers and deprecation headers;
- how the routers that `ROOT_ROUTER` includes are served.

## Errors

Every error response is JSON. With the default
`ERROR_FORMAT = "problem+json"`, every error, the middleware's included,
is RFC 9457 problem details, as `application/problem+json`:

```python
from domainkit.http import get_asgi_application
from domainkit.testing import TestClient

with TestClient(get_asgi_application(), base_url="http://localhost") as client:
    print(client.get("/nowhere").text)
    print(client.post("/auth/login", json={"username": "ann"}).text)
```

```text
{"type":"about:blank","title":"Not Found","status":404,"detail":"Not Found","instance":"/nowhere","code":"not_found","request_id":"9d319f0bc63345f5fdcf152e7013bd03"}
{"type":"about:blank","title":"Unprocessable Content","status":422,"detail":"The request is invalid.","instance":"/auth/login","code":"validation_error","request_id":"fe68e4db0d79428e9c1a4006b4374b02","errors":[{"loc":["body","password"],"msg":"Field required","type":"missing"}]}
```

A domain's error looks the same, with its own code and message:

```json
{
  "type": "about:blank",
  "title": "Not Found",
  "status": 404,
  "detail": "No invoice has that id.",
  "instance": "/invoices/7",
  "code": "invoice_not_found",
  "request_id": "6f1c7e6a7d4b4c559f0e1f2a3b4c5d6e"
}
```

[problems.md](problems.md) lists every code the framework answers with.

### The fields of an error

| Field | What it holds |
| --- | --- |
| `type` | `about:blank`, or, with `ERROR_TYPE_BASE_URL`, that URL plus the code. |
| `title` | The status in words; with `ERROR_TYPE_BASE_URL`, the code in words. |
| `status` | The HTTP status. |
| `detail` | A message for people, in the request's language. It may change between releases. |
| `instance` | The request's path, without its query. |
| `code` | The machine-readable [problem code](../learning.md#words). It never changes: clients switch on it. |
| `request_id` | The request's id, as in `X-Request-ID` and the logs. |
| `errors` | For validation errors and for values a constraint refused: a list of `loc` (where), `msg` (what), and `type`. |

`code`, `request_id`, and `errors` are extension members, so clients can
switch on `code` in either format. Submitted values are never echoed
back. With `LocaleMiddleware`, `detail` and each `msg` are in the
request's language; `code` and `type` never change ([i18n.md](../i18n.md)).

With `ERROR_TYPE_BASE_URL` each code is a problem type of its own. Serve
a page at that URL that describes each code:

```text
{"type":"https://errors.example.com/not_found","title":"Not found","status":404,"detail":"Not Found","instance":"/nowhere","code":"not_found","request_id":"3d2c1d4006cffda04448dc848c9a32e2"}
```

With `ERROR_FORMAT = "domainkit"`, an error is the
`{"detail", "code", "request_id"}` body alone (plus `errors`), as
`application/json`:

```text
{"detail":"Not Found","code":"not_found","request_id":"0a5bbfed9135a4198eb522802eb15a3e"}
```

OpenAPI documents the format in use. See ADR 0032.

### Raising an error

Code below the HTTP layer raises its own error, and the table below maps
it to a status. A route that must answer a status of its own raises
`APIError`:

```python
from domainkit.http import APIError

raise APIError(409, "This order was shipped already.", code="order_shipped")
```

`APIError(status_code, detail, *, code, headers=None, errors=None)`
takes the status, the message, the code, and optional headers and field
errors.

### Statuses and codes

| Raised | Status | `code` |
| --- | --- | --- |
| `NotFound` | 404 | from the class, such as `invoice_not_found` |
| `Conflict`, `ConcurrentUpdate`, `DuplicateValue`, `LockNotAvailable` | 409 | from the class; a unique constraint may name its own (see [databases.md](../databases.md#unique-values)). A `DuplicateValue` adds one entry to `errors` per field, `loc` `["body", <field>]`; `lock_not_available` for `get_for_update(nowait=True)` ([queries.md](../queries.md#row-locks)) |
| `BusinessRuleViolation` | 422 | from the class |
| `PermissionDenied` | 403 | from the class |
| other `DomainError` | 400 | from the class |
| `domainkit.http.APIError(status, detail, code=…)` | as given | as given |
| `domainkit.exceptions.ServiceBusy(message, retry_after=…)`: something the operation needs is busy, raised below the HTTP layer (a full hashing queue is `PasswordChecksBusy`) | 503 | from the class (`password_checks_busy`), with `Retry-After` |
| `domainkit.exceptions.UpstreamError`: an outgoing call failed (`domainkit.http.client`'s `CircuitOpen`, `CallTimeout`, `ResponseTooLarge`) | 503, 504, 502 | `upstream_unavailable` with `Retry-After`, `upstream_timeout`, `upstream_response_too_large`; the detail names no host, and nothing is reported ([runtime.md](../runtime.md#outgoing-http-calls)) |
| over `RATE_LIMIT`, a route's `Throttle`, or a throttle of `domainkit.contrib.auth` | 429 | `rate_limited`, with `Retry-After` |
| `domainkit.money.MoneyError`: amounts of two currencies added or compared, an amount with more places than its currency, an unknown currency, money set on an attribute whose column cannot hold it | 422 | `currency_mismatch`, `inexact_amount`, `unknown_currency`, `amount_out_of_range`; the detail names no amount ([money.md](../money.md#over-http)) |
| request validation | 422 | `validation_error` |
| a cursor this API did not issue for the request's ordering, or one that was altered | 422 | `invalid_cursor` ([queries.md](../queries.md#pages)) |
| a filter a `FilterSet` does not offer, given twice, or over its bounds | 422 | `invalid_filter` ([queries.md](../queries.md#filters-over-http)) |
| `PreconditionFailed`: a write whose `If-Match` names another version than the stored one | 412 | `precondition_failed` ([resources.md](../resources.md#versions-and-conditional-requests)) |
| a write without `If-Match` (or with `If-Match: *`) to a resource that requires it | 428 | `precondition_required` |
| a partial change of a field the caller may not change (`FieldNotEditable`) | 403 | `field_not_editable` ([resources.md](../resources.md#field-policies)) |
| no pooled connection within `POOL_TIMEOUT`, or a statement cancelled by `STATEMENT_TIMEOUT` or `LOCK_TIMEOUT`, or lost to a deadlock or a serialization failure | 503 | `database_busy`, with `Retry-After: 1`; logged as a warning, not reported |
| a value the database refuses (SQLSTATE class 22: a NUL character, text too long, a number out of range) | 422 | `invalid_value`; logged as a warning, not reported |
| a foreign key (SQLSTATE 23503, or SQLite's `FOREIGN KEY constraint failed`): the row is still referenced, or refers to one that does not exist | 409 | `referenced`; the detail says which side when the database does (PostgreSQL), and `errors` names the field of a missing reference |
| a NOT NULL column (23502) | 422 | `missing_value`, with the field in `errors` |
| a CHECK constraint (23514) | 422 | `check_violation`, with the fields of the columns it names |
| a unique constraint outside a unit of work (23505) | 409 | as `DuplicateValue` |
| a request past its deadline (`REQUEST_TIMEOUT`, or the route's `RequestTimeout`) | 503 | `deadline_exceeded`, with `Retry-After: 1`; logged once, as a warning |
| no slot under `MAX_CONCURRENT_REQUESTS`: the queue was full, or the wait ran out, for a new request or for the next chunk of a body it waited for | 503 | `overloaded`, with `Retry-After: 1`; warned about at most every 10 seconds |
| a request body that took longer than `REQUEST_BODY_TIMEOUT` to arrive | 408 | `request_timeout`, with `Connection: close`; logged as a security event |
| a URL a guarded `HttpClient` may not call, or one that leaves a client's base URL | 422 | `unsafe_url` ([runtime.md](../runtime.md#outgoing-http-calls)) |
| unexpected exception | 500 | `internal_error`; logged and sent to `ERROR_REPORTERS` |

WebSockets that the database refuses are closed with 1013 (busy) or 1007
(a value it refuses).

### One root for every exception

Every exception that domainkit defines is a
`domainkit.exceptions.DomainkitError`, beside the base it had
(`ValueError`, `TimeoutError`, Starlette's `HTTPException`, ...). So
`except DomainkitError` catches the framework's errors at once, and the
narrower handlers still catch what they did. Code below the HTTP layer
never raises an HTTP exception: it raises its own error, and the table
above maps it ([ADR 0084](../adr/0084-one-exception-root-mapped-to-http-at-the-boundary.md)).

### How fields are named

Fields are named as the API knows them: a column's `info={"field": ...}`,
else the attribute of the class mapped onto its table, else the column's
name. Constraint errors are logged with their SQLSTATE and the
constraint's name, never with the row's values. SQLite checks foreign
keys only on connections that turn `PRAGMA foreign_keys` on.

## JSON responses

`domainkit.http.JSONResponse` is Starlette's `JSONResponse`, with
pydantic's encoder (`pydantic_core.to_json`) for content made of JSON's
own values. It writes the same bytes, and raises the same errors:

```python
from decimal import Decimal

from domainkit.http import JSONResponse

print(JSONResponse({"count": 3, "amount": "12.50"}).body)
JSONResponse({"amount": Decimal("12.50")})
```

```text
b'{"count":3,"amount":"12.50"}'
TypeError: Object of type Decimal is not JSON serializable
```

It checks the content first (`domainkit.http.responses.json_native`):
dicts, lists, and tuples; strings, integers, floats, booleans, and None;
instances of str, int, and float subclasses and enum members, written as
`json.dumps` writes them; keys that are strings, integers, finite floats,
or booleans. Anything else goes to `json.dumps`, as in Starlette, and
fails as it failed there:

- a pydantic model passed where its `model_dump(mode="json")` belongs, a
  dataclass, a `Decimal`, a `datetime`, a `UUID`, bytes, or a set raise
  `TypeError`, a 500, and none of their fields is written;
- NaN, Infinity, and an integer longer than Python writes
  (`sys.get_int_max_str_digits()`) raise `ValueError`, before a digit is
  written;
- JSON content that pydantic would write differently (a float under
  1e-4, a `None` key, an enum whose value is not its data, a dict
  subclass's own order) is written by `json.dumps` too.

The check is Python, and it costs about what the encoding saves. A small
body encodes in under half the instructions `json.dumps` takes, a page of
50 resources in 8% fewer, and content of many small dicts (200 orders of
eight lines) in a quarter more
([performance.md](../performance.md#json-responses-and-logs)).
domainkit's own bodies (resource items, pages, and batches, error bodies,
readiness answers) are built JSON-native, skip the check, and encode in
half the time (ADR 0063). Routes that answer large bodies are cheapest
with a response model, which FastAPI writes with pydantic directly.

### When to use it

Routes with a response model (a return annotation, or `response_model=`)
are encoded by pydantic in FastAPI already. Leave them FastAPI's default:
any response class you set turns that off, and changes how their floats
are written. Give `JSONResponse` to routes without a response model, or
build it from JSON content:

```python
from fastapi import APIRouter

from domainkit.http import JSONResponse

router = APIRouter()


@router.get("/totals", response_model=None, response_class=JSONResponse)
async def totals() -> dict[str, object]:
    return {"count": 3, "amount": "12.50"}
```

FastAPI passes such a route's value through `jsonable_encoder` first,
which costs more than encoding it (about 1 ms for 50 resources). A
response model skips both, but writes a `Decimal` as a string, where
`jsonable_encoder` makes a float of one returned outside a model.

## The API surface

### Operation ids

FastAPI makes each operation id from the route's name, path, and method
(`list_products_api_v1_products_get`). So moving a router to another
prefix renames every method of the generated clients. With
`OPENAPI_OPERATION_IDS = "route_name"` (the default), the id is the
route's name alone: its function's name, or `name=`. Names must then be
unique. `check` lists clashes (`openapi.E001`), and staging and
production refuse to start with any. A route with several methods has one
id, as FastAPI makes it (and warns). See ADR 0031.

### Breaking changes: `openapi --diff`

`python manage.py openapi` prints the schema, and
`openapi --output openapi.json` writes it. `openapi --diff openapi.json`
lists what changed since in ways that break clients, and exits 1 if
anything did:

```bash
python manage.py openapi --output openapi.json
python manage.py openapi --diff openapi.json
```

It counts as breaking:

- a path or an operation removed, or a success status no longer answered
  (or no longer answered with JSON);
- a new required parameter, request body, or body field;
- a request value accepted before and refused now: a narrower type, fewer
  enum values, a tighter bound, or fields that a closed object no longer
  takes;
- a response field removed or no longer always present, or one that may
  now hold another type or enum value.

Renaming a path parameter breaks nothing. Run it in CI against the schema
of the last release.

The old schema must be a JSON file of at most 64 MiB, whose arrays and
objects nest at most 256 deep (`MAX_SCHEMA_BYTES`, `MAX_SCHEMA_DEPTH`).
Other files are refused before they are parsed.

### Versions and deprecations

`domainkit.http.versioning` serves several versions of an API, and marks
old ones as deprecated:

```python
from datetime import UTC, datetime

from fastapi import APIRouter, Depends

from domainkit.http.versioning import Deprecation, versioned_router

router = APIRouter()
products_v1 = APIRouter()
products_v2 = APIRouter()

V1_SUNSET = Deprecation(
    since=datetime(2026, 11, 1, tzinfo=UTC),
    sunset=datetime(2027, 5, 1, tzinfo=UTC),
    link="https://api.example.com/docs/migrate-to-v2",
)
v1 = versioned_router("v1", deprecation=V1_SUNSET)  # Served under /v1.
v1.include_router(products_v1)
v2 = versioned_router("v2")
v2.include_router(products_v2)


@router.get("/old-report", dependencies=[Depends(V1_SUNSET)])
async def old_report() -> dict[str, str]:
    return {"report": "monthly"}
```

Every response of a deprecated route, errors included, then carries
three headers:

```text
Deprecation: @1793491200
Sunset: Sat, 01 May 2027 00:00:00 GMT
Link: <https://api.example.com/docs/migrate-to-v2>; rel="deprecation"
```

| Name | What it does |
| --- | --- |
| `versioned_router(version, *, deprecation=None, dependencies=(), **options)` | An `APIRouter` served under `/<version>`. A version's operation ids are the version and the route's name (`v1_list_products`), whatever `OPENAPI_OPERATION_IDS` says, so versions can share route names. |
| `Deprecation(*, since, sunset=None, link=None)` | A deprecation, on a router, an `include_router` call, or a route (`Depends(...)`). The most specific one wins. Times need a time zone; a date means midnight UTC. A sunset must follow `since`. |
| `api_version(request)` | The version that serves this request (DRF's `request.version`): `"v1"` under `/v1`, `None` outside every versioned router. |

The headers are `Deprecation: @<seconds>` (RFC 9745),
`Sunset: <HTTP date>` (RFC 8594), and `Link: <...>; rel="deprecation"`.
OpenAPI marks the operations `deprecated`, with the headers on each
response, `x-deprecated-since`, `x-sunset`, and the link as
`externalDocs`.

The version is the URL's alone: no header or query parameter chooses it.
So a client, a cache, and the logs always agree on which version
answered.

### Included routers

`get_asgi_application` serves the routes of the routers that
`ROOT_ROUTER` includes from the top of the application (ADR 0060).
FastAPI keeps each `include_router` call nested, and matches each request
again at each level. Every route keeps what its routers gave it: the full
path, the dependencies and access rules, tags, responses, response class,
route class, callbacks, and OpenAPI settings. Mounts stay mounts, the
routers' lifespans and startup hooks run, and the OpenAPI document is the
same, byte for byte.

What a project can see:

- `app.routes` lists the routes themselves, with their full paths, in the
  order they are matched;
- `request.scope["route"].path` is the route's full path template;
- a router with frontend routes (`APIRouter.frontend`) stays included;
- a router that serves requests in its own way stays included, with the
  prefixes and rules of the routers above it, in its place. That is a
  router whose class (or the router itself) has its own `matches`,
  `handle`, or any other method of `APIRouter` or Starlette's `Router`,
  except `__init__` and those that declare routes. An IP gate in `handle`
  or a host rule in `matches` holds as under FastAPI's nesting; the
  routers around it are still flattened;
- a route added to an included router after the application is built is
  not served: finish the routers before `get_asgi_application` runs.

The application's router then tries only the routes a request's path can
reach (0.10): those whose template starts with the path's first segment,
and those that may match any path (a parameter in the first segment, a
mount at the root, a host, a router left included), in order. It serves
the route that trying every route would serve, whatever the number of
routes ahead of it. Order still decides between routes of one segment.

`FLATTEN_ROUTERS = False` keeps FastAPI's nesting, and its matching. Only
the application that `get_asgi_application` builds is flattened and
indexed. The code that does it rests on FastAPI's internals, so it is not
public (it was `domainkit.http.routing` until 1.0), and an application
built by hand keeps FastAPI's nesting.

## See also

- [Problem codes](problems.md): every code, with its status.
- [HTTP settings](http.md): `ERROR_FORMAT`, `ERROR_TYPE_BASE_URL`, and
  `OPENAPI_OPERATION_IDS`.
- [Versioning](../versioning.md): what is public, and how it changes.
