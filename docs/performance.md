# Performance: what a request costs, how to measure it, how to cut it

A domainkit request does three kinds of work: Python (routing, middleware,
validation, the framework's own checks), round trips to the database, and
the server's work around them. This page gives what each costs on the paths
a service serves most, how to measure them with the benchmark ladder, how
CI holds the costs down, and what makes a request cheaper. Server settings
and limits are in [reference.md](reference.md#server-limits); event loops,
logging and servers in [runtime.md](runtime.md).

## What a request costs

The ladder measures thirteen rungs, from a bare ASGI application to a keyed
write. Requests go straight into the application, one at a time, with no
server or socket; the database rungs run against PostgreSQL. Each rung runs
in a process of its own.

Measured on domainkit 0.8.0 with this release's dependencies (FastAPI
0.143.1, Starlette 1.8.0, SQLAlchemy 2.1.4, asyncpg 0.32.0, Pydantic
2.14.1), Python 3.13.16 on uvloop, and PostgreSQL 16 on the same 4-vCPU
Linux machine, which other work shared (load average about 3.5). Per
request; CPU is the best of three ladder runs.

| Rung | Stack and request | Calls | Statements | Transactions | Checkouts | Pings | CPU µs |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `asgi` | A bare ASGI application: `GET /hello` | 5 | 0 | 0 | 0 | 0 | 1.2 |
| `fastapi` | Plain FastAPI: `GET /hello` | 234 | 0 | 0 | 0 | 0 | 51.0 |
| `bare` | domainkit, no middleware: `GET /hello` | 1,668 | 0 | 0 | 0 | 0 | 154 |
| `nested` | domainkit, no middleware, two routers deep: `GET /a/b/hello` | 1,904 | 0 | 0 | 0 | 0 | 170 |
| `default` | `DEFAULT_MIDDLEWARE`: `GET /hello` | 1,840 | 0 | 0 | 0 | 0 | 206 |
| `default-json` | `DEFAULT_MIDDLEWARE`, JSON access log: `GET /hello` | 1,981 | 0 | 0 | 0 | 0 | 283 |
| `template` | The `init` template's stack: `GET /hello` | 2,033 | 0 | 0 | 0 | 0 | 281 |
| `template-json` | The template's stack, JSON access log: `GET /hello` | 2,173 | 0 | 0 | 0 | 0 | 370 |
| `me` | Template stack, opaque token: `GET /me` | 5,296 | 5 | 1 | 1 | 1 | 3,995 |
| `read` | Template stack, token, `ModelPermissions`, read through a unit of work: `GET /items/{id}` | 6,662 | 6 | 2 | 2 | 2 | 5,325 |
| `write` | Template stack, token, `ModelPermissions`, write through a unit of work: `POST /items` | 7,055 | 6 | 2 | 2 | 2 | 5,953 |
| `keyed-write` | The write with an `Idempotency-Key` (`IdempotencyMiddleware` last): `POST /items` | 10,428 | 10 | 5 | 5 | 5 | 10,056 |
| `throttled` | Template stack, token, refused by `RATE_LIMIT` (429): `GET /me` | 3,788 | 5 | 1 | 1 | 1 | 3,695 |

- **Calls** are the Python function calls cProfile counts per request,
  built-in ones included, in every thread: the JSON access log is
  formatted on the logging thread, and counts. A coroutine resumed after a
  wait counts again, so each layer of `await` adds a call per wait for the
  database. Every rung counts the same calls in every run, but for the two
  with a JSON access log, which vary by under 1% (the logging thread waits
  on its queue more or less often).
- **Statements, transactions, checkouts, pings** are what reaches the
  database, per request: statements sent to the driver, transactions begun,
  connections taken from the pool, and the `SELECT 1` that tests a pooled
  connection first (`POOL_PRE_PING`).
- **CPU** is the process's CPU time per request (all threads), the best of
  the timed runs. It moves by 15% or more from run to run on a shared
  machine; never read a smaller difference into it.

What the rungs show:

- **Routing is most of a light request.** `domainkit init` mounts `/health`,
  `/auth` and `/admin/auth` (23 routes) before the project's own, and
  FastAPI walks the routes of each included router a request passes:
  `/hello` behind them takes 12 router matches and 48 route matches, and
  1,180 of the `bare` rung's 1,668 calls (488 when mounted first). A route
  two routers deeper costs 236 calls more.
- **Authentication is most of an authenticated request.** `GET /me` with an
  opaque token runs five SELECTs in their own transaction: the token, the
  user (password hash included), their groups, their own permissions, and
  their groups' permissions. A permission-guarded read through a unit of
  work adds one more transaction for the row.
- **A 429 costs nearly what the request it refuses would have**:
  authentication runs before the rate limit, so the same five SELECTs run
  for a caller who is then turned away (3,788 calls and 3,695 µs, against
  `me`'s 5,296 and 3,995).
- **A keyed write costs five transactions** where a plain write costs two:
  after authentication, one deletes an expired claim of the same key,
  one inserts the claim, the write commits with the key's commit mark, and
  one stores the response; each takes its own connection and ping.

## Running the ladder

From a checkout, with the development dependencies installed:

```bash
python -m benchmarks                    # the rungs without a database
export DOMAINKIT_BENCH_POSTGRES_URL=postgresql+asyncpg://postgres@localhost:5432/postgres
python -m benchmarks                    # all of them
python -m benchmarks me read throttled  # some
```

`DOMAINKIT_BENCH_POSTGRES_URL` (or `--postgres`, which other users of the
machine can read in its process list) names a database the ladder may make
schemas in. The ladder passes it on to the processes it starts in their
environment, never on their command line. Each run creates
`domainkit_bench_<tree>_<pid>`, migrates it with the code measured, adds a
user in a group that may view and add items, a token and an item, and drops
the schema at the end. It touches nothing else, and only ever creates or
drops schemas named `domainkit_bench_...`.

It prints a table, checks the counts against the budgets (below), and
exits 1 when a rung fails or goes over budget. `--json PATH` keeps every
figure, `--summary PATH` writes the Markdown table CI shows, and
`--no-gate` only reports.

**Another commit, side by side.** The harness measures any source tree,
so it compares two commits with the same code, the rungs of each
interleaved so drift on the machine hits both:

```bash
git worktree add ../base a686177
python -m benchmarks --baseline ../base/src
# Another environment for the baseline (other dependency versions):
python -m benchmarks --baseline ../base/src --baseline-python ../base/.venv/bin/python
```

**One rung, closer.** `python -m benchmarks.rung template --profile-out
template.pstats` measures one rung and keeps cProfile's statistics of the
counted requests; `--profile-dir DIR` keeps them for every rung of a run.
Two of them, from two trees, show which functions gained or lost calls.

**Sizes.** Each rung warms up, counts one pass of requests (200 in
process, 100 with the database), then times several (5 × 2,000 in
process, 3 × 300 with the database). `--warmup`, `--count`, `--requests`
and `--repeats` change them for every rung.

## Budgets and the CI gate

`benchmarks/budgets.toml` holds, for each rung, how many calls and round
trips a request may cost:

```toml
[tolerance]
calls = 0.02        # a fraction of the budget
statements = 0      # per request: exact
transactions = 0
checkouts = 0
pings = 0

[rungs.me]
calls = 5296
statements = 5
transactions = 1
checkouts = 1
pings = 1
```

- **Over budget fails**, naming the rung, the measure, the budget and the
  value: `OVER BUDGET: me statements: budget 5, measured 6.`
- **Under budget by more than the tolerance passes with a hint**:
  `Under budget: me statements: budget 5, measured 1; lower the budget to
  lock it in.` Lower it in the same pull request, so the gain stays won.
- **Time is never gated** (ADR 0064): a shared runner's timings are too
  noisy. CI's job summary shows CPU time per rung, beside the base branch's
  on pull requests.

The `bench` job in `.github/workflows/ci.yml` runs the ladder on Python
3.13 against a PostgreSQL service on every push and pull request, with
`--strict`: a rung that did not run, or a measure without a budget, fails
too.

**Updating the budgets.** Budgets hold for CI's Python (3.13) and the lock
file: an upgrade that changes FastAPI's or SQLAlchemy's calls moves them.
After a change that should move them, run on Python 3.13 with the locked
dependencies:

```bash
python -m benchmarks --write-budgets
```

and review the diff of `budgets.toml`: every raised number is a cost the
change adds. For the two JSON-log rungs keep the highest of a few runs.

## Cores for 500 million requests a day

One process per core (each on its own event loop), from the CPU per request
above. These are lower bounds: the HTTP server's own work comes on top
(about 34 µs a request for uvicorn on uvloop with httptools, as the
re-audit measured it on this machine class), and so does the database's.

| 500 million requests per… | Requests/s | Light: `default-json`, 283 µs | Authenticated read: `read`, 5,325 µs | Re-audit, 0.6.1: light / read |
| --- | ---: | ---: | ---: | ---: |
| Day (average) | 5,787 | 2 | 31 | 2 / 27 |
| Day (3× peak) | 17,361 | 5 | 93 | 4 / 79 |
| Hour | 138,889 | 40 | 740 | 32 / 625 |
| Minute | 8,333,333 | 2,356 | 44,376 | 1,887 / 37,487 |
| Second | 500,000,000 | not a framework's question (2 ns a request): edge caches and CDNs | | |

A core serves about 3,500 light requests a second, or 190 authenticated
reads. The re-audit's figures (4,418 and 222 a second, at 0.6.1, the light
one over real HTTP) came from a lighter setup: its application had two
routes where the ladder's light request is routed past the template's 23
(1,180 calls a request, above), `DEFAULT_MIDDLEWARE` had five entries where
it now has eight, its user was in no group (here the group's permissions
take a fifth SELECT), and its machine was quieter. The sections below lower
these numbers as the performance branch's changes land.

## Making requests cheaper

- **Serve one worker per core**, on uvloop: `uvicorn config.asgi:application
  --workers N --loop uvloop`, or Granian (reference.md has the whole
  command and its limits).
- **Mount busy routers first.** FastAPI tries routes in order; a domain's
  routes mounted after the template's (`/health`, `/auth`, `/admin/auth`)
  pay for those 23 routes on every request.
- **Keep handlers and dependencies `async def`.** A plain `def` goes to the
  thread pool; `manage.py check` lists them (runtime.md).
- **Count round trips in tests**: `assert_num_queries` and
  `assert_max_queries` (testing.md) keep a path's statements from growing.
- **Profile before changing**: `python -m benchmarks.rung NAME
  --profile-out` on the path, and on a project's own routes the debug
  toolbar's SQL and N+1 report.

## What the performance branch changed

Each change to a request's cost has a section below with what it saves,
measured with the ladder.

### The ladder, and the upgrade to FastAPI 0.143, SQLAlchemy 2.1 and asyncpg 0.32

0.8.0 on its own pins (FastAPI 0.141.1, Starlette 1.8.0, SQLAlchemy
2.0.54, asyncpg 0.31.0) against the same code on this release's (0.143.1,
1.8.0, 2.1.4, 0.32.0), in three ladder runs with each rung's two sides
interleaved (`--baseline ../base/src --baseline-python ...`):

| Rung | Calls, before | Calls, after | CPU µs, before | CPU µs, after | CPU in 3 interleaved runs |
| --- | ---: | ---: | ---: | ---: | --- |
| `asgi` | 5 | 5 | 1.1 | 1.2 | within noise |
| `fastapi` | 168 | 234 | 39.2 | 51.0 | +30%, higher in every run |
| `bare` | 1,653 | 1,668 | 158 | 154 | within noise |
| `nested` | 1,889 | 1,904 | 176 | 170 | within noise |
| `default` | 1,825 | 1,840 | 222 | 206 | within noise |
| `default-json` | 1,965 | 1,981 | 285 | 283 | within noise |
| `template` | 2,018 | 2,033 | 283 | 281 | within noise |
| `template-json` | 2,158 | 2,173 | 376 | 370 | within noise |
| `me` | 5,887 | 5,296 | 4,863 | 3,995 | -18%, lower in every run |
| `read` | 7,354 | 6,662 | 6,042 | 5,325 | -12%, lower in every run |
| `write` | 7,642 | 7,055 | 6,185 | 5,953 | within noise |
| `keyed-write` | 10,971 | 10,428 | 10,771 | 10,056 | within noise |
| `throttled` | 4,396 | 3,788 | 4,695 | 3,695 | -21%, lower in every run |

- **The same round trips** on every rung: statements, transactions,
  checkouts, and pings did not move.
- **FastAPI 0.143 adds 15 calls to every domainkit request**: its
  telemetry hooks (three checks for operation spans, the route selection,
  its exception middleware) run although domainkit turns its telemetry off
  (ADR 0065). With its telemetry on, plain FastAPI adds 66 calls and 11.8
  µs a request (+30%, higher in every run), as the re-audit found for
  0.147.
- **SQLAlchemy 2.1 takes 543 to 692 calls off every database request**:
  fewer for statement cache keys and annotations, and its results are
  compiled code now. The authenticated reads
  and the 429 took 12–21% less CPU in every run; the writes' difference is
  within this machine's noise.
- **FastAPI still nests included routers** (`_IncludedRouter`), and walks
  the routes of each one a request passes: `/hello` behind the template's
  routers costs 1,668 calls (`bare`), and 488 when mounted before them.
  That is the largest single cost of a light request, and what flattening
  routers (PERF-04) saves.

The ladder's budgets were set from these figures: 0.8.0 on this release's
dependencies. Each change below lowers the ones it beats.

### The HTTP stack does each piece of work once

A request through the middleware stack used to pay for its routers twice
at each level of nesting, parse the client address two to four times, add
security headers through `MutableHeaders`, look metric labels up four
times, build a UUID for its id, and set each response header through a
fresh `MutableHeaders`. Now:

- **Routes are matched once, at the top.** `get_asgi_application`
  flattens the routers `ROOT_ROUTER` includes (`FLATTEN_ROUTERS`, ADR
  0060): FastAPI kept each `include_router` call nested, matched a request
  twice per level, and built the route's handler again for every request.
  Everything the routers gave their routes is kept, and the OpenAPI
  document does not change ([reference.md](reference.md#included-routers)).
- **The client address is parsed once per request**, for the
  refused-credential hold and the rate limit together
  (`scope["domainkit.address"]`), and the groups of the latest 4,096
  addresses are remembered. Only IPv6 addresses read
  `RATE_LIMIT_IPV6_PREFIX`. `ProxyHeadersMiddleware` remembers the latest
  4,096 addresses it parsed, and whether each is a trusted proxy's.
- **Headers are set raw.** Security headers are added in one append, and
  the request id and `RateLimit-*` headers without `MutableHeaders`; the
  bytes sent are the same, in the same order.
- **Request ids come from `os.urandom(16).hex()`**: the 32 lowercase hex
  digits of `uuid4().hex`, at a quarter of the cost.
- **`MetricsMiddleware` keeps each series' handle**, for at most 4,096
  series, instead of calling `labels()` four times per request.

Each cache is bounded, holds what a pure function of its key gives (an
address's group, a proxy's trust under its own networks, a series'
handle), and never answers for another key; addresses longer than 64
characters (an IPv6 zone can be any length) are never kept.

#### What it saves

Python calls per request, counted with cProfile (exact), `GET /hello`
answering `{"status": "ok"}` in process, against 0.8.0 (`a686177`), on
FastAPI 0.141.1 and on 0.143.1, the release this branch ships with. "T7"
is the re-audit's `init` stack of 0.6 (seven middleware); "init" is the
eleven `domainkit init` writes now, with the template's routes in front
of `/hello` (health, auth, and the auth admin).

| Stack and request | 0.141.1: 0.8.0 | routers flattened | all changes | 0.143.1: 0.8.0 | all changes |
| --- | ---: | ---: | ---: | ---: | ---: |
| No middleware, template routes, `/hello` (1 level) | 1,638 | 434 | 434 | 1,674 | 500 |
| No middleware, template routes, `/v1/hello` (2 levels) | 1,760 | 442 | 442 | 1,796 | 508 |
| 5 middleware (the re-audit's default) | 388 | 274 | 226 | 424 | 292 |
| `DEFAULT_MIDDLEWARE` (8) | 476 | 362 | 314 | 512 | 380 |
| T7 | 749 | 509 | 405 | 785 | 471 |
| T7, behind a trusted proxy | 844 | 604 | 427 | 880 | 493 |
| T7, an IPv6 client | 923 | – | 409 | 959 | 475 |
| init (11) | 2,019 | 815 | 711 | 2,055 | 777 |
| init, behind a trusted proxy | 2,114 | 910 | 733 | 2,150 | 799 |
| init, an IPv6 client | 2,193 | – | 715 | 2,229 | 781 |
| init, `/v1/hello` (2 levels) | 2,141 | 823 | 719 | 2,177 | 785 |
| init, `/auth/me` without a token (401, 2 levels) | 1,729 | 1,161 | 1,059 | 1,745 | 1,105 |

Time per request, the same requests: CPU time of the process, best of
fifteen runs of 10,000 in three rounds of fresh processes, 0.8.0 and this
branch alternating, with the range of each process's best; on a shared
4-core machine at a load of 5 to 8, so the absolute numbers are high (the
re-audit measured T7 at 166 µs on a quiet one) and wall time spread more.

| Stack and request | 0.8.0 µs | now µs | change |
| --- | ---: | ---: | ---: |
| 5 middleware | 92.7 (93–103) | 62.2 (62–64) | −33% |
| `DEFAULT_MIDDLEWARE` (8) | 125.2 (125–139) | 90.4 (90–97) | −28% |
| T7 | 171.4 (171–188) | 107.3 (107–111) | −37% |
| T7, behind a trusted proxy | 192.2 (192–205) | 116.1 (116–121) | −40% |
| T7, an IPv6 client | 242.8 (243–258) | 106.6 (107–120) | −56% |
| init (11) | 301.0 (301–324) | 170.8 (171–192) | −43% |
| init, behind a trusted proxy | 339.1 (339–359) | 185.6 (186–192) | −45% |
| init, `/v1/hello` (2 levels) | 332.5 (333–343) | 173.9 (174–181) | −48% |
| init, `/auth/me` without a token (401) | 421.1 (421–477) | 321.8 (322–340) | −24% |

One route nested in FastAPI alone (the re-audit's `nest_bench.py`, adapted
to compare each depth nested and flattened in one process):

| Depth | FastAPI 0.141.1, nested | flattened | 0.143.1 and 0.158.2, nested | flattened |
| --- | ---: | ---: | ---: | ---: |
| 0 | 168 calls | 168 | 234 | 234 |
| 1 | 280 | 168 | 316 | 234 |
| 2 | 370 | 168 | 406 | 234 |
| 3 | 492 | 168 | 528 | 234 |
| 1, behind 20 sibling routes | 1,078 | 320 | 1,114 | 386 |
| 2, behind 20 sibling routes | 1,605 | 320 | 1,641 | 386 |

On 0.141.1, the best of seven alternating rounds of 20,000 requests took
72.7 µs nested and 39.8 µs flattened at depth 2, and 176.0 and 67.9 µs
behind 20 sibling routes (wall time; medians 85.9 and 48.3, 204.4 and
80.1 µs).

What the pieces cost alone (`timeit`, best of 5, at 0.8.0):
`MutableHeaders` security headers 4.3 µs, raw 0.45 µs; `uuid4().hex`
1.96 µs, `os.urandom(16).hex()` 0.45 µs; `labels()` 0.86 µs, a kept
handle 0.05 µs; `client_group` of an IPv4 address 2.5 µs and of an IPv6
one 25 µs, remembered 0.06 µs; the proxy's trust of an address 2.6 µs.

#### Measuring it

The harness feeds hand-built scopes straight into the application, so no
server or network is timed: 1,000 warm-up requests, then five runs of
10,000; the best run counts, and cProfile counts calls over 2,000. To
compare two versions, run each in fresh processes, alternating which goes
first, and read the call counts before the times: on shared machines run
times swing by 15% or more.

### Database round trips: reads, keyed writes, and audit keys

A round trip to PostgreSQL costs the server work and the request its
latency, however little it carries. The performance branch cut them on
the paths every service takes:

- **Pre-ping, one round trip.** A pooled asyncpg connection is tested with
  SQLAlchemy's own `;`, outside a transaction, where SQLAlchemy wraps it
  in `BEGIN` and `ROLLBACK`. Every checkout saves two round trips. Through
  PgBouncer, which needs the transaction, the ping is as before
  ([databases.md](databases.md#connection-pools)).
- **Reads without a transaction.** `reads=Reads.STATEMENTS` runs each
  statement of a read-only unit of work on its own, on a pool held
  read-only by PostgreSQL. `reads=Reads.SNAPSHOT` reads one snapshot
  ([queries.md](queries.md#how-read-only-work-reads)).
- **Keyed writes.** An `Idempotency-Key` is claimed with one upsert, which
  also replaces an expired key, and the response is stored with one
  statement. Both commit as they run
  ([contrib.md](contrib.md#idempotency-safe-retries-of-unsafe-requests)).
- **Audit keys.** A new subject's key comes back from its insert, and a
  transaction reads each key once.

#### What it saves

Round trips per request on the wire, counted by a proxy that reads
PostgreSQL's protocol. These are the re-audit's scenarios: 16 requests in
flight, and the old 10-middleware stack (proxy, metrics, security, CORS,
context, hosts, token authentication, rate limit, body limit,
idempotency) for the writes.

| Request | 0.8.0 | Now | Transactions | Checkouts |
| --- | ---: | ---: | --- | --- |
| Read by id through a unit of work, no middleware | 6 | 4 | 1 → 1 | 1 → 1 |
| … with `reads=Reads.STATEMENTS` | 6 | **1** | 1 → 0 | 1 → 1 |
| Write through a unit of work, with a token | 15 | 11 | 2 → 2 | 2 → 2 |
| Keyed write (`Idempotency-Key`), first request | 34 | **16** | 5 → 2 | 5 → 4 |
| Keyed write, replay | 27 | **10** | 4 → 1 | 4 → 2 |
| Audited write, known subject | 17 | 13 | 2 → 2 | 2 → 2 |
| Audited write, new subject | 19 | 14 | 2 → 2 | 2 → 2 |

- Every checkout's ping went from 3 round trips to 1. A read through a
  unit of work is then `;`, `BEGIN`, the `SELECT`, `ROLLBACK`.
- A `STATEMENTS` read of columns is the `SELECT` alone; one that runs
  SQL text or calls a function adds a reset (below).
- Of the keyed write's 18 saved round trips, the ping saves 10 and the
  claim and store 8. Of its 9 statements, the `DELETE` of the key's
  expired row is gone, since the upsert replaces that row, and the claim
  and the store each run alone.
- A new audit subject costs 2 statements for its keys instead of 3, the
  key coming back from the insert. A known subject still costs 1 `SELECT`
  for the keys of every subject the commit writes about.

The counts are the same with SQLAlchemy 2.0.54 and with 2.1.4 and asyncpg
0.32.0.

The benchmark ladder counts on SQLAlchemy's side, with a project of its
own. Its `keyed-write` rung went from 10 statements, 5 transactions, 5
checkouts and 5 pings to 9, 4, 4 and 4, with either SQLAlchemy. It counts
a transaction for each connection SQLAlchemy begins, the claim's and the
store's included, which send no `BEGIN`; the table above counts the
transactions the server runs.

CPU of the Python process per request, the best of three runs, each in a
fresh process, old and new interleaved (16 in flight, 5 × 1,600 requests
after 320 to warm up), with SQLAlchemy 2.0.54. The machine was shared,
with a load average of 2.6 to 12, so read differences under about 5% as
noise. Each figure is the best run, with the range of all three:

| Request | 0.8.0 µs | Now µs |
| --- | ---: | ---: |
| Read by id through a unit of work | 1,257 (1,257–1,323) | 1,214 (1,214–1,248) |
| … with `reads=Reads.STATEMENTS` | | 995 (995–1,032) |
| Write through a unit of work | 5,049 (5,049–5,372) | 4,903 (4,903–5,148) |
| Keyed write, first request | 8,997 (8,997–9,237) | 8,386 (8,386–8,489) |
| Keyed write, replay | 7,105 (7,105–7,216) | 5,759 (5,759–5,986) |
| Audited write, known subject | 6,630 (6,630–6,946) | 6,277 (6,277–6,634) |
| Audited write, new subject | 7,689 (7,689–7,865) | 7,104 (7,104–7,319) |

What the CPU table shows:

- A replay costs 19% less.
- A `STATEMENTS` read costs 21% less than a read through a unit of work
  did.
- A first keyed write costs about 7% less: every run after the change was
  cheaper than every run before it.
- The rest moved within the noise.
- Round trips cost latency as well, which the client's CPU does not
  show: on a database across a network, each saved round trip is one
  network round trip less per request.

#### What it does not change

- **Authentication** still makes a transaction and four `SELECT`s per
  request without the principal cache. With `reads=Reads.STATEMENTS` it
  would drop `BEGIN`, `ROLLBACK` and the ping.
- **Core for hot reads** was measured and left out. A repository `get` by
  id through the ORM took 760 Python calls per read, and a Core `SELECT` of
  the same row 591. Keeping the ORM's identity map and version tracking
  costs those calls back (ADR
  [0061](adr/0061-read-without-a-transaction-and-ping-once.md)).
- **A second pool.** `STATEMENTS` reads open a pool of their own per
  alias, `<alias>:reads`. Count it when sizing `max_connections`.

#### What the security review added

A review of these paths found that `STATEMENTS` connections carried one
caller's session to the next, that a read could run twice or fail after
a restart, that two claims could deadlock through PgBouncer, and that
`forget` could miss a key being made. The fixes cost round trips only
where the work may have changed something. Measured alone, on the wire,
by the same kind of proxy (pool of one connection, prepared statements
warm, mean of 10):

| Work | Before | Now |
| --- | ---: | ---: |
| `STATEMENTS` `SELECT` by id (asyncpg) | 1 | 1 |
| `STATEMENTS` repository `get` (gadget, parts, makers) | 3 | 3 |
| `STATEMENTS` `SELECT` by id, then SQL text | 2 | 3 |
| `STATEMENTS` `count(*)` (a function) | 1 | 2 |
| `STATEMENTS` `SELECT` by id through psycopg | 1 | 3 |
| Audited commit, known subject | 6 | 6 |
| Audited commit, new subject (PostgreSQL) | 7 | 8 |

- **The reset.** A `STATEMENTS` connection that ran anything but plain
  `SELECT`s is reset before the next caller gets it: one simple query.
- **psycopg** cannot tell a closed idle connection, or count what ran on
  one, without asking: its `STATEMENTS` pool pings and always resets.
- **A new audit subject** takes its lock first: one statement more, 3 for
  its keys. A known subject still costs 1 `SELECT`, and no lock.
- **Keyed writes** cost what they did: the claim sweeps expired keys after
  its upsert instead of before, and the pin and late mark keep to the
  deadline with a timer, not a round trip.

### JSON responses and logs

0.9 encodes JSON responses with pydantic's encoder and writes the JSON access
log with less work, without changing a byte of either; and a busy service can
sample its access log (ADR 0063). Measured on domainkit 0.8.0 and this
release side by side, with the re-audit's harness (`GET /hello` fed straight
into the application, 4 shared vCPUs).

**Responses.** `domainkit.http.JSONResponse` gives the bytes Starlette's
gave, for 41% to 52% fewer instructions. Resource pages, items, and
batches, error bodies, and readiness answers use it.

| Payload | Instructions, 0.8.0 | Instructions, 0.9 | Time, 0.8.0 | Time, 0.9 |
| --- | ---: | ---: | ---: | ---: |
| `{"status": "ok"}` | 21,879 | 10,678 | 2.0 µs | 0.9 µs |
| an error body (211 bytes) | 38,678 | 22,438 | 3.6 µs | 1.8 µs |
| one resource (419 bytes) | 60,021 | 29,000 | 5.7 µs | 2.3 µs |
| a page of 50 resources (20 KB) | 1,939,596 | 991,051 | 179 µs | 69 µs |
| 200 orders with lines (159 KB) | 15,992,955 | 9,488,955 | 1,259 µs | 620 µs |

A request that answers the page of 50, with no middleware, costs 1,302,835
instructions instead of 2,155,451: 40% fewer. One that answers an error body
costs 382,839 instead of 400,160.

**Access log.** The JSON access line costs about 154,000 instructions instead
of 181,000, on the default middleware and on the re-audit's seven-middleware
`init` template stack alike (15% fewer; a light request is about
470,000). Of that, the formatter
takes 45,278 instructions a line instead of 69,900, and the copy for the
logging queue 16,302 instead of 26,844; the rest is the standard library's
`LogRecord`, the handler's lock, the queue, and the thread. A line the
logger's level drops is no longer built: with the access log off, a
request costs 468,844 instructions instead of 477,115.

**Sampling.** `ACCESS_LOG_SAMPLE_RATE = 0.1` writes about one ordinary
request in ten, and every error, refusal, and slow request
([runtime.md](runtime.md#sampling-the-access-log)):

| Default middleware | Instructions a request |
| --- | ---: |
| 0.8.0, every access line | 659,160 |
| 0.9, every access line | 623,077 |
| 0.9, `ACCESS_LOG_SAMPLE_RATE = 0.1` | 486,281 |
| 0.9, no access log | 468,844 |

The timer sees sampling's gain, not the formatter's: in two interleaved
series of 20 runs on a busy shared machine, a request with every line took
160 to 170 µs at best on either version, 112 to 117 µs sampling at 0.1,
and 91 to 111 µs without the log. Over HTTP (one uvicorn worker with
uvloop and httptools on one core, 64 connections, best of three runs at a
load average of 5 to 7), the default middleware served 3,643 requests a
second with every line on 0.8.0, 3,904 on 0.9, and 5,802 sampling at 0.1.

These counts are on FastAPI 0.141.1. On FastAPI 0.143.1 and SQLAlchemy
2.1.4 a whole request costs about 3% more (FastAPI's telemetry hooks), and
the savings hold within 2%: with every access line a request costs 674,619
instructions on 0.8.0 and 643,267 on 0.9 (the line 181,741 and 156,844),
505,391 sampling at 0.1, and 486,423 without the log; the page of 50 costs
1,314,338 instead of 2,168,067. FastAPI 0.143.1 still chooses between
pydantic's encoder and `jsonable_encoder` as 0.141.1 does, so the bytes are
the same on both.

#### How it was measured

- **Instructions** are counted by valgrind's callgrind, every thread
  included: each scenario runs with 100 and with 600 requests after the
  same warm-up, and the difference, divided by 500, is one request
  (`PYTHONHASHSEED=0`; two runs of one scenario differed by 0.25%).
  Instructions do not move with the machine's load; times do.
- **Times** are the best of 20 runs of 5,000 requests, the versions
  interleaved in fresh processes; on a shared machine runs of one version
  spread by 15% or more, so differences under that are noise.
- **Encodings** are of the dicts `model_dump(mode="json")` gives, best of 7
  with `timeit`, and counted with callgrind the same way.

#### Making it faster

- Sample the access log when it costs more than it tells: at 0.1 its cost
  nearly disappears, and the lines that matter stay.
- Keep the access log, and other loggers, at a level that drops what you do
  not read: a dropped access line costs nothing.
- Declare a response model (a return annotation) on routes that return
  plain dicts: FastAPI then encodes them with pydantic in Rust and skips
  `jsonable_encoder`, which costs about 1 ms for 50 resources. Mind that
  `jsonable_encoder` makes a float of a bare `Decimal`, and pydantic a
  string. Where the bytes must stay as they are, give the route
  `response_class=JSONResponse` instead ([reference.md](reference.md#json-responses)).
