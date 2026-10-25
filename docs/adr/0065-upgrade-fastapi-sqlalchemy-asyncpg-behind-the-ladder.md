# 0065. Upgrade FastAPI, SQLAlchemy, and asyncpg behind the ladder, with FastAPI's telemetry off

- Status: Accepted
- Date: 2026-10-21

## Context

domainkit 0.8.0 held FastAPI at `>=0.141.1,<0.142.0`, SQLAlchemy at
`>=2.0.54,<2.1` and asyncpg at `<0.32`; Starlette came through FastAPI
(1.8.0 in the lock), whose own floor is 0.46. On 21 October 2026 PyPI had
FastAPI 0.158.2, Starlette 1.9.0, SQLAlchemy 2.1.6 and asyncpg 0.32.0
(re-audit ASYNC-R18, FX-18). FastAPI made fifteen minor releases between
29 September and 21 October, eight of them in the last two days, several
of them breaking.

The rule for a target: the newest release public for a week, unless a
newer one fixes something domainkit hits.

- **FastAPI 0.143.1** (14 October). 0.143.2 to 0.143.5 fix the frontend
  fallback, JSONL and SSE streams of non-finite floats, and `bytes` file
  lists, none of which domainkit uses (its event streams are its own);
  0.144 to 0.158 add features and change how forms, defaults and `null`
  bodies validate, which would change projects' APIs.
- **Starlette 1.8.0** (13 October), the version already locked. 1.9.0
  (18 October) is too new, and fixes nothing domainkit hits.
- **SQLAlchemy 2.1.4** (7 October). 2.1.5 adds `unmapped_column` for
  declarative mappings (domainkit maps imperatively); 2.1.6 fixes
  regressions in `with_loader_criteria` and `.params()`, which domainkit
  does not call.
- **asyncpg 0.32.0** (6 October). **Alembic 1.20.0** stays: migrations,
  `makemigrations` and squashing pass on SQLAlchemy 2.1.4.

What the release notes change for domainkit, and what the suite found:

- FastAPI 0.142 adds native OpenTelemetry, on by default, and depends on
  `opentelemetry-api`. With an SDK provider configured, as reference.md
  tells deployments to do, every request gets a server span that records
  the query string, operation spans, request metrics, and log records of
  validation failures and unhandled exceptions with their messages and
  stack traces. domainkit keeps query strings out of its access log and
  scrubs what it reports; this path would bypass both.
- FastAPI 0.143.1 resolves an included router's routes in
  `iter_route_contexts()`: the context now answers for a WebSocket, a
  Starlette route or a mount as served, and the attribute domainkit read
  the full path from is gone. The deny-by-default listing (ADRs 0017,
  0021) and the blocking-code check listed such routes without their
  prefix.
- FastAPI still nests included routers (`_IncludedRouter`), and a request
  still walks the routes of each one it passes: `/hello`, behind the 23
  routes `domainkit init` mounts first, costs 12 router matches, 48 route
  matches and 1,668 calls a request, where plain FastAPI's costs 234.
- SQLAlchemy 2.1 deprecates `Result.tuples()` (every audited write raised,
  with warnings as errors); `TypeEngine.python_type` answers `object` for
  types it cannot tell, where 2.0 raised `NotImplementedError` (fixtures
  would no longer have validated `UTCDateTime` and `ExactDecimal` values);
  `select(func.count())` is typed `Select[int]`; the session flushes before
  Core statements too; asyncpg's DETAIL moves from the error's text to its
  `detail` attribute (domainkit already clears it there); `Computed()`
  without `persisted` is VIRTUAL on PostgreSQL 18 and warns on older
  servers; and `postgresql://` without a driver means psycopg 3.
- asyncpg 0.32 changes its own pool's `min_size`, which SQLAlchemy does
  not use, and asks for `sslmode` or an `SSLContext` before direct TLS,
  which domainkit does not use.

Measured with the ladder (ADR 0064), 0.8.0 on the old pins against 0.8.0
on these (Python 3.13, uvloop), per request: the same statements,
transactions, checkouts and pings on every rung; 15 more Python calls on
every domainkit rung without a database (FastAPI's telemetry hooks, asked
even when off), 66 more on plain FastAPI (its telemetry, on); and 543 to
692 fewer on every database rung (SQLAlchemy 2.1 spends fewer calls on
statement cache keys, and its results are compiled code).

## Decision

- Require `fastapi (>=0.143.1,<0.144.0)`, `sqlalchemy[asyncio]
  (>=2.1.4,<2.2)`, and `asyncpg (>=0.32.0,<0.33.0)` in the `postgres`
  extra; list `starlette (>=1.8.0,<2.0.0)` itself, since domainkit imports
  it and FastAPI's floor is 0.46.
- `get_asgi_application()` turns FastAPI's telemetry off (tracing,
  metrics and logs), as it was before 0.142. Deployments that want
  request spans run under `opentelemetry-instrument`, as reference.md said
  already.
- It is off in the FastAPI applications mounted in domainkit's too, which
  keep FastAPI's defaults otherwise. The application it returns (a
  `FastAPI` subclass) sets `scope["fastapi.telemetry"]` to None on each
  HTTP and WebSocket request before any middleware sees it, the mark
  FastAPI sets itself for a request an enclosing application measures or
  excludes, and FastAPI skips its telemetry for a request with that key
  (0.143.1 and 0.158.2 read it so). A middleware would do the same, but a
  project's `MIDDLEWARE` could leave it out.
- Fix what the upgrade breaks, where it breaks, and nothing else: the
  route walkers read the route as served from FastAPI's context; the
  `python_type` readers take `object` as unknown; counts use
  `scalar_one()`; `Result.tuples()` goes.
- Set the ladder's budgets from 0.8.0 on these versions, so CI holds what
  the upgrade costs and the performance branch lowers them.

## Consequences

- Every install of domainkit now brings `opentelemetry-api` (through
  FastAPI). The `tracing` extra keeps its floor.
- Nothing in domainkit's API changes. Projects meet SQLAlchemy 2.1 in
  their own code: `Result.tuples()` warns, rows are typed `Row[int, str]`,
  `Computed()` needs `persisted=True` to stay stored on PostgreSQL 18, and
  a session flushes before `text()` statements. CHANGELOG lists these.
- The route walkers need FastAPI 0.143.1 or later: on 0.141 they would
  list included WebSockets and mounts without their prefix again.
- FastAPI costs 15 calls a request more on every route, and keeps nested
  routers; flattening them in domainkit (PERF-04) saves far more.
- Database paths cost 5–14% fewer Python calls. The authenticated reads
  and the 429 took 12–21% less CPU in each of three interleaved runs; the
  writes' difference was within noise (docs/performance.md).

## Amendment (2026-10-25)

ADR 0075 widens the FastAPI range to `>=0.143.1,<1.0.0`, and asyncpg's to
`<1.0.0`: ranges now run to the next major, and a `newest` CI job tests
the newest releases they allow. The suite and the ladder passed on FastAPI
0.162.0. The floors, the telemetry decision and the SQLAlchemy range
(`<2.2`) stand.
