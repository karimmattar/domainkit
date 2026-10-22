# 0063. Encode JSON with pydantic where the bytes stay the same, and let projects sample the access log

- Status: Accepted
- Date: 2026-10-21

## Context

The performance re-audit found two JSON costs on every request (PERF-08,
PERF-02):

- The JSON responses domainkit builds itself (resource pages, items, and
  batches, error bodies, readiness answers) are Starlette `JSONResponse`s:
  `json.dumps` with compact separators, text in UTF-8 as it is, NaN and
  Infinity refused. On a page of 50 resources that is about 180 µs.
- The JSON access log cost about 50 µs a request. 0.8.0 moved the writing
  to a thread (ADR 0051), but measured again it costs as much: the event
  loop still builds the record, runs the context filter, copies the record
  for the queue (`copy.copy`), and hands it to the thread, which formats it
  with `json.dumps` (7.4 µs a line) and writes it.

Clients and log pipelines parse these bytes, ETags are taken over response
bodies, and domainkit is used for money: not one byte may change unless a
setting says so.

How FastAPI 0.141.1 writes a route's value:

- With a response model (a return annotation, or `response_model=`), it
  validates the value and writes it with pydantic's `TypeAdapter.dump_json`,
  in Rust, into a plain `Response`; but only while the route's response
  class is FastAPI's default placeholder.
- Without one, it walks the value with `jsonable_encoder` (in Python; it
  makes a float of a bare `Decimal`), then calls the response class's
  `render`: Starlette's `json.dumps`.
- A response class set anywhere (the application's
  `default_response_class`, a router, a route) turns the first path off:
  such routes get `dump_python(mode="json")` and the class's `render`. That
  changes their bytes: pydantic writes `1e-7` where `json.dumps` writes
  `1e-07`, and NaN as `null` where `json.dumps` refuses it, so they answer
  500. A `Default(...)` placeholder at the application is ignored.

The candidates, in µs per encode of the dicts `model_dump(mode="json")`
gives (best of 7):

| Payload | `json.dumps` | `pydantic_core.to_json` | orjson 3.13.0 | msgspec 0.22.0 |
| --- | ---: | ---: | ---: | ---: |
| `{"status": "ok"}` | 2.0 | 0.3 | 0.3 | 0.1 |
| an error body | 4.8 | 0.9 | 0.6 | 0.5 |
| one resource | 7.0 | 1.6 | 0.7 | 0.8 |
| a page of 50 resources (20 KB) | 199 | 44 | 21 | 18 |
| 200 orders with lines (159 KB) | 1,229 | 480 | 245 | 167 |

`TypeAdapter.dump_json` writes models, not these dicts: it is FastAPI's
own path. None of the candidates writes exactly what `json.dumps` writes.
Over 5 million floats, `to_json` differs only from 1e-9 up to 1e-4 (`1e-7`
and `0.00005` for `1e-07` and `5e-05`). It also writes NaN and Infinity
instead of refusing them, a `None` key as `"None"` instead of `"null"`, and
encodes values JSON has no type for. orjson also refuses integers over 64
bits and float subclasses, and writes NaN as `null`; msgspec writes `1e16`
for `1e+16`.

## Decision

- **`encode_json`** (`domainkit.http.responses`) writes with
  `pydantic_core.to_json`, then searches the bytes for the forms
  `json.dumps` writes otherwise or refuses: a one-digit negative exponent
  at the end of a number, a number that starts `0.0000`, `NaN`, `Infinity`,
  and a `"None"` key. When one is there, or when `to_json` refuses the
  content, it encodes with `json.dumps` itself. JSON content (what
  `jsonable_encoder` and `model_dump(mode="json")` give) gets Starlette's
  bytes or Starlette's error. A string that only looks like one of those
  forms costs a second encoding, never a wrong one. Single-byte searches
  go first, as they cost almost nothing, and after 16 strings that look
  like a tiny number the check stops and `json.dumps` encodes: text
  crafted to be all lookalikes costs at most about twice what `json.dumps`
  alone costs (a megabyte of `e-1x`: 8.4 ms instead of 3.6), never a
  search through each of them in Python.
- **`domainkit.http.JSONResponse`** renders with it, and domainkit's own JSON
  responses use it: resource pages, items, and batches (their ETags, taken
  over the bytes, stay the same), error bodies, readiness answers.
- **No default response class, and no setting for one.** In FastAPI 0.141.1
  it would take pydantic's path from every route with a response model and
  change its bytes. Projects give `response_class=JSONResponse` to routes
  without a response model, or build it by hand.
- **No optional extra.** orjson and msgspec are faster still (about 25 µs
  less than `to_json` on the page of 50), but they differ from `json.dumps`
  in more places, so they would need the same fallbacks, and the default
  path would still need its own.
- **`JsonFormatter`** writes the same line, field for field and byte for
  byte, by joining strings: the escaping `json.dumps` uses for text, the
  encoder `json.dumps` builds for any other value, and a timestamp whose
  date and time are worked out once a second and rounded as `datetime`
  rounds. **`BackgroundHandler.prepare`** copies a plain `LogRecord` field
  by field, without `copy.copy`'s generic machinery.
- **Access-log sampling**, off by default: `ACCESS_LOG_SAMPLE_RATE` (1.0,
  every line) and `ACCESS_LOG_SLOW_DURATION` (1 second). Below 1.0, the
  lines of every response from status 400 up (client errors, refusals, and
  server errors), of requests that got no response, and of requests that
  took the slow duration or longer are always written; each other line (a
  success or a redirect) is written with probability rate, drawn per
  request with `random.random`. The middleware checks the logger's level,
  then the sample, before it builds anything. Only `domainkit.access` is
  sampled: `domainkit.security`, the other loggers, and the audit trail
  (a table, not a log) never are.

Rejected:

- Taking the routes without a response model off `jsonable_encoder`: it
  is most of their cost, but pydantic writes a bare `Decimal` as a string
  where `jsonable_encoder` makes it a float, which clients see.
- Checking the content's types with a strict JSON schema in pydantic, to
  make `encode_json` refuse what `json.dumps` refuses: it takes 300 µs on
  the page, more than `json.dumps`.
- One line in N instead of a draw: periodic traffic biases it, and a
  caller who knows N can keep their requests out of it.
- Sampling refusals, or `domainkit.security`: those are the lines an
  operator reads during an attack.
- Sampling client errors other than 401, 403, and 429, as the first
  version did. A scoped resource answers 404 for what is out of the
  caller's reach, so at a low rate a caller trying other tenants' ids, one
  after another, left almost no trace; 400, 405, 409, and 422 are probes'
  answers too. They are few in a healthy service, so keeping them all
  costs little, and they are what an investigation needs.
- Refusing a low sample rate in staging and production: it never drops
  errors, refusals, or security events, so it stays the operator's choice.

## Consequences

- Responses keep their bytes, and get cheaper: a page of 50 resources
  encodes in 991,051 instructions instead of 1,939,596 (69 µs instead of
  179), and a request that answers it costs 1,302,835 instead of
  2,155,451; an error body encodes in 22,438 instead of 38,678.
- The JSON access line costs about 154,000 instructions a request instead
  of 181,000 (15% fewer), on the default middleware and on the re-audit's
  `init` template stack alike; the formatter is 35% of a line cheaper, the copy 39%.
  With `ACCESS_LOG_SAMPLE_RATE = 0.1` it costs about 18,000.
- `encode_json` differs from Starlette for content outside JSON's values,
  which neither domainkit nor `jsonable_encoder` nor `model_dump(mode="json")`
  gives it: values JSON has no type for are encoded (a `Decimal` as a
  string, never a float; a `datetime` in pydantic's ISO 8601), so are keys
  of other types, NaN or infinite keys, and integers over 4,300 digits, and
  a dict subclass that overrides `items()` is written in its stored order.
- It depends on FastAPI 0.141.1's choice between `dump_json` and
  `jsonable_encoder`; `test_routes_with_a_response_model_are_left_to_fastapi`
  pins it, so a FastAPI upgrade that changes it fails there. It depends on
  pydantic-core's float format and string escaping; the byte-for-byte
  corpus and the property tests fail, rather than the clients, if a
  pydantic-core release changes them.
- With sampling on, counting requests from the access log means counting
  each sampled line `1 / rate` times; the metrics still count every
  request.
- What an access line still costs is mostly the standard library's: the
  `LogRecord`, the handler's lock, the queue's condition, the hand-off to
  the thread, and the write; sampling is the way past it.
