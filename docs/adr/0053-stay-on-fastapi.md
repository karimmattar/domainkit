# 0053. Stay on FastAPI and Starlette

- Status: Accepted
- Date: 2026-10-20

## Context

domainkit serves its HTTP API through FastAPI, on Starlette. The 0.6.2
re-audit asked whether a faster framework should replace it before 1.0
(R16). The two candidates are Litestar, which validates and serializes
with msgspec and has its own dependency injection, and plain Starlette,
which FastAPI is built on.

What a request costs, measured in process without a server (Python 3.13,
FastAPI 0.141.1, Starlette 1.8.0, Litestar 2.18.0, Pydantic 2.14.1,
msgspec 0.22.0): the fastest of nine rounds of 10,000 requests, in two
runs on a 4-core Linux container with little else running:

| Request | Starlette | FastAPI | Litestar |
| --- | --- | --- | --- |
| `GET`, a small JSON object | 12–13 µs | 39–41 µs | 26 µs |
| `POST`, a path parameter, a validated body, an async dependency | 22–23 µs (by hand) | 92–93 µs | 62–65 µs (Pydantic), 52–54 µs (msgspec) |
| the same `POST`, its dependency a plain `def` | | 184–186 µs | |

domainkit's own `GET /health`, through its whole middleware stack, took
107–114 µs the same way, and one `SELECT 1` through SQLAlchemy on a local
PostgreSQL took 123–127 µs. The re-audit found where an authenticated
request spends its time: a token lookup and the user with groups and
permissions (4–5 queries), sync dependencies sent to the thread pool
(`current_user`), logging written on the event loop, and 2–3 extra
transactions for an idempotency key. One query, or one hop to the thread
pool (about 90 µs above an async dependency, in the table), costs more
than FastAPI does over Litestar (15–40 µs a request); logging blocks the
loop whenever stdout does. None of these goes away with another framework.

What a move would cost:

- FastAPI is in 41 of the framework's 309 modules, and Starlette in 24.
  Deny by default (ADRs 0017 and 0021) and the blocking-code check read
  FastAPI's dependency tree (`Dependant`, `iter_route_contexts`);
  operation ids (ADR 0031), problem details (ADR 0032), OpenAPI, the
  admin API, resources, and the test client build on FastAPI's.
- Every generated project, and every project already made, writes routes
  with `APIRouter` and `Depends`. Litestar's handlers, injection, and
  DTOs differ throughout, so each project would be rewritten by hand.
- Plain Starlette has no injection, validation, or OpenAPI. domainkit
  would build those itself, and become the framework it chose not to be.
- FastAPI has the larger ecosystem: more users who know it, and more
  libraries, guides, and answers. A framework for teams is easier to adopt
  on what teams already know.

## Decision

- Stay on FastAPI and Starlette.
- Make domainkit's own costs cheap instead: `check` flags sync handlers
  and dependencies, `acurrent_user` replaces the thread-pool hop for
  `CurrentUser`, logs and error reports leave the loop (ADR 0051), and
  the performance branch caches the principal (R13), upgrades FastAPI
  (R18), and measures requests in CI (R17).
- Where serialization matters, routes may return a `Response` with
  bytes that msgspec or orjson encoded; that needs no new framework.

## Consequences

- Projects keep FastAPI's routes, dependencies, and documentation, and
  domainkit keeps reading FastAPI's dependency tree, so a FastAPI release
  that changes it needs a domainkit release.
- A request spends 15–40 µs more in the framework than it would on
  Litestar. Once the principal is cached, that share grows, and the
  benchmarks in CI (R17) will show by how much.
- What would change this:
  - those benchmarks showing FastAPI's own routing and validation as a
    large share of typical requests, say a third of their time, after
    domainkit's own costs are gone;
  - FastAPI falling behind new Python or Pydantic releases, or dropping
    the dependency tree that the checks read, with no replacement;
  - a migration path for projects, such as Litestar running FastAPI-style
    routes, that makes a move cheap for the people using domainkit.
