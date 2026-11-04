# Performance

A domainkit request does three kinds of work: Python (routing,
middleware, validation, the framework's own checks), round trips to the
database, and the server's work around them. This page gives what each
kind costs on the paths a service serves most. It shows how to measure
the costs with the benchmark ladder, how CI keeps them from growing, and
what makes a request cheaper. Every number on this page is a
measurement: the page says how, where, and on which versions it was
taken.

On this page you learn how to:

- read what a request costs on each rung of the benchmark ladder;
- run the ladder, compare two commits, and profile one rung;
- read and update the budgets that CI gates on;
- work out how many cores a load needs;
- make your own requests cheaper.

Server settings and limits are on [reference/servers.md](reference/servers.md).
Event loops, logging, and servers are on [runtime.md](runtime.md).

## A first example

The benchmark ladder lives in the `benchmarks` package of a domainkit
checkout. From the checkout, with the development dependencies
installed, run three rungs that need no database:

```bash
python -m benchmarks asgi fastapi bare
```

It prints a line as each rung's process ends, then a table, then the
budget checks:

```text
  current  asgi          done
  current  fastapi       done
  current  bare          done
rung     what                      calls  SQL            modules  MiB  CPU µs       start ms
asgi     Bare ASGI app             5.0    0 / 0 / 0 / 0  169      27   1.3 (+5%)    5
fastapi  Plain FastAPI             234.0  0 / 0 / 0 / 0  398      50   51.4 (+17%)  307
bare     domainkit, no middleware  303.0  0 / 0 / 0 / 0  798      102  58.1 (+6%)   1,043
me: skipped, no PostgreSQL (--postgres)
...
OVER BUDGET: asgi modules: budget 166, measured 169.
OVER BUDGET: fastapi modules: budget 393, measured 398.
OVER BUDGET: bare modules: budget 789, measured 798.
OVER BUDGET: bare memory: budget 90, measured 102.
```

- **calls** are the Python function calls per request: the same as the
  budgets, because calls are exact.
- **SQL** is statements, transactions, pool checkouts, and pings per
  request.
- **CPU µs** is the best timed run. The percentage after it is how far
  the worst run was from the best.
- **start ms** is the time from the process's first line to the
  application's lifespan.

This run was on Python 3.14, not on CI's Python 3.13.16. The modules and
memory a process holds depend on the Python version and on what is
installed, so they went over their budgets here. The calls did not move.
That is why CI runs the gate on one pinned Python
([Budgets and the CI gate](#budgets-and-the-ci-gate)).

## What a request costs

The ladder measures twenty rungs, from a bare ASGI application to a
ledger posting counted against a limit. Requests go straight into the
application, one at a time, with no server or socket. The database rungs
run against PostgreSQL. Each rung runs in a process of its own.

The table below was measured on this release: FastAPI 0.143.1, Starlette
1.8.0, SQLAlchemy 2.1.4, asyncpg 0.32.0, Pydantic 2.14.1; Python 3.13.16
on uvloop, the version CI's `bench` job is pinned to; PostgreSQL 16. It
shows the highest calls and the best CPU of three ladder runs on a
shared 4-vCPU machine. All figures are per request:

- the round trips are statements, transactions, checkouts, and pings;
- the modules are what the rung's process holds once it has served;
- "0.8.0" is that release's calls on its own pins, where the rung
  existed then.

| Rung | Stack and request | Calls (0.8.0) | Round trips | Modules | CPU µs |
| --- | --- | ---: | ---: | ---: | ---: |
| `asgi` | A bare ASGI application: `GET /hello` | 5 (5) | 0/0/0/0 | 166 | 0.9 |
| `fastapi` | Plain FastAPI: `GET /hello` | 234 (168) | 0/0/0/0 | 393 | 48 |
| `bare` | domainkit, no middleware: `GET /hello` | 303 (1,653) | 0/0/0/0 | 782 | 60 |
| `nested` | domainkit, no middleware, two routers deep: `GET /a/b/hello` | 303 (1,889) | 0/0/0/0 | 782 | 59 |
| `default` | `DEFAULT_MIDDLEWARE`: `GET /hello` | 414 (1,825) | 0/0/0/0 | 783 | 105 |
| `default-json` | `DEFAULT_MIDDLEWARE`, JSON access log: `GET /hello` | 551 (1,965) | 0/0/0/0 | 783 | 171 |
| `template` | The `init` template's stack: `GET /hello` | 529 (2,018) | 0/0/0/0 | 785 | 135 |
| `template-json` | The template's stack, JSON access log: `GET /hello` | 666 (2,158) | 0/0/0/0 | 785 | 207 |
| `me` | Template stack, opaque token: `GET /me` | 673 (5,892) | 0/0/0/0 | 813 | 226 |
| `read` | Template stack, token, `ModelPermissions`, read through a unit of work: `GET /items/{id}` | 1,715 (7,359) | 1/1/1/1 | 813 | 1,484 |
| `write` | Template stack, token, `ModelPermissions`, write through a unit of work: `POST /items` | 2,054 (7,648) | 1/1/1/1 | 813 | 1,725 |
| `resource-list` | Template stack, token, a `ResourceRouter` page of 20 items with a scope and `ModelActions`: `GET /resources/items`, measured before any rung writes | 2,765 | 1/1/1/1 | 817 | 2,167 |
| `resource-read` | Template stack, token, a `ResourceRouter` read with a scope and `ModelActions`: `GET /resources/items/{id}` | 1,985 | 1/1/1/1 | 817 | 1,890 |
| `resource-command` | Template stack, token, a `ResourceRouter` command on one item, with a scope: `POST /resources/items/{id}/rename` | 2,726 | 1/1/1/1 | 817 | 2,500 |
| `keyed-write` | The write with an `Idempotency-Key` (`IdempotencyMiddleware` last): `POST /items` | 3,530 (10,977) | 4/3/3/3 | 814 | 3,548 |
| `money-write` | Template stack, token, a `Money` field written through a unit of work: `POST /wallets` | 2,178 | 1/1/1/1 | 882 | 1,777 |
| `posting` | Template stack, token, `post_entry` of two lines, the credited account's holder under a daily limit (`contrib.limits`): `POST /postings` | 8,049 | 14/1/1/1 | 889 | 7,216 |
| `encrypted-read` | Template stack, token, a read of a field `Encrypted()` under the data key: `GET /cards/{id}` | 1,758 | 1/1/1/1 | 882 | 1,584 |
| `step-up` | Template stack, a token that stepped up, a route behind `StepUp(max_age=...)`: `GET /stepped` | 1,580 | 1/1/1/1 | 882 | 1,348 |
| `throttled` | Template stack, token, refused by `RATE_LIMIT` (429): `GET /me`; its message translated in one lookup since PERF-24 | 498 (4,401) | 0/0/0/0 | 813 | 188 |

The four money rungs came with the 0.15 audit (PERF-32). They install
money's ledger, limits, keys, and a small domain of their own beside the
catalog's (`benchmarks/project/money_routes.py`). So they hold 69 modules
more than the other database rungs, which install none of it.

`benchmarks/budgets.toml` holds the gate for each rung
([below](#budgets-and-the-ci-gate)). Its calls and round trips are the
ones in this table. Its module budgets were written later, and are a few
modules higher (7 on most rungs).

### What each measure means

- **Calls** are the Python function calls that cProfile counts per
  request, built-in ones included, in every thread. The JSON access log
  is formatted on the logging thread, and counts. A coroutine resumed
  after a wait counts again, so each layer of `await` adds a call per
  wait for the database. Every rung counts the same calls in every run,
  except the two with a JSON access log. They can vary by up to 2.5%,
  because the logging thread waits on its queue more or less often.
  Their budgets are the highest of three runs.
- **Round trips** are what reaches the database, per request: statements
  sent to the driver, transactions begun, connections taken from the
  pool, and the check that a pooled connection still answers
  (`POOL_PRE_PING`).
- **CPU** is the process's CPU time per request (all threads), the best
  of the timed runs. It moves by 15% or more from run to run on a shared
  machine: never read a smaller difference into it. The calls and round
  trips are exact.

### What the rungs show

- **A guarded route resolves no dependency that only documents it.**
  `Require` names the bearer token so that OpenAPI shows it. FastAPI
  resolved that dependency on every request: about a hundred calls that
  read the `Authorization` header the authentication middleware had read
  already. Routes keep it for the documentation, and serve requests
  without it. Every authenticated rung costs about 98 calls less.
- **Authentication costs nothing once its principal is kept.** `GET /me`
  with an opaque token made five SELECTs in their own transaction. A
  kept principal makes none, and a 429 for a valid token makes none
  either (the [principal cache](#authentication-the-principal-cache)). A
  principal that is not kept is read in two queries.
- **Routing no longer walks nested routers.** Behind the template's 23
  routes, `/hello` took 1,180 of the `bare` rung's 1,653 calls.
  Flattened, a route two routers deep costs what one at the top does
  ([the HTTP stack](#the-http-stack-does-each-piece-of-work-once)).
- **A keyed write takes three transactions instead of five.** The claim
  is one statement outside a transaction, and so is storing the response
  ([database round trips](#database-round-trips-reads-keyed-writes-and-audit-keys)).
- **A scoped command on one aggregate takes one transaction instead of
  two** (0.10). The scope is checked, and the service runs, in one unit
  of work, and the service finds the aggregate the check read
  ([ADR 0025](adr/0025-resources-commands-and-expected-versions.md)). A
  rule that looks at the aggregate still checks it in a transaction of
  its own, closed before the rule runs.
- **Plain FastAPI is slower on 0.143** than on 0.141: its own telemetry
  hooks run on every request (ADR 0065). domainkit turns its telemetry
  off, and its own rungs gained far more than that.
- **A posting is the most expensive request**: 14 statements in one
  transaction, and about four times a plain write's calls (8,049; 11,973
  before 1.6 made the statements it runs on every posting once). The
  ledger locks its accounts and moves their balances (8 statements).
  `contrib.limits` takes its shared lock of the ledger's kinds as the
  posting commits (SEC-16b-2), reads the limits, locks the holder's row,
  sums its day's usage, and records each subject's movement (6 more).
  Without limits installed, a posting was 8 statements and about 6,000
  calls (PERF-26).
- **An encrypted field costs a few microseconds a value** (5 to 6 µs to
  seal or open one, `timeit`). The `encrypted-read` rung costs 43 calls
  more than `read`. A value sealed for a person
  (`Encrypted(subject=...)`) also reads that person's key: a statement
  more per person.
- **A step-up route reads the token's last step-up**: one statement, a
  transaction, and a checkout per request, where `me` makes none.

## Running the ladder

From a checkout, with the development dependencies installed:

```bash
python -m benchmarks                    # the rungs without a database
export DOMAINKIT_BENCH_POSTGRES_URL=postgresql+asyncpg://postgres@localhost:5432/postgres
python -m benchmarks                    # all of them
python -m benchmarks me read throttled  # some
```

`make bench` runs the ladder against its budgets too.

**The database.** `DOMAINKIT_BENCH_POSTGRES_URL` names a database in
which the ladder may make schemas. `--postgres URL` does the same, but
other users of the machine can read it in the process list. The ladder
passes the URL to the processes it starts in their environment, never on
their command line, and masks its password in what it prints. Each run:

1. creates `domainkit_bench_<tree>_<random>`, a schema no other run has.
   It refuses a schema that exists, rather than replace it;
2. migrates the schema with the code measured;
3. adds a user without a usable password, in a group that may view and
   add items, a token that expires within the hour, and an item;
4. drops that schema, and only that one, at the end.

The ladder touches nothing else, and only ever creates or drops schemas
named `domainkit_bench_...`.

**The result.** The ladder prints a table, checks the counts against the
budgets (below), and exits with status 1 when a rung fails or goes over
budget.

**Another commit, side by side.** The harness measures any source tree.
It compares two commits with the same code, the rungs of each
interleaved, so drift on the machine hits both:

```bash
git worktree add ../base a686177
python -m benchmarks --baseline ../base/src
# Another environment for the baseline (other dependency versions):
python -m benchmarks --baseline ../base/src --baseline-python ../base/.venv/bin/python
```

**One rung, closer.** `python -m benchmarks.rung template --profile-out
template.pstats` measures one rung, and keeps cProfile's statistics of
the counted requests. `--profile-dir DIR` keeps them for every rung of a
run. Two of them, from two trees, show which functions gained or lost
calls.

**Sizes.** Each rung warms up, counts one pass of requests, then times
several:

| | Warm-up | Counted | Timed |
| --- | ---: | ---: | --- |
| In process | 300 | 200 | 5 runs of 2,000 |
| With the database | 100 | 100 | 3 runs of 300 |

`--warmup`, `--count`, `--requests`, and `--repeats` change them for
every rung.

The options of `python -m benchmarks`:

| Option | What it does |
| --- | --- |
| `RUNG ...` | The rungs to run (default: all). |
| `--postgres URL` | A database to make schemas in (default: `$DOMAINKIT_BENCH_POSTGRES_URL`). |
| `--src SRC`, `--python PYTHON` | The source tree and the interpreter to measure. |
| `--baseline SRC`, `--baseline-python PYTHON` | Another tree, and its interpreter, to compare side by side. |
| `--budgets PATH` | Another budgets file. |
| `--no-gate` | Report only; never fail. |
| `--strict` | Fail on a rung that did not run, or a measure without a budget. |
| `--write-budgets` | Rewrite the budgets from this run. |
| `--json PATH` | Keep every figure. |
| `--summary PATH` | Write the Markdown table CI shows. |
| `--profile-dir DIR` | Keep each rung's cProfile statistics. |
| `--warmup`, `--count`, `--requests`, `--repeats` | Change the sizes for every rung. |

## Budgets and the CI gate

`benchmarks/budgets.toml` holds, for each rung, how much a request may
cost. This is the tolerance, and the `me` rung's budget, as the file
holds them now:

```toml
[tolerance]
calls = 0.02
statements = 0
transactions = 0
checkouts = 0
pings = 0
modules = 2
memory = 0.1

[rungs.me]
calls = 673
statements = 0
transactions = 0
checkouts = 0
pings = 0
modules = 820
memory = 94
```

`calls` and `memory` tolerances are fractions of the budget. The round
trips are exact. `modules` is a count, either way. `memory` is in MiB.

- **Over budget fails**, and names the rung, the measure, the budget, and
  the value, as in the first example: `OVER BUDGET: bare modules: budget
  789, measured 798.`
- **Under budget by more than the tolerance passes, with a hint**:
  `Under budget: <rung> <measure>: budget <budget>, measured <value>;
  lower the budget to lock it in.` Lower it in the same pull request, so
  the gain stays won.
- **Modules are gated per process** (ADR 0104). Each rung counts the
  modules its process holds once it has built the application and served
  its warm-up, past those the interpreter started with. Their tolerance
  is 2 modules either way, so a new eager import of anything heavy fails
  the job. What is installed changes the count (`prometheus_client`,
  `opentelemetry`). So the budgets are written from a run with every
  extra, as `make install` and CI's `bench` job install them.
- **Memory is gated per process**: the most memory the rung's process
  has held (its peak resident set) once it has built the application and
  served its warm-up, in MiB. It strays by a MiB or two from run to run,
  so its tolerance is a tenth of the budget. A dependency that brings
  tens of MiB, or a cache that grows with requests, fails the job. A bare
  ASGI process holds 26 MiB, plain FastAPI 46, a domainkit application
  90 to 94, and one with money's ledger, limits, and keys 110.
- **Time is never gated** (ADR 0064): a shared runner's timings are too
  noisy. CI's job summary shows the CPU time per rung, beside the base
  branch's on pull requests. It also shows the time each rung's process
  took to start, from its first line to the application's lifespan:
  about a second for a domainkit application. Modules, which are gated,
  are most of that time.

The `bench` job in `.github/workflows/ci.yml` runs the ladder on Python
3.13.16 against a PostgreSQL service, on every push and pull request,
with `--strict`. A rung that did not run, or a measure without a budget,
fails too.

The job's Python is pinned to the patch release the budgets were written
on. At the 0.15 milestone, budgets written on another 3.13 counted
`throttled` 21 calls below what 3.13.16 counts, on 0.15 and on 0.12.1
alike. So the gate failed on code that had not changed (PERF-32). Moving
the pin is a change of its own, with `--write-budgets` run on the new
release.

### Updating the budgets

Budgets hold for CI's Python (3.13.16) and the lock file. An upgrade
that changes FastAPI's or SQLAlchemy's calls moves them. After a change
that should move them, run this on Python 3.13.16 with the locked
dependencies:

```bash
python -m benchmarks --write-budgets
```

Then review the diff of `budgets.toml`: every raised number is a cost
the change adds. For the two JSON-log rungs, keep the highest of a few
runs.

## Cores for 500 million requests a day

One process per core, each on its own event loop, from the CPU per
request above. These are lower bounds. The HTTP server's own work comes
on top (about 34 µs a request for uvicorn on uvloop with httptools, as
the re-audit measured it on this machine class), and so does the
database's.

| 500 million requests per… | Requests/s | Light: `default-json`, 171 µs | Authenticated read: `read`, 1,484 µs | 0.8.0, 304 µs / 6,481 µs: light / read |
| --- | ---: | ---: | ---: | ---: |
| Day (average) | 5,787 | 1 | 9 | 2 / 38 |
| Day (3× peak) | 17,361 | 3 | 26 | 6 / 113 |
| Hour | 138,889 | 24 | 207 | 43 / 901 |
| Minute | 8,333,333 | 1,425 | 12,367 | 2,534 / 54,009 |
| Second | 500,000,000 | not a framework's question (2 ns a request): edge caches and CDNs | | |

A core now serves about 5,800 light requests a second, or 670
authenticated reads whose principal is kept: more than four times the
reads of 0.8.0. A posting counted against a limit takes about 11 ms: 90
a second per core.

To size your own service, measure the CPU per request of your own routes
(profile a rung like them, or time them under a server), divide a second
by it, and leave room for the server, the database, and peaks.

## Making requests cheaper

- **Serve one worker per core**, on uvloop: `uvicorn config.asgi:application
  --workers N --loop uvloop`, or Granian
  ([reference/servers.md](reference/servers.md) has the whole command and
  its limits).
- **Keep a route's first path segment literal** where you can
  (`/invoices/{id}`, not `/{tenant}/invoices/{id}`). The application
  tries only the routes of a request's first segment, and those that may
  match any path. So a route behind 1,000 others costs what the first
  does ([0.10](#010-foundations)). Routes whose first segment is a
  parameter are tried for every request, in order.
- **Keep `FLATTEN_ROUTERS` on** (the default). Give a router with its own
  `matches` or `handle` (an IP gate, a host rule) only the routes it
  guards: it stays nested
  ([The HTTP stack](#the-http-stack-does-each-piece-of-work-once)).
- **Keep handlers and dependencies `async def`.** A plain `def` goes to
  the thread pool. `manage.py check` lists them
  ([runtime.md](runtime.md#finding-blocking-code)).
- **Keep principals.** `AUTH.PRINCIPAL_CACHE.TTL` keeps who a token
  names. With `STAMPS` on a shared Redis cache, raise it up to 5 minutes
  ([the principal cache](#authentication-the-principal-cache)).
- **Read without a transaction** where a read needs no snapshot:
  `reads=Reads.STATEMENTS`
  ([database round trips](#database-round-trips-reads-keyed-writes-and-audit-keys)).
- **Sample the access log** on a busy service:
  `ACCESS_LOG_SAMPLE_RATE = 0.1`
  ([JSON responses and logs](#json-responses-and-logs)).
- **Declare response models** on routes that return large bodies:
  FastAPI then encodes them with pydantic directly.
- **Count round trips in tests**: `assert_num_queries` and
  `assert_max_queries` ([testing.md](testing.md#counting-queries)) keep a
  path's statements from growing.
- **Profile before you change anything**: `python -m benchmarks.rung NAME
  --profile-out` on the path, and on a project's own routes the debug
  toolbar's SQL and N+1 report.

## Common mistakes

- **Reading a small CPU difference as a change.** On a shared machine,
  runs of one version spread by 15% or more. Compare calls and round
  trips first; they are exact.
- **Comparing two versions in one process, or one after the other.** Run
  each in fresh processes, and alternate which goes first
  (`--baseline`), so drift on the machine hits both.
- **Writing budgets on another Python.** Modules, memory, and some calls
  change with the patch release. Write them on CI's Python 3.13.16, with
  the locked dependencies.
- **Raising a budget without a reason.** Every raised number is a cost
  the change adds. Say why in the pull request.

## What the performance branch changed

This part is the record of each change that moved a request's cost: what
it saved, and how it was measured. The numbers are the measurements of
their time, on the versions each section names. They are kept as they
were taken, so you can compare your own runs with them.

### The ladder, and the upgrade to FastAPI 0.143, SQLAlchemy 2.1 and asyncpg 0.32

This table compares 0.8.0 on its own pins (FastAPI 0.141.1, Starlette
1.8.0, SQLAlchemy 2.0.54, asyncpg 0.31.0) with the same code on this
release's pins (0.143.1, 1.8.0, 2.1.4, 0.32.0). It comes from three
ladder runs, with each rung's two sides interleaved
(`--baseline ../base/src --baseline-python ...`):

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
  compiled code now. The authenticated reads and the 429 took 12–21% less
  CPU in every run. The writes' difference is within this machine's
  noise.
- **FastAPI still nests included routers** (`_IncludedRouter`), and walks
  the routes of each one a request passes: `/hello` behind the template's
  routers costs 1,668 calls (`bare`), and 488 when mounted before them.
  That is the largest single cost of a light request, and what flattening
  routers (PERF-04 of the [performance re-audit](audit/0.9.0.md#the-performance-re-audit))
  saves.

The ladder's budgets were first set from these figures: 0.8.0 on this
release's dependencies. They now hold this release's own counts, the
highest of three ladder runs (the table at the top of the page).

### Authentication: the principal cache

An authenticated request used to read its principal (the token, the user
with the password hash, groups, the groups' permissions, and the user's
own permissions) in a transaction of its own: four or five `SELECT`s, a
`BEGIN`, a `ROLLBACK`, a pool checkout, and a pre-ping, about 70% of an
authenticated read. A 429 for a valid token paid the same, since
authentication runs before the rate limit.

Now a principal is kept per process for `AUTH.PRINCIPAL_CACHE.TTL`
(5 seconds), and read in two queries when it is not
([auth.md](auth/authentication.md#the-principal-cache); ADRs
[0058](adr/0058-cache-principals-and-stamp-revocations.md) and
[0059](adr/0059-read-principals-in-two-queries-without-the-hash.md)).

**What a request costs**, on PostgreSQL 16, through the `init` template's
middleware (proxy, security, context, hosts, auth, rate limit, body limit),
per request:

| Request | Before (0.8.0) | Kept (hit) | Read (miss) |
| --- | --- | --- | --- |
| `GET /me`, `IsAuthenticated`, a user in no group | 4 `SELECT`, `BEGIN`, `ROLLBACK`, pre-ping, checkout; 4,265 Python calls | nothing; 972 calls | 2 `SELECT`, `BEGIN`, `ROLLBACK`, pre-ping, checkout; 2,395 calls |
| `GET /guarded`, `HasPermissions`, a user with a permission and a group | 5 `SELECT`, the rest as above; 5,221 calls | nothing; 1,262 calls | 2 `SELECT`, the rest as above; 2,693 calls |
| `GET /guarded/items/{id}`, the same, and a read through a unit of work | 6 `SELECT`, 2 transactions, 2 pre-pings, 2 checkouts; 6,834 calls | the read's 1 `SELECT`, transaction, pre-ping, checkout; 2,876 calls | 3 `SELECT`, 2 of each; 4,307 calls |
| `GET /me` over the rate limit (429), a valid token | 4 `SELECT`, `BEGIN`, `ROLLBACK`, pre-ping, checkout; 4,049 calls | nothing; 752 calls | 2 `SELECT`, the rest as above; 2,175 calls |

Statements, transactions, pre-pings, and pool checkouts are counted with
SQLAlchemy's events over 400 requests, one at a time; "nothing" is none
after the first request, which reads the principal. Python calls are
cProfile's exact count over 500 requests. `last_used_at` adds an `UPDATE`
and a `COMMIT` once per token per five minutes, before and after.

**CPU per request**, 16 requests in flight, best of five runs of 1,600
requests each, old and new in fresh processes, interleaved, over three
rounds (in brackets, the best run of each round, lowest to highest):

| Request | Before (0.8.0) | Kept (hit) | Read (miss) |
| --- | --- | --- | --- |
| `GET /me` | 3,658 µs (3,658–3,758) | 248 µs (248–318), −93% | 1,556 µs (1,556–1,645), −57% |
| `GET /guarded` | 4,576 µs (4,576–4,820) | 339 µs (339–346), −93% | 1,602 µs (1,602–1,867), −65% |
| `GET /guarded/items/{id}` | 5,868 µs (5,868–6,221) | 1,748 µs (1,748–1,856), −70% | 2,918 µs (2,918–3,078), −50% |
| 429 for a valid token | 3,668 µs (3,668–3,725) | 219 µs (219–242), −94% | 1,458 µs (1,458–1,603), −60% |

Measured on 21 October 2026 on a shared 4-CPU machine (load average 6 to
9 during the runs), with the re-audit's harness (`bench.py`, requests fed
straight to the ASGI application), CPU time of the process per request.
Under that load, 0.8.0 also answered a few of the reads with 503 (the
pool's wait ran out), and the new code none.

- "Kept" is a hit: the principal was read by an earlier request of the
  same credential within the TTL. "Read" is a miss, forced by dropping
  every principal before each request.
- A hit makes no query. Its cost is the user built for the request (about
  16 µs for a user, and 16 µs more for each permission with its content
  type, measured with `timeit`; estimated in proportion for users with
  hundreds of permissions) and the rest of the stack.
- With `STAMPS`, a hit adds one `MGET` to the shared cache. A miss reads
  `ALL`'s stamp and the token's before taking a connection, giving `ALL`
  one if absent (`SET NX`), and gives any other absent one a stamp once
  the credential proved real. A commit that changes principals writes
  their stamps twice, before and after it.
- Memory: principals share their grants. Measured with `tracemalloc` over
  200 tokens of one user holding 1,000 permissions through a group: 368 kB
  per entry before (3.7 GB at the default `MAX_ENTRIES`), 5.6 kB after.
- Lending the password hash to `request.user` while `.password` is
  deprecated (until 0.10) adds no statement. It adds 6 Python calls to a
  hit and 13 to 18 to a miss, and one hash of about 100 characters to each kept
  principal. The CPU per request did not change measurably: best of three
  rounds, `/me` hit 293 µs before and 283 µs after, miss 1,852 µs and
  1,776 µs; `/guarded` hit 378 µs and 374 µs, miss 1,910 µs and 1,921 µs.
  Those were measured on 22 October 2026, at load average 6 to 9.
- Measure it yourself: `auth_principal_cache_total{event="hit"}` against
  `{event="miss"}` gives the hit rate. A low one means short-lived tokens,
  many processes per client, or a `TTL` shorter than the gap between a
  client's requests.

How to make it faster:

- Raise the `TTL` (up to 5 minutes) when `STAMPS` names a shared Redis
  cache: changes still reach every process at once, and fewer requests
  read their principal. Without `STAMPS`, the `TTL` is how long a
  revocation takes to reach the other processes; `check` warns above
  5 seconds.
- Keep groups and permissions per user to what they need: a user's
  principal is built for each request, in time proportional to its grants.

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
  document does not change ([reference/responses.md](reference/responses.md#included-routers)).
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

A router whose class has its own `matches`, `handle`, or other method
that serves requests (an IP gate, a host rule) stays included, so its own
routes cost what nesting costs; the routers around it are still
flattened ([reference/responses.md](reference/responses.md#included-routers)). Give such a
router only the routes it guards.

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
  ([contrib.md](contrib/idempotency.md#idempotency-safe-retries-of-unsafe-requests)).
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

**Responses.** domainkit's own JSON bodies (resource pages, items, and
batches, error bodies, and readiness answers) give the bytes Starlette's
gave, for 41% to 52% fewer instructions.

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

`domainkit.http.JSONResponse`, for a project's own bodies, first checks
that its content is JSON's own values, so that what Starlette refused (a
model passed by mistake, a `Decimal`, a `datetime`, an integer past
Python's digit limit) it refuses too ([reference/responses.md](reference/responses.md#json-responses)).
The check is Python and costs about what pydantic's encoder saves:
instructions per encode on FastAPI 0.143.1, with the payloads above and
those of the security review:

| Payload | `json.dumps` (Starlette) | `JSONResponse` | domainkit's own bodies |
| --- | ---: | ---: | ---: |
| `{"status": "ok"}` | 21,723 | 9,489 | 11,411 |
| an error body | 38,722 | 26,479 | 22,803 |
| one resource | 60,752 | 38,201 | 28,830 |
| a page of 50 resources | 1,885,266 | 1,732,036 | 977,754 |
| 200 orders with lines | 16,194,560 | 20,374,652 | 9,528,040 |
| one string of 1 MB | 35,022,878 | 11,010,388 | 18,548,736 |
| 100,000 strings like `",0.0000x"` | 81,674,934 | 69,262,245 | 126,938,647 |
| 100,000 floats of 0.5 | 210,950,885 | 162,327,471 | 55,165,425 |
| 200,000 keys, integer values | 336,499,646 | 411,797,353 | 207,510,234 |

On content of many small dicts or many numbers the check costs more than
`json.dumps`; text costs it nothing, as it reads no string. Routes that
answer large bodies are cheapest with a response model, which FastAPI
writes with pydantic directly.

**Access log.** The JSON access line costs about 154,000 instructions instead
of 181,000, on the default middleware and on the re-audit's seven-middleware
`init` template stack alike (15% fewer; a light request is about
470,000). Of that, the formatter
takes 45,278 instructions a line instead of 69,900, and the copy for the
logging queue 16,302 instead of 26,844; the rest is the standard library's
`LogRecord`, the handler's lock, the queue, and the thread. A line the
logger's level drops is no longer built: with the access log off, a
request costs 468,844 instructions instead of 477,115.

**Sampling.** `ACCESS_LOG_SAMPLE_RATE = 0.1` writes about one successful
request in ten, and every response from 400 up and every slow request
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
  `response_class=JSONResponse` instead ([reference/responses.md](reference/responses.md#json-responses)).

### 0.9.2: work done once instead of per request

The audit of 0.9.0 found work on the request path that does not depend on
the request: content types worked out again, domain settings looked up
again, read-only engines and the idempotency statements built again, and
the access line's caller found by walking the stack. 0.9.2 does each once,
and works it out again only when what it depends on changes. Round trips
did not move on any rung. Measured with the ladder against 0.9.1
(`--baseline`), each rung's two sides interleaved:

| Rung | Calls, 0.9.1 | Calls, 0.9.2 | Change |
| --- | ---: | ---: | --- |
| `default-json` | 751 | 741 | -10 (access line; the logging thread's waits vary) |
| `template-json` | 871 | 856 | -15 (access line) |
| `me` | 1,193 | 1,181 | -1% |
| `read` | 2,439 | 2,261 | -7.3% |
| `write` | 2,815 | 2,595 | -7.8% |
| `keyed-write` | 5,880 | 4,044 | -31% |
| `throttled` | 1,039 | 1,013 | -2.5% |

The rungs without authentication or a database count the same calls.
In two interleaved runs (load average 2 to 5), the keyed write took
3,548 and 3,788 µs of CPU against 5,001 and 5,073 (-25% to -29%), the
read 1,541 and 1,511 against 1,710 and 1,531, and the write 1,658 and
1,833 against 1,908 and 1,967: lower in both runs, by less than this
machine's noise for the read. `me` and `throttled` moved within noise.
The JSON-log rungs count 10 to 15 calls fewer in each run, but vary by more
than that from run to run with the logging thread's waits (728 to 743
for `default-json`), so their budgets stay as they were.

- **Content types** (`ModelPermissions`, PERF-01). `content_type_key`
  sorted every mapper and read every class's `Meta` to find one class's
  key: about 170 calls a request with the ladder's six mapped classes, and
  ten times that with sixty. The keys are now worked out once, kept with
  the state they were worked out in (`registry.generation`, which
  `populate` and `clear` move; a count of classes mapped and tables
  attached, from SQLAlchemy's events; the number of the project's tables,
  which falls when one is removed), and worked out again when it changes,
  by one thread at a time. Classes unmapped by `registry.dispose()` are
  not seen until `registry.clear()`.
- **Domain settings** (`registry.settings`, `auth_settings`, PERF-06).
  Each of its 2 to 8 calls a request walked the domains and read the raw
  settings through the lazy proxy; it now answers from the settings object
  it validated from while that is the current one. Every way of changing
  settings replaces that object, so overrides in tests are seen at once.
- **Read-only engines** (PERF-07). A read-only session no longer builds an
  `OptionEngine` of its own: each loop's engine keeps one for
  `TRANSACTION` and one for `SNAPSHOT`. A read-only unit of work with one
  `SELECT` makes 917 calls instead of 1,129 on PostgreSQL, and 1,601
  instead of 1,701 on SQLite. `connections.autocommit()` keeps setting
  `AUTOCOMMIT` on its connection: an engine made once with it cost 26
  calls a use more (664 instead of 638), as its connections join two
  event dispatchers.
- **Idempotency statements** (PERF-12). The claim, the read of a held
  key, the take-over, the sweep, the store or release, the pin and the
  commit mark are built once per dialect and clock, with each request's
  values bound; they send the SQL text and parameters the statements
  built for each request sent, which the tests check on asyncpg, psycopg
  and aiosqlite, on both clocks. No SQLAlchemy statement is built on the
  keyed write's path any more.
- **The access line** (PERF-18). `log_enabled` makes the record
  `Logger.log` makes, field for field, without asking the level again or
  walking the stack: 15 calls and about 2.5 µs of the caller's 16 µs a
  line, measured alone, and the same bytes from every formatter. Turning
  off `logging.logThreads`, `logProcesses` and `logMultiprocessing`, as the
  audit proposed, would save 7 more calls, but it empties fields a
  project's own formatter may print, so it is not done.

### 0.10: foundations

0.10 stopped paying per request for what a request does not use, and
stopped a herd of reads where many requests need the same principal.
Measured with the ladder against 0.9.2 (`--baseline` at `d7f93a4`, the two
interleaved; 0.10's routing package and its rungs are in these numbers
too), per request:

| Rung | Calls, 0.9.2 | Calls, 0.10 | Round trips |
| --- | ---: | ---: | --- |
| `bare` | 481 | 305 | none, as before |
| `nested` | 489 | 305 | none, as before |
| `default` | 598 | 423 | none, as before |
| `default-json` | 742 | 567 | none, as before |
| `template` | 713 | 538 | none, as before |
| `template-json` | 858 | 681 | none, as before |
| `me` | 1,181 | 780 | none, as before |
| `read` | 2,261 | 1,839 | 1/1/1/1, as before |
| `write` | 2,595 | 2,173 | 1/1/1/1, as before |
| `resource-read` | 3,063 | 2,606 | 1/1/1/1, as before |
| `resource-command` | 4,546 | 3,329 | 2/2/2/2 to 1/1/1/1 |
| `keyed-write` | 4,054 | 3,635 | 4/3/3/3, as before |
| `throttled` | 1,013 | 803 | none, as before |

Run on a shared 4-CPU machine at load average 8 to 17, so the CPU times
are only indicative: `bare` 82 to 63 µs, `me` 425 to 335 µs, `read` 1,950
to 1,497 µs, `throttled` 418 to 305 µs, each the best of its timed runs.

- **Routes by first segment** (PERF-05, ADR 0060's amendment). A request
  tries only the routes whose template starts with its path's first
  segment, and those that may match any path, in order: 176 fewer calls
  on every light rung, which reach `/hello` behind the template's 23
  routes. One GET behind N routes:

  | Routes ahead | Calls, every route tried | Calls, indexed | CPU µs, tried / indexed |
  | ---: | ---: | ---: | --- |
  | 9 | 409 | 329 | 62 / 56 |
  | 99 | 1,129 | 329 | 133 / 58 |
  | 999 | 8,329 | 329 | 757 / 59 |

- **A request's grants, built when it reads them** (PERF-03, ADR 0059's
  amendment). A kept principal no longer builds the user's groups,
  permissions, and content types for each request: `has_perm` answers from
  keys worked out once per principal. Building the user and one
  `has_perm`: 70 to 9 µs with two permissions, 545 to 11 µs with fifty,
  2,580 to 12 µs with 220. `me` and the guarded rungs count about 225
  calls fewer for it (401 in all on `me`, with the route index's 176).
- **One principal read at a time** (PERF-04, ADR 0058's amendment). Ten
  credentials with twenty requests each, all missing at once: 400
  `SELECT`s before, 20 now. Near the end of its `TTL`, one request reads a
  kept principal again while the others are served it: with a request a
  millisecond on one credential, 34 requests in 3,000 waited for a read
  (up to 5 reads at once), and 11 do now (one at a time).
- **Exceptions in the log queue** (CONC-04, ADR 0051's amendment): 2,000
  queued errors from frames holding 50 kB each kept 103.1 MB, now 1.7 MB;
  the caller formats the traceback, 59 to 176 µs per error record.
- **The first outgoing call** (CONC-06): `HttpClient` builds its TLS context
  on a thread, and the first call on a loop holds it 6 to 8 ms instead of
  20 to 39 (1 to 2 ms with the context already built).
- **Deadlines and connections** (CONC-08, ADR 0048's amendments): ten
  requests cut off at a 1 s deadline mid-statement opened and discarded
  ten connections in 0.10, in a transaction and with `Reads.STATEMENTS`
  alike. With asyncpg, a cut-off connection is now kept once it has
  settled (the cancelled exchange finished, the transaction rolled back):
  the ten open none and discard none, on the one connection their pool
  opened first, and still answer 503 about 25 ms after the deadline. Under
  an anyio cancel scope, and on other drivers, it is closed, as before.
- **Pools** (PERF-14): `check` warns when a pool cannot serve the
  requests a loop admits ([reference/backends.md](reference/backends.md#sizing-pools)).
  The defaults stay: requests that each hold a connection for 20 ms
  against a pool of 15, a burst of 200, are all served under a cap of
  1,000 (p50 277 ms, at most 463 ms), where a cap of 60 refuses 40; a
  burst of 1,000 is all served within 3.3 s, where a cap of 60 refuses 840.
- **Startup** (PERF-15): auth loads `domainkit.contrib.mail` only when it
  checks an address or sends mail. Importing the `init` project's
  `config.asgi` with production settings (which builds the application),
  best of five fresh processes: 817 modules, 84.3 MB, and 973 ms of CPU
  at 0.9.2; 829 modules (the routing package's), 84.5 MB, 969 ms before
  this change; 818 modules, 83.5 MB, 930 ms now. The rest of startup is
  what serving the template's routes needs: FastAPI builds each route's
  models as it is added (about 190 ms of the admin routers' 230), Python's
  dataclasses compile each event class (auth's 27: about 60 ms), and
  FastAPI imports its OpenAPI models (about 150 ms).

### 0.12: startup, keyed bodies, flag changes, and what was verified

Measured on Python 3.13.17, against 0.11.1 (`--baseline`), PostgreSQL 18.

- **Startup** (PERF-15, ADR 0104). Best of five fresh processes, every
  extra installed: `import domainkit.http` 679 to 57 modules, 580 to 7 ms
  of CPU, 69.2 to 10.1 MB; the `init` project's `config.asgi` with
  production settings 824 to 806 modules, 85.6 to 84.8 MB, its CPU the
  same (about 910 ms). A served project no longer loads the ledgers, the
  brokers, or messaging's worker and dispatcher unless it uses them. The
  ladder gates the modules: 18 fewer on each rung without a database
  (template 800 to 782), 16 on those with one (827 to 811).
- **Keyed bodies** (PERF-11, ADR 0105). The peak the idempotency
  middleware holds, 0.11.1 to now: an 8 MiB keyed body 16.0 to 1.1 MiB;
  an 8 MiB kept response 16.2 to 8.2 MiB; 32 MiB each way 96.2 to
  32.3 MiB, the replay byte-identical. What is left is the value stored.
  The `keyed-write` rung costs 16 calls more (3,639).
- **Flag changes** (PERF-17, ADR 0033's amendment). 20 replicas over one
  cache reading as a change lands: 20 loads of the flags, now one.
- **FastAPI's dependency solver and telemetry** (PERF-13): verified, not
  changed. domainkit now adds one dependency to each route, its access
  rule, not two: `bare` costs 304 calls against plain FastAPI's 234
  (62.7 against 53.8 µs), most of the 70 in FastAPI's dependency solver
  for that one dependency. FastAPI's telemetry hooks, its own telemetry
  off, cost 22 calls of `template`'s 519, and 11 of `me`'s 750. The rule
  is how deny by default works (ADRs 0017, 0080), and the hooks are
  FastAPI's: no cheap win.
- **Memory per request** (PERF-16): verified, no leak. The peak a request
  allocates on top of what the process holds, 2,000 requests on uvloop
  with the loop running between them, as under a server: `template`
  23.1 KB, `me` 25.3 KB, `read` 57.3 KB, `write` 67.0 KB, `keyed-write`
  72.9 KB (70.8 at 0.11.1; the 0.9.0 audit measured 26 KB to 104 KB);
  one collection per 1,000 requests, and nothing domainkit allocates is
  still held after 4,000 (a tracemalloc snapshot diff). Requests that
  never yield to the loop, as the ladder sends them in process, keep each
  request's context until asyncio purges the cancelled deadline timers
  that hold it, at its next iteration: about 4.4 KB a request, bounded by
  that purge under a server.


### The 0.15 audit: list indexes, startup, and the money rungs

Measured on Python 3.13.16 and PostgreSQL 16, against `d126394` (the 0.15
milestone).

- **Lists read through an index** (PERF-29). The default order of each new
  list endpoint, newest first, had no index, so every page scanned and
  sorted the table: the ledger's entries 107 ms a page at 500,000 rows,
  approval requests 78 ms at 300,000, holds 17 ms at 100,000 (EXPLAIN
  ANALYZE, `LIMIT 51`). Entries (`posted_at`), holds (`placed_at`), and
  approval requests (`submitted_at`) have one now. An aggregate's history
  in the audit trail scanned the whole trail (555 ms a page at 500,000
  rows); the fields named in `AUDIT.HISTORY_FIELDS` get an index that
  `migrate` keeps on PostgreSQL ([audit.md](contrib/audit.md#history)).
- **Fewer index writes** (PERF-30). A posting wrote two indexes of
  `entry_id`: the unique `(entry_id, line)` serves the look-ups alone. The
  limits' subject lock updates `touched_at` on every movement, which was
  indexed, so no lock was a HOT update (1,857 updates, none HOT); it is
  not indexed now, and the hourly purge reads the subjects whole. Usage's
  `key`, looked up only by equality, has a hash index on PostgreSQL: 40 MB
  instead of 111 MB at 1.5 million rows.
- **Startup** (PERF-31). 0.14 and 0.15 added seven modules to every
  process, whether it used money or encryption or not: `domainkit.db`
  imported the key ring, and money the `csv` module. Those four now load
  on first use (`bare` holds 782 modules, 786 before). Three remain, in
  every process that imports `domainkit.db`: `domainkit.money`,
  `domainkit.db.money` and `domainkit.db.encrypted`, about 5 ms of import
  (`-X importtime`, best of five), and every ORM statement runs one check
  that refuses bulk updates of encrypted fields. Keeping them off the
  import path would take a lazy `domainkit.db` namespace and mappers that
  look for encrypted columns without their type: more tangle than 5 ms
  is worth, so they stay. A project of `domainkit init --profile fintech`
  starts with 913 modules, 1.4 s of CPU and 106 MB; the default template
  with 777, 0.8 s and 84 MB.
- **The money rungs and a stable gate** (PERF-32). `money-write`,
  `posting`, `encrypted-read` and `step-up` measure what 0.14 and 0.15
  added (the table at the top of the page). The bench project keeps a
  principal for the whole run (`AUTH.PRINCIPAL_CACHE.TTL` of 5 minutes):
  with 5 seconds, a counted pass that outlasted it read the principal
  again now and then, and `keyed-write` counted 4.02 statements in one run
  of three. CI's `bench` job is pinned to Python 3.13.16, which the
  budgets were written on; the JSON-log rungs' budgets are the highest of
  three runs (560 and 675: they vary by up to 2.5%, not 1%).


## See also

- [Runtime](runtime.md): event loops, logging, deadlines, and shedding.
- [Server limits](reference/servers.md): production commands.
- [Sizing pools](reference/backends.md#sizing-pools).
- [Testing](testing.md#counting-queries): counting queries in tests.
- [The performance re-audit](audit/0.9.0.md#the-performance-re-audit).
