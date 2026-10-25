# Changelog

All notable changes to domainkit. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/); see `docs/versioning.md`.

## [Unreleased]

### Fixed

- Event loops on several threads (free-threaded Python, a loop per
  thread) looking up a memory cache for the first time at once get one
  store per alias, and an application's throttle backend is made once:
  a loop could keep a private store that never saw the others' writes,
  invalidations, or `caches.reset()`, and a second memory throttle
  backend let each route's limit through twice (CONC-01).

## [0.9.0] - 2026-10-22

### Added

- The benchmark ladder: `python -m benchmarks` measures what a request
  costs on thirteen rungs, from a bare ASGI application through the
  `init` template's stack to an authenticated read, a keyed write, and a
  429, in Python calls and database round trips (statements,
  transactions, checkouts, pings) and CPU time. CI's new `bench` job fails
  a change that makes a rung's requests cost more calls (by over 2%) or any
  more round trips than `benchmarks/budgets.toml` allows, and shows CPU
  time beside the base branch's; time is never gated (PERF-09, ASYNC-R17;
  ADR 0064). `--baseline` measures any other commit with the same harness.
  Each run makes a PostgreSQL schema of its own (a random suffix), drops
  only that one, masks the database's password in what it prints, and
  gives its bench user a token that expires within the hour and no
  password.
- `docs/performance.md`: what each rung costs, how to run the ladder and
  update its budgets, the cores 500 million requests a day need, and what
  makes requests cheaper. The README says to serve one worker per core.
- `domainkit.http.JSONResponse`: Starlette's `JSONResponse` encoded by
  pydantic's Rust encoder where the content is JSON's own values, with the
  same bytes and the same errors. It checks the content first; anything
  else goes to `json.dumps` and fails as in Starlette: a pydantic model
  passed by mistake, a dataclass, a `Decimal`, a `datetime`, bytes, or a
  set is a 500 that writes none of their fields, and an integer past
  Python's digit limit is refused before it is written. A body of
  `{"status": "ok"}` takes 9,489 instructions instead of 21,723, a page of
  50 resources 8% fewer, and 200 orders of eight lines a quarter more: the
  check costs about what the encoding saves. Give it to routes without a response model
  (`response_class=JSONResponse`); routes with one keep FastAPI's default,
  which encodes them with pydantic already. domainkit's own bodies,
  built JSON-native (resource pages, items, and batches, and their ETags,
  error bodies, readiness answers), skip the check: a page of 50 resources
  encodes in half the instructions (69 µs instead of 179) (PERF-08; ADR
  0063).
- `ACCESS_LOG_SAMPLE_RATE` (1.0) and `ACCESS_LOG_SLOW_DURATION` (1 second):
  write a share of the access lines of successful and redirected requests,
  each drawn on its own. Every response from 400 up (the 404 of what is out
  of the caller's reach and the 401, 403, and 429 of refusals among them),
  every request without a response, and requests that take the slow
  duration or longer are always written, and `domainkit.security` and the
  audit trail are never sampled. At 0.1 the
  JSON access line costs about 18,000 instructions a request instead of
  154,000 (PERF-02; ADR 0063). The production settings `init` writes read
  the rate from the environment.
- `domainkit.http.routing.flatten_routes(router)` puts the routes of each
  router included in `router` in its place, as `get_asgi_application` now
  does for `ROOT_ROUTER` (ADR 0060). `FLATTEN_ROUTERS = False` keeps
  FastAPI's nesting.
- Read-only units of work choose how they read with `reads=`, which also
  makes them read-only (`docs/queries.md`; ADR 0061).
  - `Reads.STATEMENTS` runs each statement on its own, without `BEGIN` and
    `ROLLBACK`: a read by id on PostgreSQL takes 1 round trip instead of 6.
    It reads on a pool of its own per alias, `<alias>:reads`, sized like
    the alias's, whose connections PostgreSQL holds read-only, so a write
    is refused however it is sent.
  - `Reads.SNAPSHOT` reads one snapshot, in a `REPEATABLE READ`, `READ
    ONLY` transaction.
  - Both sessions refuse to flush, and refuse write and DDL statements,
    with `ReadOnlyError`.
  - `STATEMENTS` bounds each statement by the deadline. A connection the
    driver knows the server closed is replaced before a statement is sent
    on it; a statement whose connection closes while it runs fails, and is
    never run again, since it may have had effects outside its
    transaction. Through psycopg, which notices only when it next sends,
    the pool pings.
  - A `STATEMENTS` connection is reset (`RESET ALL`, `RESET ROLE`,
    `UNLISTEN *`, advisory locks, cursors, temporary tables) before the
    next caller gets it, so nothing one unit of work sets in the session,
    `default_transaction_read_only` included, reaches another. A unit of
    work that ran only plain `SELECT` statements needs no reset; one that
    ran SQL text or called a function takes one more round trip. A new
    connection that is not read-only, because a pooler dropped the
    startup setting, is refused, and readiness checks the pool as
    `database:<alias>:reads`. `SET LOCAL` and transaction-scoped advisory
    locks end with each statement.
  - Through PgBouncer and on SQLite it reads in the `READ ONLY`
    transaction.
  - `readonly=True` alone keeps its `READ ONLY` transaction, as before
    (PERF-03).
- `connections.autocommit()`: a connection whose statements commit as they
  run, for short writes that stand alone; through PgBouncer, a transaction.
- `deadlines.enforced()`: whether a timer of the running task already cuts
  it off at its deadline.
- `domainkit.contrib.auth.principals.invalidate(...)`, `invalidate_all()`,
  and `invalidate_everywhere(...)` (with the stamps) drop principals that SQL
  outside the ORM changed;
  `UserRepository.password_hash(user_id)` and
  `domainkit.contrib.auth.api.password_holds(user, password)` read and
  check a password hash afresh.
- The principal cache (`AUTH.PRINCIPAL_CACHE`; ADR 0058): who a credential
  names, with their groups and permissions, is kept per process for `TTL`
  (5 seconds; 5 minutes at most), at most `MAX_ENTRIES` (10,000) of them.
  A request whose principal is kept makes no query to authenticate (was
  4 or 5 `SELECT`s, a `BEGIN`, a `ROLLBACK`, and a pre-ping), and a 429
  for a valid token none either (PERF-01, ASYNC-R13, N8). Opaque tokens,
  personal tokens, browser sessions, JSON Web Tokens, and client
  certificates are all kept; expiry, CSRF, and signatures are checked on
  every request. A change committed in the process (logging out, a
  revoked token, a password change, a deactivated or deleted user, a
  group joined or left, a permission given or taken, a group changed)
  drops what it affects at once, as does a bulk statement a session runs
  on the tables principals are read from (`update_rows`, `delete_rows`,
  Core `UPDATE` and `DELETE`); other processes see it within `TTL`, or at
  once with `STAMPS`, a Redis cache every process shares, stamped before
  and after each commit. A cache that fails, or loses its stamps, is
  never trusted: the database answers. Principals share their grants: an
  entry costs a few kilobytes whatever the user's role holds. `check`
  warns about a `TTL` above 5 seconds without `STAMPS` in staging and
  production; `auth_principal_cache_total` counts hits, misses,
  invalidations, and evictions by authentication class (`docs/auth.md`).
- `AUTH.MAX_LIVE_TOKENS` (100): a user holding that many live access
  tokens cannot make another personal token (`409 too_many_tokens`).

### Changed

- The weekly mutation run is split into eight shards that run side by side,
  each within its own time limit: the whole package no longer fits in one
  job. A test checks that every module falls in exactly one shard.
- domainkit requires FastAPI 0.143.1 (`<0.144`), SQLAlchemy 2.1.4
  (`<2.2`), and asyncpg 0.32.0 (`<0.33`, the `postgres` extra), and lists
  Starlette 1.8.0 (`<2`) itself, which FastAPI allows down to 0.46
  (ASYNC-R18, FX-18; ADR 0065). Requests run the same statements as
  before; each makes 15 more Python calls (FastAPI's telemetry hooks), and
  those that reach the database 5–14% fewer (SQLAlchemy's compiled paths).
  What projects meet:
  - FastAPI now depends on `opentelemetry-api` and traces requests itself
    when an OpenTelemetry SDK is configured. `get_asgi_application` turns
    that off, as it was, in its application and in every FastAPI
    application mounted in it: its spans record query strings and its logs
    exception messages. Trace requests with `opentelemetry-instrument`.
  - SQLAlchemy 2.1, in a project's own code: `Result.tuples()` is
    deprecated (rows unpack as they are); statements are typed by column
    (`Select[int, str]`); a session flushes before Core statements such as
    `text()` too; `Computed()` without `persisted=True` makes a VIRTUAL
    column on PostgreSQL 18 and warns on older servers; and a
    `postgresql://` URL without a driver now means psycopg 3, so keep
    writing `postgresql+asyncpg://`. SQLAlchemy's "What's New in 2.1" has
    the rest.
- `JsonFormatter` writes the same lines, byte for byte, with 35% fewer
  instructions, and `BackgroundHandler` copies a record for its queue with
  39% fewer: the JSON access line costs about 154,000 instructions a
  request instead of 181,000. An access line the logger's level drops is no
  longer built at all (PERF-02).
- The routes of the routers that `ROOT_ROUTER` includes are served from the
  top of the application (PERF-04, N5, FX-18; ADR 0060). FastAPI keeps each
  `include_router` call nested: it matched a request again at each level,
  and built the route's handler again for each request. Every route keeps
  its full path, access rules and other dependencies, tags, responses,
  response and route classes, and callbacks, and the OpenAPI document is
  the same byte for byte, on FastAPI 0.141, 0.143, and 0.158. In the
  project template's routes, a request makes 434 Python calls instead of
  1,638 before any middleware (500 instead of 1,674 on 0.143). `app.routes` now lists the routes, with their
  full paths, and `request.scope["route"].path` is the full template. A
  route added to an included router after the application is built is not
  served, and a router with frontend routes stays included. So does a
  router whose class has its own `matches`, `handle`, or other method that
  serves requests (an IP gate, a host rule, a tenant's context), at any
  depth, with the prefixes and rules of the routers above it; the routers
  around it are flattened.
- Each request does its work once (PERF-05, PERF-06, N1, FX-16). The client
  address is parsed once per request, for the refused-credential hold and
  the rate limit together, and the rate-limit groups and the proxy's trust
  of the latest 4,096 addresses are remembered (an IPv6 group took about
  25 µs to work out). Security headers are added in one append, request
  ids come from `os.urandom` (still 32 lowercase hex digits, at a quarter
  of the cost of `uuid4()`), and `MetricsMiddleware` keeps the handle of
  each series, for at most 4,096. With the template's eleven middleware a
  request makes 711 Python calls instead of 2,019, and with
  `DEFAULT_MIDDLEWARE` 314 instead of 476 (`docs/performance.md`).
- `MetricsMiddleware` labels a Starlette route or a mount inside an
  included router as it would one at the top: `unmatched`, or the mounted
  application's route. Nesting gave them an empty `route` label.
- Testing a pooled asyncpg connection (`POOL_PRE_PING`, still on by
  default) is SQLAlchemy's prepared `;` outside any transaction, where it
  was wrapped in `BEGIN` and `ROLLBACK`. Every checkout saves two round
  trips, and a read through a unit of work takes 4 instead of 6. Through
  PgBouncer, whose transaction pooling needs the transaction, the test is
  as before (PERF-03; ADR 0061).
- A request with an `Idempotency-Key` claims its key with one upsert that
  also replaces an expired key. It stores its response with one more
  statement, and both commit as they run (N3, FX-17; ADR 0062).
  - A first keyed request makes 2 transactions and 4 pool checkouts
    instead of 5 and 5: 16 round trips instead of 34 on PostgreSQL, with
    the cheaper ping.
  - A replay makes 2 checkouts instead of 4: 10 round trips instead of
    27.
  - Every guarantee holds as before. A committed operation never runs
    twice, a pinned key is never released, a cancelled request settles
    its key, and of requests racing with one key one runs.
  - Without messaging, a claim deletes expired keys after its upsert, so
    two claims through PgBouncer, whose statements share a transaction,
    never deadlock on each other's sweep. The pin before a commit on
    another database and the mark after it keep to the deadline in a task
    that only shares it.
- The audit trail makes a new subject's key with an insert that returns
  it: an audited commit about a new person costs 2 statements for the keys
  instead of 3. A transaction reads each subject's key once, and `forget`
  drops the keys that open transactions of the process hold (N4).
- A principal that is not kept is read in two queries: the token, then the
  user with every group and permission in one statement (was 4 or 5; N6,
  ADR 0059).
- `AuthenticationMiddleware` looks at the client address only while an
  address is held for refused credentials, and `TokenAuthentication` and
  `CookieAuthentication` read no settings for a request without their
  credentials (FX-16).
- Each request gets a user of its own, built as a query builds it. A
  user's `repr` leaves out its password.

### Deprecated

- `request.user.password` (and the password of any user authentication
  builds): check a password with `password_holds(user, password)`
  (`domainkit.contrib.auth.api`), or read the hash in a unit of work with
  `await uow.users.password_hash(user.id)`. It still returns the stored
  hash, warns with `DomainkitDeprecationWarning` the first time each
  request reads it, and is removed in 0.10. `has_usable_password()` keeps
  working, without the hash and without a warning.

### Removed

- `timeout`, `command_timeout`, and `connect_timeout` in a database's
  `CONNECT_ARGS`, deprecated in 0.8.0: they are refused when the
  application is built, as every argument the settings derive is. Set
  `CONNECT_TIMEOUT` and `COMMAND_TIMEOUT` instead. A `command_timeout`
  still applies when neither `COMMAND_TIMEOUT` nor `STATEMENT_TIMEOUT` is
  set, since nothing derives one then.

### Fixed

- Engines made and dropped on several threads at once (an event loop per
  thread) no longer race in SQLAlchemy's listener registry, which is not
  thread-safe and keys listeners by the ids of the target and the function.
  domainkit's listeners were one function shared by every engine, so a
  freed pool's reused id repeated a key: the registry's weakref callback
  raised `KeyError` ("Exception ignored", and an error under pytest's
  `filterwarnings = error`) and left stale entries. Each engine now listens
  with callables of its own.
- With `LOG_PARAMETERS` off, a value the driver cannot send no longer shows
  in the error: asyncpg's `invalid input for query argument $1: '4111...'`
  reads `$1: <hidden>`, with the reason kept, in the message, its `repr`,
  and logged tracebacks. A value PostgreSQL cannot read, which it quotes
  (`invalid input syntax for type integer: "<hidden>"`, how psycopg's
  untyped values are refused), is hidden too, and so is the `CONTEXT` it
  adds (`JSON data, line 1: ...`). A `DETAIL` that spans lines is dropped
  whole.
- On SQLite, a read-only unit of work begins its transaction before it
  reads, so DDL or a `WITH ... INSERT` sent as SQL text is rolled back with
  it. They committed at once, since SQLite's driver begins a transaction
  only before `INSERT`, `UPDATE`, `DELETE` or `REPLACE`. The reader now
  holds SQLite's read lock until it ends.
- `IDEMPOTENCY` with `TTL_SECONDS` below `LEASE_SECONDS` is refused at
  startup. A key that expired while its request still held it could be
  claimed by a retry, which ran the request a second time.
- `forget` no longer misses a subject's first key while a writer is making
  it: on PostgreSQL the key is made under a transaction advisory lock of
  the subject, which `forget` takes too, so it waits for the writer and
  deletes the key. It returned 0, and the key was committed after it.
  Known subjects take no lock; a new one costs one more statement. A key
  a lookup read before `forget` began is no longer kept for the rest of
  its transaction.
- `loaddata` reads a fixture's numbers with a fraction as decimals, so a
  money column gets every digit: `1234567890123456.78` was loaded as
  `1234567890123456.80`, through a float that `ExactDecimal` would have
  refused. Other columns get the same floats as before.

## [0.8.0] - 2026-10-21

### Added

- `domainkit.tasks`: `spawn(coro, name=...)` starts a background task in a
  clean context, keeps it, logs its failure once (`tasks_failed_total`),
  and stops it at shutdown; `supervise(factory, name=...)` restarts work
  that crashes, after a jittered backoff, until shutdown
  (`tasks_restarted_total`). Each event loop owns its tasks, and a closed
  loop is never kept alive by them (`docs/runtime.md`; ADR 0045).
- `SHUTDOWN_TIMEOUT` (15 s): one budget for the whole shutdown, split into
  phases (domain hooks, work in flight, background tasks, connections, and
  a final flush), each with a share no other phase can take.
  `lifecycle.register(name, close, phase=...)` adds closers, and
  management commands run them too (R22, N13; ADR 0046).
- `EVENT_HANDLER_TIMEOUT` (10 s), and `event_bus.subscribe(..., timeout=...)`
  per handler: an in-process handler still running then is cancelled,
  logged, and reported as `HandlerTimedOut`, and the request goes on
  (ASYNC-26).
- `AUDIT.SEAL_LATE_WINDOW`, `AUDIT.SEAL_BATCHES`, and
  `AUDIT.SEAL_BACKLOG_WARNING`, and the `audit_unsealed_rows` gauge: a
  sealing run is bounded, and a backlog is counted and logged (N6).
- `AUTH.HASHING_RESERVED` (1): places of the hashing queue kept for
  signed-in users changing their password and staff setting one, so a
  flood of logins cannot lock them out; `acheck_password`, `amake_password`,
  and `UserRepository.set_password` take `priority=True` (N7).
- `docs/runtime.md`: how domainkit runs async work.
- `WebSocketLimitsMiddleware` bounds every WebSocket (R08; `docs/live.md`;
  ADR 0055): `LIVE_MAX_CONNECTIONS` open connections per process and
  `LIVE_MAX_CONNECTIONS_PER_CALLER` per user or address (one more gets 503
  with `Retry-After`, or a 1013 close where the server cannot answer a
  handshake with a status); a send queue of `LIVE_SEND_QUEUE` messages, a
  client slower than `LIVE_SEND_TIMEOUT` closed with 1013;
  `WEBSOCKET_MESSAGE_RATE` messages per connection, closed with 1008 past
  it; and `WEBSOCKET_IDLE_TIMEOUT` and `WEBSOCKET_MAX_LIFETIME`, closed with
  1001. Each close says why, and cancels the handler, so none outlives its
  connection. `LiveLimits` sets a route's own limits.
- Open WebSockets and event streams are re-authorized every
  `LIVE_REAUTH_INTERVAL` seconds (N9; ADR 0056): the handshake's
  credentials are checked again and the route's `Require` rules re-run with
  the user loaded afresh, so a revoked or expired token, a deactivated
  user, or a removed permission closes the connection with 1008. A check
  that fails or hangs closes it with 1013.
- `domainkit.http.sse`: `EventSourceResponse` streams Server-Sent Events,
  with `ServerSentEvent` (`id`, `event`, `retry`, data on several lines,
  `ServerSentEvent.json`), `last_event_id(request)` for clients that
  resume, keep-alive comments after `SSE_KEEPALIVE` seconds, an end as soon
  as the client leaves, and the caps, queue, re-checks, and
  `SSE_MAX_LIFETIME` above.
- `domainkit.http.live_events`: domains stream their committed events to
  the clients that may see them, over an event stream
  (`live_events.stream`) or a WebSocket (`live_events.forward`). Each event
  passes the subscription's access rule on its own, goes out without its
  `Personal` fields unless `render` says otherwise, and a client that falls
  more than `LIVE_SEND_QUEUE` events behind is closed as too slow. Events
  reach the connections of the process that committed them (ADR 0057).
- `check` warns, in staging and production, when the application serves
  WebSockets without `WebSocketLimitsMiddleware`, and when
  `LIVE_REAUTH_INTERVAL` is `None`.
- `runserver` and management commands with an `async` handle, `runworker`
  among them, run on uvloop when the new `uvloop` extra is installed.
  `EVENT_LOOP` (`auto`, `asyncio`, `uvloop`) chooses, and
  `domainkit.utils.loops.loop_factory` gives other entry points the same
  loop (ASYNC-25).
- `domainkit.log.BackgroundHandler` and `BackgroundListener`: log records
  go on a bounded queue and a thread writes them, so a blocked stdout,
  file, or socket never stops the event loop. A full queue drops records
  and counts them, and says how many once there is room. `flush_logs()`
  waits for what is queued (ASYNC-10; ADR 0051).
- `domainkit.reporting.flush_reports()` waits for error reports still
  waiting for the reporting thread, as tests do before checking what a
  reporter received; the pytest plugin calls it, and `flush_logs()`,
  after every test.
- Blocking code is found before production (ASYNC-14, R07;
  `docs/runtime.md`, "Finding blocking code"):
  - with `DEBUG`, the application's loop runs in asyncio's debug mode and
    logs every step that holds it for `SLOW_CALLBACK_DURATION` (0.1 s);
  - `pytest --detect-blocking` fails a test that holds its loop that long
    (`--blocking-seconds` to raise the bar);
  - `check` warns about route handlers and dependencies of the installed
    domains written as `def`, which FastAPI runs on its thread pool;
    `domainkit.http.blocking.allow_sync` marks one that must stay sync;
  - projects made by `init` turn on Ruff's `ASYNC` rules.
- `Ledger.append(key, value, unchanged_since=...)` writes only if no
  revision of the key came after the one read, in one step in the ledger
  (immudb's `keyNotModifiedAfterTX` precondition), and raises the new
  `LedgerChanged` otherwise.
- Granian as a supported server: the new `granian` extra, `runserver
  --server granian`, and production commands and limits for uvicorn and
  Granian side by side in `docs/reference.md` ("Server limits"), with
  `--timeout-graceful-shutdown` and keep-alive (ASYNC-27, R12).
- In staging and production, `check` warns when an application that serves
  WebSockets runs under a server that reads messages larger than
  `MAX_REQUEST_BODY_SIZE` before the application can refuse them: uvicorn without a small enough `--ws-max-size`, or Granian
  with WebSockets on. It reads the server's `UVICORN_*` and `GRANIAN_*`
  variables, and a deployed application logs the same warning from the
  server's command line when it starts.
- CI runs the tests of state shared between threads, and of the
  framework's own threads, on free-threaded Python 3.14 (3.14t), with the
  GIL off throughout (R11). The whole suite crashes the 3.14.6t interpreter
  now and then, already at 0.7.0, so it is not run there yet.
- `domainkit.contrib.auth.acurrent_user`, the dependency behind
  `CurrentUser`, which FastAPI awaits instead of calling on its thread
  pool (ASYNC-09). `current_user` stays a plain function.
- `MESSAGING.BREAKER_FAILURES` (5) and `MESSAGING.BREAKER_COOLDOWN` (30 s):
  a broker that keeps failing trips the publisher's circuit breaker, and
  commits leave their messages to the relays, touching neither the
  database nor the broker, until a probe finds the broker answering again.
  It is logged once when it opens and once when it closes;
  `messaging_publisher_breaker_open` shows it, and
  `messaging_left_to_relay_total` counts the messages commits left to the
  relays, by reason (FX-12, N5; ADR 0042).
- `MESSAGING.NOTIFY`: on PostgreSQL, a commit wakes idle workers through
  `LISTEN`/`NOTIFY`, so a message starts right after its commit instead of
  on the next poll, and `POLL_INTERVAL` can be longer. Polling stays as the
  fallback (R20; ADR 0043).
- `MESSAGING.CLEANUP_BATCH_SIZE` (1,000) and `MESSAGING.METRICS_TTL`
  (15 s), below.
- `domainkit.contrib.messaging.Budget`, the concurrency budget `Worker` and
  `BrokerWorkers` share through `budget=`.
- `docs/runtime.md`, "Workers and the outbox".
- Request deadlines (ASYNC-11, R04; `docs/runtime.md`; ADR 0048): each
  HTTP request gets `REQUEST_TIMEOUT` seconds (30) until its response
  starts, through the new `DeadlineMiddleware`. Past them the work is
  cancelled, its unit of work rolled back and its idempotency key
  released, and the request is answered 503 `deadline_exceeded` with
  `Retry-After`, logged once. A route sets its own with
  `Depends(RequestTimeout(seconds))`, or none with `RequestTimeout(None)`;
  streamed responses, background tasks, and WebSockets are not cut off.
- `domainkit.deadlines`, to keep to the deadline anywhere: `remaining()`,
  `within(awaitable, seconds)`, and `async with timeout(seconds)`, bounded
  by the smaller of their seconds and the time left. PostgreSQL
  transactions lower `statement_timeout` to the time left (`SET LOCAL`),
  which `assert_num_queries` does not count.
- Load shedding (`docs/runtime.md`; ADR 0049): `ConcurrencyLimitMiddleware`
  lets each process work on `MAX_CONCURRENT_REQUESTS` requests at once
  (1000); the next wait up to `REQUEST_QUEUE_TIMEOUT` (1 second) behind at
  most `MAX_QUEUED_REQUESTS` (100), then get 503 `overloaded` with
  `Retry-After`. Health and readiness probes and `/metrics` are never
  refused (`CONCURRENCY_EXEMPT_PATHS`). New metrics:
  `http_requests_admitted`, `http_requests_queued`,
  `http_requests_shed_total`, and `http_request_deadlines_exceeded_total`.
- Client-side database timeouts (ASYNC-12): `DATABASES` entries take
  `CONNECT_TIMEOUT` (10 seconds, where asyncpg waited 60) and
  `COMMAND_TIMEOUT` (`STATEMENT_TIMEOUT` plus 5 seconds).
- `PGBOUNCER = True` for databases behind PgBouncer's transaction pooling:
  no statement cache, a unique name for each prepared statement, and the
  timeouts set in each transaction instead of the session. `check` warns
  in staging and production about a database that seems to go through
  PgBouncer without it.
- `domainkit.http.client`, outgoing HTTP calls (R09; the new `httpclient`
  extra; ADR 0050): pooled `HttpClient`s closed with the application,
  each call bounded by a timeout and by the request's deadline, retried
  with jittered backoff only when that is safe, a circuit breaker per
  host, the request id and trace context sent along, answers read up to
  `HTTP_CLIENT.MAX_RESPONSE_BYTES`, and logs without query strings or
  headers. A client made with `guard=True` calls only public addresses,
  checked as each connection opens; `stub(handler)` stubs every call in
  tests.
- `auditseal --late` (and `seal(..., late=True)` and `seal_all(late=True)`)
  seals the audit rows that committed later than `AUDIT.SEAL_LATE_WINDOW`,
  which no run could reach, into the chain, each seal flagged `late`;
  `auditverify` counts them. It reads the whole trail, so the worker never
  runs it on its own (ADR 0047).

### Changed

- Background work runs in a clean context: a `get_or_set` (or `@cached`)
  computation, a readiness round, and settling an `Idempotency-Key` no
  longer carry the first caller's request, actor, language, span, or key
  (FX-03, N2). A cached factory that read `current_request()` or the active
  language must now take them as arguments, in the key. `@cache_page` still
  runs the endpoint as the caller, whose user and language its key holds.
- Resource kinds, and the items of each kind, close at once at shutdown, so
  one that hangs keeps no other open; domain shutdown hooks run within a
  share of the budget, and one that overruns is cut off with a
  `TimeoutError` naming it. `DomainRegistry.lifespan` takes `stop_within`.
- `/ready` checks domains, databases, ledgers, and caches at once: a round
  takes one `READINESS_TIMEOUT` at worst, not four (ASYNC-16).
- Audit sealing looks for late rows `AUDIT.SEAL_LATE_WINDOW` (15 minutes)
  before the newest sealed row instead of a day, so its cost follows what
  is new, not how long the trail is: sealing 10 rows after 320k sealed over
  a day went from 493 to 15 ms on SQLite (FX-04, N6; ADR 0047). A row that
  commits later than the window stays unsealed, and `auditverify` reports
  it; raise the window if your transactions run that long. The chain and
  `auditverify` are unchanged.
- A refused password check's `Retry-After` is the time the hashing queue
  takes to drain, jittered, from 1 to 60 seconds, instead of always 1.
- `DEFAULT_MIDDLEWARE` and the project template list
  `WebSocketLimitsMiddleware`, so WebSockets of projects that set no
  `MIDDLEWARE` get the limits above. List it in an older project's
  `MIDDLEWARE`, after `RateLimitMiddleware`; with `domainkit.contrib.auth`
  installed, listing it before `AuthenticationMiddleware` is refused at
  startup, as it would count addresses rather than users.
- `build_logging_config` writes through `BackgroundHandler`, with a queue
  of 10,000 records (`queue_size`; `None` keeps writing on the caller's
  thread), and the settings `init` writes now configure logging with it:
  text lines, JSON lines in production, at `<PREFIX>LOG_LEVEL`. Lines are
  written a moment after the call; shutdown waits up to 5 seconds for
  them.
- Error reporters run on a reporting thread, in a copy of the caller's
  context, instead of inside the request: `report_error` returns at once,
  and at most 100 reports wait (more are dropped and counted). Shutdown
  waits up to 5 seconds for them. Tests that check a reporter call
  `flush_reports()` first (ASYNC-10).
- `CurrentUser`, a `ResourceRouter`'s unit of work and `FilterSet`, and the
  auth admin's user and group listings no longer hop to FastAPI's thread
  pool on every request: they are awaited, or built, on the loop.
  Domains made by `startdomain` declare `get_unit_of_work` and `caller` as
  `async def`; existing ones keep working, and `check` names them.
- The in-memory rate limiter, `AuthenticationMiddleware`'s
  refused-credential holds, and the password hashing pool's count take a
  lock, so event loops on several threads, as free-threaded Python runs
  them, share them safely (ASYNC-23).
- immudb ledgers answer readiness probes on a thread and a client of their
  own, so a probe no longer waits behind a slow write and reports the
  ledger unready; `close()` waits at most `TIMEOUT` for a busy client,
  which closes when its call returns, and no longer leaves a thread open
  when shutdown cuts it short. Audit anchors are written with
  `unchanged_since`, so a retried anchor, or one that landed after its
  caller gave up, is written at most once (ASYNC-13; ADR 0052).
- TLS contexts are built once per process for each set of TLS settings,
  never on the event loop while it serves: databases' when the application
  is built, RabbitMQ's and Redis's on a worker thread at the first
  connection. Redis connections share one context instead of building
  their own at each connect (ASYNC-21; `docs/runtime.md`, "TLS contexts").
  A process uses renewed certificate files after a restart.
- Relationships given to `map_imperatively` that leave `lazy` at
  SQLAlchemy's default (`"select"`), and the backrefs they make, now raise
  on an unloaded access (`lazy="raise"`) instead of trying a lazy load,
  which async sessions cannot do and which failed later with
  `MissingGreenlet` (ASYNC-22, R19; ADR 0054). `children`, `child`, and
  every relationship that names its `lazy` are unchanged. To upgrade, load
  such a relationship in the query (`selectinload`), pass
  `lazy="selectin"`, or keep lazy loading on purpose with `lazy=True`
  (for `AsyncAttrs.awaitable_attrs` or `run_sync`).
- ADR 0053 records why domainkit stays on FastAPI and Starlette rather
  than Litestar or plain Starlette, with what each costs a request, and
  what would change the decision (R16).
- **`MESSAGING.CONCURRENCY` is one budget for the whole worker process.**
  Every outbox's worker, `runworker --once`, and the deliveries of every
  queue a broker hands over draw from it, so a process runs at most
  `CONCURRENCY` messages at once. A process serving several outboxes or
  queues ran several times as many before; raise `CONCURRENCY` if it
  needs them (R23, ASYNC-19, IND-A4, IND-A5; ADR 0044).
- Relays and the publisher lock what they publish for a short lease, the
  broker's `CONNECT_TIMEOUT` plus `PUBLISH_TIMEOUT` and five seconds, and
  for `REPUBLISH_AFTER` only once the broker confirms. Whatever it did not
  confirm is unlocked at once, also when the publication is cancelled
  (FX-02; ADR 0042).
- At shutdown the publisher gets the time the drain phase always has, a
  fifth of `SHUTDOWN_TIMEOUT` (3 s by default), instead of a fixed five
  seconds. Its flush ends early enough (`RELEASE_TIMEOUT`, or half that
  time if less) for a publication it cuts short to be unlocked before the
  phase ends (ADR 0042).
- Committed messages are published `BATCH_SIZE` at a time, never a whole
  backlog in one statement (N11), and a relay logs one line per round
  instead of one per message.
- The publisher's task is supervised (`domainkit.tasks.supervise`): it runs
  in a clean context instead of the first committing request's, starts
  again after a crash, and ends when idle. Shutdown's drain phase closes
  the publishers, in management commands too; `MessagingConfig` no longer
  closes them in its `shutdown` hook (N12, R22).
- On PostgreSQL the outbox's clock decides what is due and when locks
  expire (`statement_timestamp()`), so workers on hosts whose clocks
  disagree neither run a message twice nor leave it waiting. Under
  `freeze_time` the frozen clock still decides (ASYNC-18; ADR 0043).
- Cleanup runs in a task of each polling or relaying worker, never in the
  claim loop, and deletes `CLEANUP_BATCH_SIZE` rows per statement; a batch
  that fails halves the next, so statement timeouts slow cleanup down but
  never stop it (FX-15, ASYNC-07; ADR 0043).
- Every RabbitMQ call has a deadline: `CONNECT_TIMEOUT` for opening
  channels, declaring, binding, setting the prefetch, and starting or
  stopping consumers, as for connecting (N4).
- The outbox gauges count the table at most once per `METRICS_TTL` in each
  process, and concurrent scrapes share one count (ASYNC-17).
- Workers run their tasks in `asyncio.TaskGroup`s: a failure in a worker's
  own task (a relay, cleanup) ends the worker with the error instead of
  being lost (R15).
- `DEFAULT_MIDDLEWARE`, and the settings `init` writes, list
  `DeadlineMiddleware` and `ConcurrencyLimitMiddleware` right after
  `RequestContextMiddleware`. Projects with a `MIDDLEWARE` of their own add
  them there; until they do, `check` warns in staging and production.
- The SSRF guard of `domainkit.contrib.webhooks` is now
  `domainkit.http.guard`, shared with the HTTP client;
  `domainkit.contrib.webhooks.guard` keeps its names, messages, and codes.
- The uvicorn example in `docs/reference.md` sets `--limit-concurrency
  1200`, above the default cap and queue, so the application refuses
  first and never refuses its probes.
- The idempotency keys table has a new `pinned` column: run
  `makemigrations` and `migrate` in projects that install idempotency. A
  commit on a database other than `IDEMPOTENCY.DATABASE` that raises now
  keeps its key even if nothing was saved, and retries get 409
  `idempotency_response_unavailable` instead of running again (ADR 0020).
- The audit seals table has a new `late` column: run `makemigrations` and
  `migrate` in projects that install audit. On PostgreSQL, seals are dated
  by the database's clock rather than the sealer's.
- Settings that had no upper bound have a generous one, and larger values
  are refused at startup: in `MESSAGING`, `TASK_TIMEOUT` (a day),
  `VISIBILITY_TIMEOUT` (two days), `POLL_INTERVAL` (an hour), `BATCH_SIZE`
  (10,000), `CONCURRENCY` (65,535, the largest prefetch RabbitMQ takes),
  `SHUTDOWN_TIMEOUT` (an hour), `REPUBLISH_AFTER` (a week),
  `RETENTION_SECONDS` (a year), and `CLEANUP_INTERVAL` (a day); in
  `IDEMPOTENCY`, `TTL_SECONDS` (30 days) and `LEASE_SECONDS` (a day); in
  `AUTH`, `HASHING_THREADS` (1,024) and `HASHING_QUEUE` (10,000); and
  `READINESS_TIMEOUT` (60 seconds). Defaults are unchanged.
- `LIVE_REAUTH_INTERVAL` and `LiveLimits(reauth_interval=...)` are at most
  3,600 seconds: a longer interval, which left a revoked token its
  connection for hours, is refused when the settings load or the route is
  defined. `None` still turns re-checks off. `docs/live.md` says what
  re-checks cannot do: a route that opts out keeps revoked tokens,
  single-use credentials fail the first re-check, and blocking work on a
  thread is not cut off at the 10-second bound.
- The logging queue keeps its last tenth for records at `WARNING` and
  above: while a stalled stream drops access lines, security events and
  errors still find room (`domainkit.log.WARNING_RESERVE`; ADR 0051). The
  drop notice counts by level: `Dropped 52 log records (50 INFO, 2
  WARNING): the logging queue was full`.
- `HttpClient(breaker=False)` keeps no circuits. A client's circuits are
  shared by all its callers, so one user's failing URLs on a host opened
  the circuit for every user of a shared guarded client: make one client
  per trust domain, and give one that calls URLs your users chose
  `breaker=False`.
- `HttpClient(guard=True, transport=...)` is refused with `ValueError`: the
  transport would skip the address checks. Tests use `stub()`. A client's
  or a call's `timeout` must be finite.
- Limits have upper bounds, well above their defaults, and settings past
  them fail at startup: `MAX_CONCURRENT_REQUESTS` and `MAX_QUEUED_REQUESTS`
  100,000; `REQUEST_TIMEOUT`, `REQUEST_BODY_TIMEOUT`, and `HTTP_CLIENT`'s
  `TIMEOUT` and `BREAKER_COOLDOWN` an hour; `MAX_BACKOFF` ten minutes;
  `MAX_RESPONSE_BYTES` 1 GiB; and a database's `STATEMENT_TIMEOUT`,
  `LOCK_TIMEOUT`, `IDLE_IN_TRANSACTION_TIMEOUT`, `CONNECT_TIMEOUT`, and
  `COMMAND_TIMEOUT` 2147483.647 seconds, the longest PostgreSQL takes.
- `deadlines.timeout`, `within`, and `Deadline` refuse seconds that are not
  a finite number (NaN, infinity) with `ValueError`; `None` is how to ask
  for no bound. Zero and less still raise `TimeoutError` on entry.
- The SSRF guard always refuses AWS EKS Pod Identity (`169.254.170.23`,
  `fd00:ec2::23`) and the AWS DNS resolver (`169.254.169.253`,
  `fd00:ec2::253`) as cloud metadata addresses, whatever networks are
  allowed.
- `MAX_CONCURRENT_REQUESTS` is documented as what it always was, a cap per
  event loop: a process serving on one loop admits the cap, one running a
  loop per thread admits it on each (ADR 0049).

### Deprecated

- `timeout`, `command_timeout`, and `connect_timeout` in a database's
  `CONNECT_ARGS`: set `CONNECT_TIMEOUT` and `COMMAND_TIMEOUT`. They still
  apply, with a `DomainkitDeprecationWarning`, and are refused from 0.9.

### Fixed

- A keyed request cancelled by a cancel scope (anyio's level-triggered
  cancellation) or a deadline kept its `Idempotency-Key` until the lease
  ran out, since its release was cancelled too. The response is now stored,
  or the key released, in a task of its own, within 5 seconds (R03).
- Without messaging, concurrent claims of new idempotency keys queued on one
  another's purge of the same expired keys; the purge now skips keys
  another request is deleting (`SKIP LOCKED` on PostgreSQL) (N14).
- At midnight UTC, every anonymous audited request waited for the first
  one's insert of the new day's pseudonym key. Sealing now makes the keys
  of today and tomorrow ahead of time, in a transaction of its own (N10).
- A password check whose caller was cancelled freed its place in the
  hashing pool while its thread still hashed, so the work in flight could
  exceed `HASHING_THREADS + HASHING_QUEUE`; a call now keeps its place
  until its hash is done (N7).
- After a lull, the in-memory rate limiter forgot every idle caller in one
  request: 100,000 of them cost that request 32 ms. It forgets at most 64
  per request now (IND-RL).
- The WebSocket message size check no longer copies a text message to
  measure it: ASCII is counted by its length, other text a chunk at a
  time (N9).
- A publication cut short at shutdown no longer leaves its messages locked
  for an hour (`REPUBLISH_AFTER`) while the log says the relay sends them
  (N1).
- A brokered worker whose broker was lost or stalls at shutdown no longer
  skips its running messages' grace period or hangs: stopping the
  consumers has five seconds, and the drain always runs (FX-05, N3,
  ASYNC-06). A delivery that arrives while stopping is given back instead
  of run.
- The in-memory broker's empty queues no longer hold the slots of busy ones
  (IND-A9).
- Publishers and brokers of event loops closed without a shutdown no longer
  keep those loops alive.
- A timeout under half a millisecond (`STATEMENT_TIMEOUT = 0.0004`) no
  longer reaches PostgreSQL as 0, which means no timeout at all.
- A keyed request whose changes committed on a database other than
  `IDEMPOTENCY.DATABASE`, but whose commit raised (its reply lost, or the
  request cancelled while it waited for it), released its
  `Idempotency-Key`, so a retry ran the changes again: a second payment,
  since 0.7.0. A key pinned for such a commit is never released now.
- A brokered worker whose acknowledgement of one message failed with
  aio-pika's own error, as it does once the delivery's channel closed,
  ended, cancelling its other running messages. RabbitMQ's answers that
  fail now raise `BrokerUnavailable`, and the worker logs any answer that
  fails and leaves the message for RabbitMQ to hand out again.
- An audit sealer whose clock ran ahead moved the high-water mark past the
  rows other hosts committed afterwards, which then stayed unsealed, open
  to change unnoticed. On PostgreSQL the mark is now capped by the
  database's clock (ADR 0047).
- A domain shutdown hook that ignored cancellation (a flush behind
  `asyncio.shield`, a retry loop that catches everything) held shutdown
  for good, since it ran inline. Each hook now runs as a task, and one cut
  off at its share is cancelled and left behind, so the hooks and phases
  after it still run.
- Event loops on several threads (free-threaded Python, a loop per thread)
  raced as each new one dropped the closed loops from the registries of
  background tasks, commit publishers, and brokers: a loop could fail with
  a `KeyError`, and on 3.14t the interpreter could crash. Those registries
  take a lock now.
- On PostgreSQL, a periodic slot that every worker queues, and that all
  but one insert skip for its dedupe key, sent a notification per worker,
  each waking every idle worker. Only rows the inserts return notify now.
- `brokers.close_all()`, which shutdown runs, closed no broker since 0.6:
  it called the base class's `close`, which does nothing, rather than each
  broker's own. RabbitMQ connections are closed at shutdown now.
- `ledgers.close_all()` had the same fault: immudb clients and their
  threads were left open at shutdown. Each ledger's own `close` runs now.
- Live events a client may not see no longer fill its buffer: one user's
  commit of more than `LIVE_SEND_QUEUE` events closed every other client
  subscribed to that type with 1013, and told them a burst had happened.
  Too slow now counts only the events the client's rule allows; the others
  wait for the rule in a buffer of their own, 16 times as large. Publishing
  lets the event loop run after each event, so a client that keeps up
  takes a large commit one event at a time.
- The caps on open WebSockets and event streams are counted under a lock.
  With event loops on several threads (free-threaded Python, or a server
  with a loop per thread), slots leaked until the process refused every
  connection, and closing one could raise `KeyError`. Loops that open an
  application's first connections at once now share one count.
- `EventBus.subscribe` checks for a repeat and adds the handler under a
  lock, as do `unsubscribe` and `clear`. Live events subscribed from loops
  on several threads at once could hook themselves onto the bus twice, and
  then sent every committed event to each client twice.
- A `UVICORN_WS_MAX_SIZE` (or `--ws-max-size`) of superscript digits, or of
  more than 4,300 digits, no longer stops a deployed application from
  starting, or `check` from running, with a `ValueError`: a value that
  cannot be read is unknown, and a failure to tell the server is logged.
- `check`'s warning about WebSocket messages the server reads past
  `MAX_REQUEST_BODY_SIZE` now covers uvicorn started by gunicorn
  (`-k uvicorn.workers.UvicornWorker`, or `uvicorn_worker.UvicornWorker`,
  read from the command line or `GUNICORN_CMD_ARGS`) and by `fastapi run`,
  which both read up to 16 MiB whatever `UVICORN_WS_MAX_SIZE` says; it
  says how to set the limit under each. Hypercorn is still not recognised
  (`docs/reference.md`, "Server limits").
- A process forked while another thread was adding a logging handler, or
  flushing the logs, could hang in its child before running anything: the
  child waited for a lock only the parent's thread could let go. The lock
  is now held across the fork, and the child makes a new one (ADR 0051).
- On a stuck log stream, exit took 15 seconds, not the 5 of
  `FLUSH_TIMEOUT`: Python's logging shutdown calls the handler's `flush()`
  and then `close()`, and every wait inside them had its own 5 seconds.
  They now share one deadline, and `close()` does not wait at all once the
  flush has given up.
- A few connections that sent their headers, then their body slowly or
  never, held every slot of `ConcurrencyLimitMiddleware` until
  `REQUEST_TIMEOUT`, so every other request was answered 503, without any
  credentials. A request now lends its slot to the next one while the
  application waits for a chunk of its body, and takes one back through
  the queue when the chunk arrives (503 `overloaded` if none frees in
  time). `RequestBodyLimitMiddleware` answers 408 `request_timeout`, and
  closes the connection, once the body has taken `REQUEST_BODY_TIMEOUT`
  (10 seconds) in all (ADR 0049).
- httpx2 and httpcore2 logged every outgoing call's full URL, query string
  and the secrets in it included, at INFO, a guarded client's too.
  `build_logging_config` sets their loggers to WARNING.
- The guard looked host names up on the event loop's default executor,
  and a lookup went on after the guard gave up on it: names whose DNS never
  answers held up `asyncio.to_thread` and new database connections for as
  long as the system's resolver tried. Lookups now run on 4 threads of the
  guard's own, at most 2 of one host; one past those bounds, or of a host
  whose lookup failed in the last 5 seconds, fails at once, as a
  connection error (ADR 0050). The threads stop at shutdown.
- With `STATEMENT_TIMEOUT = None`, a deadline over 24.8 days failed every
  PostgreSQL transaction (`statement_timeout` out of range), and an
  infinite one raised `OverflowError`: a transaction asks for at most
  2147483647 milliseconds.
- On Linux, guarded clients and webhooks trusted the CAs `SSL_CERT_FILE`
  and `SSL_CERT_DIR` name, though they ignore proxies from the
  environment. They trust `CA_BUNDLE`, or the system's CAs, only.
- `base_url` kept calls on its origin but not under its path: a relative
  URL such as `../admin` or `%2e%2e/admin` reached another path of the
  host. A `.` or `..` segment, spelled out or percent-encoded, now raises
  `UnsafeURL`.
- On free-threaded Python, a client's circuit breaker could let two trial
  calls through at once, or fail with `KeyError`, and the shedding warning
  lost counts: both change only under a lock now.
- Behind `--root-path`, the probes in `CONCURRENCY_EXEMPT_PATHS` were
  counted against the cap and could be refused; paths are compared
  without the root path.

## [0.7.0] - 2026-10-20

### Added

- `SECRET_KEY` and `SECRET_KEY_FALLBACKS`, and `domainkit.signing`, Django's
  `signing`: `Signer`, `TimestampSigner`, and `dumps`/`loads` for compact,
  URL-safe, signed JSON, with HMAC-SHA256 under a key derived for each salt
  and constant-time checks. A new key signs while the old ones still verify,
  so a key can be replaced without logging anyone out. `derive_key` gives
  other uses a key of their own (`docs/reference.md`, "Signing"; ADR 0028).
- `domainkit.contrib.mail`, Django's `django.core.mail` through the outbox:
  `send_mail` and `EmailMessage(...).send(uow)` queue a task with the
  change, so a message goes out if and only if it commits, and the worker
  sends it, retrying up to `MAIL.MAX_ATTEMPTS`. Backends send over SMTP
  (STARTTLS or TLS, with the certificate checked, and timeouts), to the
  console, or to `mail.outbox` for tests. Messages refuse header injection
  and are bounded (DJ-48; `docs/mail.md`; ADR 0029).
- Mail templates: `render_mail("billing/invoice", context, to=[...])` reads a
  subject, a text body, and an optional HTML body from `templates/mail/` in
  domain packages or `MAIL.TEMPLATE_DIRS`, and escapes values in the HTML
  (DJ-49).
- `domainkit.contrib.notifications`: text messages and push notifications
  queued the same way, with `SmsBackend` and `PushBackend` ports for a
  project's provider, and console and memory backends (DJ-50).
- Password reset, email verification, and signing up (DJ-38;
  `docs/auth.md`, "Password reset"), with `domainkit.contrib.auth.recovery`
  and `domainkit.contrib.auth.signup` routers to mount:
  - `POST /auth/password-reset` always answers 202, in the same time,
    whether or not an account uses the address; the worker mails a link
    whose token is signed, expires (`AUTH.PASSWORD_RESET_TIMEOUT`), and
    ends once used or once the user logs in. Confirming it ends every
    session.
  - `AbstractUser.email_verified`, set by a mailed link and cleared when
    the address changes. Run `makemigrations` and `migrate` for the new
    column.
  - `POST /auth/signup`, off until `AUTH.SIGNUP_ENABLED`, takes a username
    and an address, no password, checks them at once, then answers 202
    and mails the address a link (`AUTH.SIGNUP_URL`), or word that the
    username or address is taken. `POST /auth/signup/confirm` with the
    link's token and a password, checked by the validators, makes the
    account, its address verified: until then no account exists, so no
    one but the address's owner learns whether it has one, and whoever
    reads the mail chooses the password, so asking with someone else's
    address gains nothing. A pending signup reserves no username; pending
    signups wait in the new `domainkit_pending_signups` table (run
    `makemigrations` and `migrate`), never in fixtures, and expired ones
    are purged hourly.
  - Requests that send mail count against the new `AUTH.EMAIL_RATE`, and
    token checks against `AUTH.LOGIN_RATE`; resets, verifications,
    signups, and refusals are audited.
- Browser sessions in cookies (DJ-43): `CookieAuthentication` reads an
  opaque token from an HttpOnly, Secure, SameSite cookie that
  `POST /auth/session` sets and `DELETE /auth/session` clears. The
  session and CSRF cookies are named `__Host-dk_session` and
  `__Host-dk_csrf` wherever the prefix holds, so a sibling subdomain
  cannot plant either; names a project sets are kept, and `check` warns
  in staging and production about one without the prefix. Unsafe
  requests it authenticates must come from a trusted origin (their own,
  `ALLOWED_HOSTS`, or `AUTH.CSRF_TRUSTED_ORIGINS`) and send a CSRF token
  signed for the session (`X-CSRF-Token`); others get `403 csrf_failed`,
  which never counts against `RATE_LIMIT` as refused credentials do, so
  another site cannot hold a browser's address. Header-token clients are
  unaffected.
- Two-factor logins (DJ-41; `docs/auth.md`, "Two-factor logins"; the `mfa`
  extra): an authenticator app (TOTP) and single-use recovery codes, from
  `domainkit.contrib.auth.mfa`. Once on, `/login` and `/auth/session`
  answer 202 with a signed challenge that `/auth/mfa/login` (or
  `/auth/session/mfa`) takes with a code. Codes work once, checks are
  throttled per user, turning it off needs the password and a code, and
  secrets are stored encrypted with AES-GCM under a key derived from
  `SECRET_KEY` (ADR 0030). `resetmfa` turns it off for a user who lost it.
  Run `makemigrations` and `migrate` for the new tables. Their tables are
  never in fixtures.
- `domainkit.encryption.SecretBox(purpose)`: AES-256-GCM under a key
  derived from `SECRET_KEY` for each purpose, bound to the value's owner,
  for secrets the database must give back (`docs/reference.md`,
  "Encryption"; the `mfa` extra). Values under a fallback key still
  decrypt, and say so, to be encrypted again.
- Privacy covers users (`docs/auth.md`, "Users' personal data"):
  `AbstractUser`'s username, email, and names are marked `Personal`, so
  `privacy export` and `privacy erase` reach them. Erasing a user also
  deactivates the account, makes its password unusable, deletes its tokens
  and second factor, and keeps its username taken: the name becomes a keyed
  digest that creating a user or signing up checks.
- Scoped personal tokens (DJ-33): `domainkit.contrib.auth.personal_tokens`
  lets users list, create, and revoke their own tokens at `/auth/tokens`,
  shown once (marked `no-store`, so no idempotency key replays one) and
  limited to scopes (permission keys they hold) if they wish. Permission
  rules allow a scoped token only what both its user and its scopes
  allow; `HasScope` and `IsUnscoped` are new rules, and
  `createtoken --scope` makes scoped tokens. Tokens record when they were
  last used, at most every five minutes. Run `makemigrations` and
  `migrate` for the tokens' new columns.
- Client certificates (DJ-44): `ClientCertificateAuthentication` logs in
  the user a certificate names, by its SHA-256 (`addcertificate`) or by its
  subject's common name. The certificate comes from the ASGI TLS extension,
  or from `AUTH.CLIENT_CERT.HEADER`, believed only from the addresses in
  `AUTH.CLIENT_CERT.PROXIES`, never from all of `TRUSTED_PROXIES`; staging
  and production take single addresses there (`docs/auth.md`, "Client
  certificates", has an nginx example).
  `ProxyHeadersMiddleware` keeps the proxy's address in
  `scope["domainkit.peer"]` for this.
- Migration tooling (`docs/reference.md`, "Commands"):
  `makemigrations --empty` writes a revision to fill in by hand, for data
  migrations; `migrate --plan` lists what would run, `--sql` prints it as
  SQL, rendered offline by Alembic, and `--fake` records a revision without
  running it; `squashmigrations` replaces a run of revisions with one that
  keeps the newest id and both directions, and refuses runs it cannot copy
  correctly (branches, helper code, irreversible downgrades) or that a
  database is in the middle of.
- `dumpdata`, `loaddata`, and `flush`, with JSON fixtures written table by
  table, parents first. State never goes in a fixture (the audit trail,
  tokens, the outbox, idempotency keys, webhook subscriptions with their
  signing secrets, and deliveries), `flush` keeps the audit trail,
  and both `loaddata` and `flush` say what they will replace or delete,
  and refuse in staging and production without `--force`. `seed` runs the
  new `DomainConfig.seed()` hook of each installed domain.
- `dbshell` opens `psql` or `sqlite3` on an alias, with the password in
  `PGPASSWORD`, never on the command line, and refuses production without
  `--allow-production`. `diffsettings` lists the settings that differ from
  the defaults, secrets hidden.
- `openapi` prints or writes the OpenAPI schema, and `openapi --diff
  OLD.json` lists the changes that break clients (removed operations or
  success statuses, new required parameters or fields, narrower request
  values, removed or wider response fields) and exits 1 if there are any. With
  `OPENAPI_OPERATION_IDS = "route_name"`, which new projects set, operation
  ids are route names, stable when routers move (ADR 0031).
- API versions and deprecations (`domainkit.http.versioning`):
  `versioned_router("v1")`, and `Deprecation(since=..., sunset=...,
  link=...)` for routers and routes, which sends `Deprecation`, `Sunset`,
  and `Link` headers (RFC 9745, RFC 8594) on every response of those
  routes and marks them deprecated in OpenAPI.
- `ERROR_FORMAT = "problem+json"` answers every error as RFC 9457 problem
  details, with `code`, `request_id`, and `errors` kept as extension
  members, and documents it in OpenAPI (ADR 0032). The default body is
  unchanged.
- `domainkit.debug.DebugToolbarMiddleware`, for development: each response
  gets `Server-Timing` with its SQL time and count, a statement repeated
  `DEBUG_TOOLBAR.REPEATED_QUERIES` times in one request is logged as a
  likely N+1 (its shape only, never values), and a request sent with
  `X-Debug-Profile: 1` can leave a cProfile file. Staging and production
  refuse to start with it; new projects list it in their local settings.
- `domainkit.contrib.flags`: feature flags on for everyone, no one, listed
  users or groups, or a stable percentage of users, and typed dynamic
  settings, stored in the database and cached (ADR 0033). Check them with
  `flag_enabled` or `RequireFlag`, and change them with `manage.py flags`
  or a staff API guarded by permissions; every change is audited.
- Records' history from the audit trail (`docs/contrib.md`, "History"):
  `history(target=...)` or `history(aggregate=("product_id", id))`, with
  each entry's field changes; `auditlog --target` and `--aggregate`; and
  `GET /history` for staff with the new `audit.view_history` permission.
  Personal values stay pseudonyms, also for people who were forgotten.
- Cron schedules: `@periodic(cron="0 3 * * 1-5", tz="Africa/Cairo")` runs
  a task at wall-clock times in a time zone, with a parser of our own
  (lists, ranges, steps, names, macros). Daylight saving time never runs a
  time twice or skips one: a skipped time runs at the end of the gap, a
  repeated one once (ADR 0034). On Windows, install `domainkit[tzdata]`.
- Tasks set their own `timeout`, `max_attempts`, `backoff` (`Backoff`,
  exponential or fixed, capped, with jitter), `retry_on`, and
  `dont_retry_on`, over `MESSAGING`'s (DJ-52). `current_attempt()` tells a
  task which run it is.
- Task `priority` (-100 to 100, higher first, as in Django's tasks),
  `chain(a, b)` to run a task after another succeeds with its result, and
  a per-task `rate` ("10/minute") counted by `RATE_LIMIT_BACKEND` (ADR
  0035).
- `messages --pending`, `--locked`, and `--queue` list waiting, running,
  and published messages; `messages --cancel ID` cancels one no worker
  runs; `messages` shows each queue's lag, and so does the new
  `messaging_outbox_lag_seconds` gauge (DJ-53).
- Outbox messages carry the W3C trace context of their commit, and the
  worker's span continues it; malformed `traceparent` and `tracestate`
  values are dropped (`domainkit.tracing.trace_headers`, `remote_parent`).
- `domainkit.contrib.messaging.testing`, a pytest plugin with
  `captured_events`, `outbox` (`assert_queued`, `run_queued` and
  `arun_queued`), and `execute_deferred`, for sync and async tests.
- `domainkit.contrib.webhooks` sends exposed domain events to subscribed
  URLs through the outbox, so only committed changes are sent. Deliveries
  are signed as Standard Webhooks, retried with backoff, logged per
  attempt, and replayable (`webhooks replay`). URLs must be https and
  resolve only to public addresses, and the checked address is the one
  connected to. Staff manage subscriptions through an admin API; secrets
  are shown once, in answers marked `no-store` that neither caches nor
  idempotency keys keep, never in reprs (nor are URLs, which may hold
  tokens), and stored encrypted under a key derived from
  `SECRET_KEY`, which webhooks require (ADR 0036). Install
  `domainkit[webhooks]`, which brings httpx2 and cryptography.
- `domainkit.http.ratelimit.Throttle`, a FastAPI dependency that limits one
  route, or the routes of one scope, per user, per address, or per key a
  function returns, apart from `RATE_LIMIT` and with the same backends. Over
  the rate, the route answers 429 `throttled` with `Retry-After` (DJ-25;
  `docs/cache.md`, "Throttles for one route").
- `@cache_page` caches what a GET endpoint returns, keyed by path, query,
  language, and the headers it lists, with an `ETag` and 304s for
  `If-None-Match`. Callers with credentials are never served shared
  answers; `vary_on_user=True` keeps an entry per user. Cached pages
  replace the `no-store` default with `Cache-Control` of their own
  (DJ-57; `docs/cache.md`, "Caching whole responses").
- Caches and rate limits on a Redis Sentinel group
  (`redis+sentinel://host-1,host-2/mymaster/0`) or a Redis Cluster
  (`redis+cluster://node-1,node-2`), `rediss+` for TLS, with
  `SENTINEL_PASSWORD` for Sentinels that ask for one. The deployment checks
  hold them to the rules for one server (DJ-58; `docs/cache.md`).
- Error details and validation messages in the caller's language
  (DJ-59; `docs/i18n.md`, ADR 0039): `LocaleMiddleware` picks a language of
  `LANGUAGES` from `Accept-Language` and answers with `Content-Language`;
  domainkit ships English and Arabic. Projects mark their own text with
  `gettext`, `ngettext`, `pgettext`, and their lazy variants
  (`domainkit.i18n`), keep catalogs per domain or in `LOCALE_PATHS`, and
  compile them with `manage.py compilemessages`, which needs no GNU tools
  and checks them (`--check` in CI). New projects install the middleware.
- File storage and uploads (DJ-27; `docs/uploads.md`, ADR 0040):
  `domainkit.storage`, with `FileSystemStorage` and `MemoryStorage`
  configured in `STORAGES`, stores files under random names it makes,
  writes them atomically, and never follows symbolic links.
  `domainkit.http.uploads.save_upload` stores a FastAPI `UploadFile` only
  if its first bytes are of a type the route accepts, stops it at
  `max_size` while it streams, and cleans the client's file name;
  `serve` streams a stored file from a guarded route as a sandboxed
  download. FastAPI's form parser comes with the new `uploads` extra.
- `TIME_ZONE`, with `localtime()`, `localdate()`, and `timezone.override()`
  to show stored UTC times where the business is, and `number_format`,
  `date_format`, `time_format`, and `datetime_format` that write numbers
  and dates as English and Arabic do (DJ-60; `docs/i18n.md`).
- `domainkit.contrib.privacy` (DJ-69; `docs/privacy.md`, ADR 0041) answers
  privacy requests across every installed domain from `Personal` markers
  on aggregate fields: `manage.py privacy export` writes what is held about
  a subject, `privacy erase` erases it through units of work, with a
  `PersonalDataErased` event, and forgets the subject in the audit trail,
  and `privacy purge` applies the retention policies domains declare in
  `DomainConfig.RETENTION`, never on append-only tables. Every operation is
  audited.
- Repositories answer queries (`docs/queries.md`, ADR 0022):
  `filter`, `first`, `count`, `exists`, and `paginate` take criteria built
  from fields, `(F("status") == "open") & (F("total") >= 10)`, with
  Django's lookups, orderings, and named `Specification`s. In-memory fakes
  evaluate them exactly as SQL does (missing values, NULL ordering, the id
  as tie-breaker, exact amounts), and unknown fields or ill-typed values
  fail before any SQL runs (DJ-11).
- `Pagination` and `PageRequest` page any repository over HTTP with
  versioned, typed cursors, or with bounded offsets, and optional counts;
  a tampered cursor is a 422 `invalid_cursor` (DJ-21, ADR 0023).
- `FilterSet` turns `?status=open&total__gte=10&ordering=-placed_at&search=acme`
  into a query, with lookups allowed per field, typed and documented in
  OpenAPI, and bounded; anything else is a 422 `invalid_filter` (DJ-20).
- `add_all` and `remove_all` on repositories, and `update_rows` and
  `delete_rows` on the SQL adapter for changes that deliberately bypass
  aggregates and their events (DJ-14).
- Soft delete: aggregates that inherit `SoftDeletable` are marked deleted by
  `remove`, left out of reads unless `include_deleted=True`, and brought
  back with `restore` (DJ-15).
- `get_for_update(id, nowait=..., skip_locked=...)` and `RowLock` lock rows
  until the commit (`FOR UPDATE` on PostgreSQL, emulated by in-memory
  repositories), and `UnitOfWork.savepoint()` undoes part of a unit of work
  that fails (DJ-18).
- Read replicas: a `DATABASES` entry names its `REPLICAS`, and read-only
  units of work (`readonly=True`) read from them while writes stay on the
  primary (DJ-17, ADR 0024).
- `table_for` derives a table from an aggregate's fields and type hints,
  with `MaxLength` and `Digits` for lengths and amounts, enums stored by
  value, and single-field value objects stored as what they wrap; `children`
  and `child` map the child entities an aggregate owns (DJ-10, DJ-12).
- `AsyncTestClient`, which serves the application in the test's own event
  loop, with its lifespan, and the `async_client`, `async_client_for`, and
  `async_admin_client` fixtures, for async tests (DJ-62). New projects'
  tests show one.
- Factories for aggregates in `domainkit.testing` (DJ-63): `Factory`, with
  `Sequence`, `LazyFunction`, `LazyAttribute`, `SubFactory`, and
  overrides (`price__amount=`). `build()` makes an object in memory;
  `await create()` stores it through a unit of work, in memory or SQL, and
  commits.
- `domainkit.clock` (DJ-64, ADR 0037): `now()`, `today()`, and
  `monotonic()`, the one clock the framework reads, and `freeze_time`, a
  context manager and fixture that stops it or moves it. Token expiry (JWTs
  included), idempotency leases, rate windows counted in the process,
  memory cache lifetimes, and the timestamps of audit entries, events,
  messages, and evidence all follow it. Domain code may import it. The
  database's own `now()` is not frozen.
- `assert_num_queries` and `assert_max_queries` (DJ-65), context managers
  and fixtures that count the SQL statements a block runs, and list them
  when the count is wrong.
- PostgreSQL test databases of their own (N-02, ADR 0038): the plugin
  creates `test_<name>` (or the entry's new `TEST_NAME`), migrates it,
  and drops it after the run; `--keepdb` keeps it for the next one, and
  each pytest-xdist worker gets its own (`test_<name>_gw0`).
- `docs/testing.md`, on the plugin and everything above.
- Resources (DJ-19, ADR 0025; `docs/resources.md`): `ResourceRouter`
  lists an aggregate (with a `FilterSet` and `Pagination`) and reads one,
  and `command_route(Command, service=..., body=...)` runs a command
  through an application service, answering `201` with `Location` when it
  creates one. Resources nest (`parent=`; the path gives a nested
  aggregate's parent, which no `crud` write changes), narrow what each
  caller reaches (`scope=`), and, for reference data only, offer generic
  create, change, and delete (`crud=True`), which record `RecordCreated`, `RecordChanged`,
  and `RecordDeleted`. `ModelActions(Model)` asks for the model permission
  of each route's action, so a command needs `change`, not `add`. A
  resource without a rule fails the deny-by-default check.
- `schema_for(Aggregate, include=, exclude=, name=, write=, partial=)`
  derives request and response schemas from the domain class, with
  `from_domain` (DJ-22): value objects become what they wrap, ids and
  `Reference[XId]` their id type, children nest.
- Partial changes (DJ-23): `PatchModel` fields default to `UNSET`, so a
  field left out differs from one set to null, and `PATCH` routes take
  `application/merge-patch+json`. Commands take `UNSET` for what was not
  sent.
- ETags and conditional requests (DJ-24, N-05): an aggregate's answer has
  `ETag: W/"<version>-<digest>"`, `If-None-Match` answers `304`, and a
  write whose `If-Match` names another version is `412
  precondition_failed`, checked in the transaction that makes the change.
  A resource may require `If-Match` (`428 precondition_required`).
- Field policies (DJ-31): `FieldPolicy(read_only=, visible=, editable=)`
  shows a field, or lets a partial change set it, only for callers granted
  its permission, and OpenAPI says so.
- `Reference[CustomerId]`, an id-only reference to another aggregate:
  `table_for` indexes its column, and `schema_for` describes it.
- `domainkit.http.error_responses(*codes)`, the OpenAPI entries of the
  errors a route answers with.
- `query_route(Query, handler=..., path=...)` serves a CQRS query on a
  resource: its fields from the query string (bounded, unknown parameters
  refused), one aggregate or `{"items": [...]}` back, and what the caller
  may not reach or see left out.
- Batches (DJ-35, ADR 0027): `batch_route(Command, service=...)` serves
  `POST /<plural>/batch` for up to `max_items` items, answering `207` with
  each item's result; `"atomic": true` runs them in one transaction and
  undoes them all if one fails.
- Exports (DJ-34): `ResourceRouter(..., exports=("csv", "ndjson"))` answers
  listings as CSV or NDJSON by `Accept` (`406` when nothing fits), every
  match streamed up to `max_export`; CSV cells that could run as formulas
  are neutralised.
- `domainkit.contrib.objectpermissions` (DJ-30, ADR 0026; `docs/auth.md`,
  "Grants on single objects"): grants of one permission on one object to a
  user or a group, as audited events; `ObjectPermissionBackend` for
  `has_perm(user, perm, obj)` and `HasObjectPermissions`; `grant_permission`
  and `revoke_permission` within what the actor holds; and `VisibleTo`,
  criteria the SQL repositories compile to `EXISTS` and in-memory ones
  read from the grants they are given, with `visible_scope` for resources.
- `domainkit.db.query.register_compiler` compiles criteria of your own.
- `domainkit.contrib.admin` (DJ-46; `docs/resources.md`, "The generic
  admin"): `admin.register(Product, ..., list_display=, filters=, search=,
  commands=[admin.command(...)])` serves a model under the admin's prefix,
  for staff with its model permissions: lists, reads with tags, commands
  run by application services (never raw field edits), queries, and each
  item's history from the audit trail. `GET /admin/meta` describes the
  models, fields, filters, and commands the caller may use, as JSON
  Schema. Users, groups, and permissions keep their own admin.
- `ResourceRouter` takes `list_fields=` (lists show only those fields) and
  `name_prefix=` (route names, hence operation ids), records its
  `operations`, and offers `fetch()` for routes of your own.
- Generators (DJ-01; `docs/resources.md`, "Generators"): `startaggregate`
  adds an aggregate to a domain, every layer and its tests, with fields
  from a registry of kinds (`--field price:decimal:10:2`, `--field
  parent:ref:Category?`; `register_field_type` adds kinds); `addcommand`
  adds a task command (method, event, use case, route, tests), `addquery`
  a query by fields (handler, route, tests), and `addevent` an event with a
  durable handler. Names are checked, files are edited where their syntax
  tree says, and nothing is written unless every edit fits. What they
  generate passes ruff, `mypy --strict`, and its own tests.

### Changed

- **Staging and production need `SECRET_KEY`** (DJ-37): startup fails
  without one, or with one shorter than 50 characters, with fewer than 5
  different ones, or starting with `domainkit-insecure-` or
  `django-insecure-`. Fallbacks are held to the same rules, and the errors
  never quote a key. The production settings that `init` creates read it
  from `<PREFIX>SECRET_KEY`; set it before upgrading a deployment.
- `/auth/me` and the admin API show `email_verified`.
- `IsAdminUser` and `IsSuperuser` refuse credentials limited to scopes,
  and the admin API, `/auth/password`, and `/auth/logout` with
  `everywhere` refuse them altogether: what staff grant is bounded by
  their permissions, never by a token's scopes, and a scoped key must not
  change or lock its account.
- Integrity errors other than a taken value no longer answer 500: a broken
  foreign key is 409 `referenced`, a NULL in a NOT NULL column 422
  `missing_value`, and a refused CHECK constraint 422 `check_violation`,
  on PostgreSQL and on SQLite. A taken value outside a unit of work is a
  409 too. Fields are named, values never are.
- A `DuplicateValue` answers with one `errors` entry per field
  (`loc: ["body", "<field>"]`), and its `fields` are the names the API
  uses: a column's `info={"field": ...}`, else the attribute a mapped class
  keeps it in, else the column's name.
- `shell` is an asyncio console where `await` works at the top level, and
  it imports each installed domain's models, repositories, and units of
  work; `-c` runs code and exits.
- The audit trail gained an index on `target`, for histories. Run
  `makemigrations` and `migrate` in projects that install audit.
- `check` and startup report operation ids that two routes share.
- The outbox has `priority` and `started_at` columns, an index, and a
  `cancelled` status: run `makemigrations` and `migrate` in projects that
  install messaging before deploying.
- `MESSAGING.RETRY_BASE_SECONDS` and `RETRY_MAX_SECONDS` are at most a
  year, and a retry delay no longer overflows after a thousand attempts.
- `messages` prints a `cancelled` count and each queue's lag after the
  counts by status.
- The auth admin's `/users` and `/groups` listings use the new paginator:
  cursors issued before the upgrade are refused once with
  `invalid_cursor`, and the client starts again.
- **On PostgreSQL, the test fixtures no longer use the database the test
  settings name**: they create `test_<name>` on the same server and drop it
  after the run, so the test role needs `CREATEDB`. A role without it can
  create the database once and run with `--keepdb`. SQLite is unchanged.
- The `client` fixture is a `domainkit.testing.TestClient`, a subclass of
  Starlette's whose requests see the caller's context variables, so a
  frozen clock reaches the application.
- JWTs' `exp`, `iat`, and `nbf` are checked by domainkit, on
  `domainkit.clock`, as PyJWT checked them on the host's clock.
- Repositories check the version a request expects (`If-Match`) when they
  hand out an aggregate; outside a resource's route nothing is expected,
  so nothing changes.
- The auth admin's user and group listings and reads are resources: reads
  answer with an `ETag` and `304` for `If-None-Match`. Their bodies are
  unchanged.
- `startdomain` generates a domain on the resource kit (DJ-12): 15 files
  instead of 24, and an API layer of about 90 lines instead of 210. The
  aggregate has a child-entity collection (notes, with `children()`) and an
  id-only reference to its owner (`Reference[OwnerId]`); its tables come
  from `table_for`; its resource pages, filters, and searches the listing,
  tags reads, and takes `PATCH` (a merge patch), `DELETE`, and commands
  that answer with `Location` and honour `If-Match`. The example management
  command and domain settings are gone. In a project laid out as `init`
  makes it, `startdomain` also adds the domain to `INSTALLED_DOMAINS` and
  mounts its router (`--manual` prints the steps instead).
- **Startup refuses `AuthenticationMiddleware` without
  `RequestContextMiddleware`** listed before it, as it already refused the
  two in the wrong order: without a request context no actor is bound, and
  rate limits, idempotency keys, the audit trail, and `@cache_page` could
  not tell users apart. Add `domainkit.http.middleware.RequestContextMiddleware`
  ahead of it (projects from `init` have it).
- **`FilterSet` refuses orderings whose cursors could be too long**, when
  declared: each field of `ordering_fields` and `default_ordering` needs a
  bound (`Annotated[str, MaxLength(n)]` on text, `Digits` on amounts), and
  the default ordering, and the `max_ordering` fields that take most room,
  must fit in `MAX_CURSOR_LENGTH`. The error says what to declare. The cap
  is now 4,096 characters (from 2,048): three text fields of
  `MaxLength(150)`, in any script. `cursor_length(ordering, model)` gives
  the bound of any ordering (ADR 0023).
- **Email addresses are checked** wherever one is taken: creating a user
  (the admin API, `create_user`, `createsuperuser`), changing one, signing
  up, and asking for a reset. An address is one plain ASCII address,
  `user@example.com`, without a display name, comment, or list; anything
  else is refused with `422 invalid_email` (`InvalidEmail`), so mail goes
  to exactly the address an account shows. Addresses compare by their
  ASCII letters alone: on PostgreSQL, a look-alike (the KELVIN SIGN, which
  `lower()` makes a `k`) no longer takes another's signup or reset, and a
  reset skips, and logs by user id, a stored address that cannot take mail
  rather than failing for every account of it. Addresses stored before
  stay as they are.
- `check` also lists warnings that installed domains return from the new
  `DomainConfig.deployment_warnings()`, in staging and production.
- Usernames that start with `erased-`, in any case, are kept for accounts
  privacy erased: signing up, the admin API, `createsuperuser`, and
  `create_user` with any user model refuse them (`422 invalid_username`),
  and erasing a user whose name only starts with `erased-` erases it too.
- `/auth/me` with a token limited to scopes lists only the permissions
  both its user and its scopes allow, and `/auth/email-verification`
  refuses scoped tokens (403).
- Staging and production refuse `AUTH.PASSWORD_RESET_URL`,
  `EMAIL_VERIFICATION_URL`, and `SIGNUP_URL` over `http://`, and `check`
  warns there when `AUTH.LOGIN_RATE` or `AUTH.MFA.RATE` is `None`.

### Fixed

- The in-memory rate limiter lets exactly its limit through at one clock
  reading (a frozen clock). It added seconds to the reading, which rounded
  with the reading's magnitude and with `period / limit`: 8 of 9 a second
  passed, or 1 of 2 where the reading was near a power of two. It now
  counts what a caller owes in requests. A frozen `monotonic()` count also
  starts at a whole second, so ticks of whole seconds add exactly.
- Autogenerated revisions keep SQLAlchemy's own `Interval` and `PickleType`
  columns as they are. An `Interval` was written as `DateTime`, a timestamp
  on PostgreSQL instead of an `INTERVAL`.
- `client_for` and `admin_client` work in async tests. They called
  `asyncio.run`, which fails inside a running event loop (N-01).
- Clients from `client_for` and `admin_client` run on the `client`
  fixture's event loop. Each of their requests started an event loop of its
  own, with a database engine nothing closed (ASYNC-24).
- `privacy export` and `privacy erase` (and `export_subject`,
  `erase_subject`) refuse an id spelled otherwise than it prints where
  domains name people by UUID or integer: an upper-case or braced UUID, one
  without dashes or with `urn:uuid:`, or `042`, `+42`, ` 42`. Such a subject
  matched the person's UUID or integer columns but not the same id in text
  columns or audit keys, and the erasure still reported success. The
  message gives the form to use.
- `@cache_page` keys entries by the scheme and host too, as Django's
  `cache_page` keys absolute URLs. A page whose answer depends on the host
  (a tenant chosen by `Host`, a link built with `url_for`) was served to
  every host and scheme from the entry the first request filled.
- `@cache_page` counts the credentials an authentication middleware
  accepted (`request.auth`) even when no actor is bound. A user logged in
  by a cookie or a client certificate, behind `AuthenticationMiddleware`
  without `RequestContextMiddleware`, sent no `Authorization` header and
  had no actor, so a page without `vary_on_user` stored their answer as
  public and served it to everyone.
- A page always answers with its cursor. One sort value longer than about
  1,500 bytes (an 800-letter Arabic title) made `encode_position` refuse
  the cursor, so every page that ended on it was a 500. Cursors going out
  are no longer refused; `FilterSet` bounds them when declared instead
  (see Changed), and one that is still too long, from a value longer than
  its field declares, is logged and refused when it comes back.
- Amounts in cursors are written in plain notation. Amounts of seven or
  more places, such as zero at eight (`0E-8`), were written with an
  exponent, which decoding refused, so the page after one was a 422
  `invalid_cursor`.
- `dumpdata --output` writes a new file only its owner can read (mode
  `0600`), and refuses a path that exists, a symbolic link too, as
  `privacy export --output` does. It wrote with the umask's mode (`0644`
  usually) a fixture that can hold personal data and password hashes, and
  followed a link planted where it would go. Delete an old fixture before
  dumping again.
- `FilterSet` bounds amounts by their field's `Digits` (40 digits either
  side of the point without), so an amount out of range is a 422
  `validation_error`. `?price__gte=1e999999999` reached the database
  driver, which could not send it to PostgreSQL: a 500.
- `FilterSet` leaves `<name>__<lookup>` alone for every name it does not
  filter, as it leaves other parameters. A field of the model it did not
  filter was a 422 naming the field, and an unknown name passed, so a
  client could list the model's fields (`password`, `deleted_at`) one
  guess at a time.
- A route's own `rule=` that looks at objects (`HasObjectPermissions`, a
  rule with `has_object_permission`) is asked about the aggregate, on
  command, batch, and query routes and the admin's commands and queries.
  It was asked only whether the request may proceed, so any logged-in
  user ran the command on any object. A query answering several
  aggregates leaves out those the rule refuses.
- `visible_scope` (and `visible_to(..., request=)`) matches nothing for
  credentials whose scopes leave the permission out. A token scoped to
  something else saw every object its user may see.
- `crud=True` creates keep to the `FieldPolicy`: `read_only` fields are
  not in the body, and fields in `editable` need their permission, as in
  a `PATCH`. A caller allowed to add set protected fields.
- `crud=True` creates and changes stay in the caller's `scope`: one that
  would put the aggregate out of the caller's reach (another tenant's) is
  `403 permission_denied`, and the transaction rolls back.
- Fields hidden from a caller by `visible` are not theirs to set, by
  `PATCH`, a `PatchModel` command, or a generic create (`403
  field_not_editable`); they could overwrite what they could not see.
- A use case that commits without loading the aggregate `If-Match` names
  writes nothing: the unit of work refuses the commit. The request failed
  after the commit, with the stale write stored.
- The generic admin refuses any user model (a subclass of
  `AbstractBaseUser`), `Group`, and `Permission`, whatever the `label=`,
  and a `label=` that names another model than the class's. A swapped
  user model registered and showed its password hash and permissions.
  `schema_for` refuses fields holding a password hash unless excluded.
- CSV exports guard formulas behind spaces of any kind, line breaks, and
  invisible characters, and in full-width and small forms (`＝`, `﹣`).
  NDJSON escapes U+0085, U+2028, and U+2029, which split a line for some
  readers.
- Deleting an object's row deletes the grants on it, so an object stored
  later with the same id does not inherit them. `delete_rows` still
  leaves them (docs/auth.md).
- `/admin/meta` leaves out the nested schemas only hidden fields used.

## [0.6.2] - 2026-10-19

### Added

- `domainkit.contrib.auth.mark_public`, which marks a mounted application
  (a `StaticFiles`) or a Starlette endpoint public on purpose, as `Public`
  does for FastAPI routes.

### Changed

- Run uvicorn with `--no-proxy-headers` (`docs/reference.md`, "Server
  limits"), so `TRUSTED_PROXIES` alone decides whom `X-Forwarded-For` is
  believed from: uvicorn reads it itself otherwise, and its
  `FORWARDED_ALLOW_IPS="*"` lets any client choose its address. The
  documented command also adds `--no-server-header`.
- `docs/auth.md` says what the per-account login limit costs (anyone can
  hold one user's logins back), and that a WebSocket handshake counts as a
  `GET` for `IsAuthenticatedOrReadOnly` and `ModelPermissions`.

### Fixed

- Deny by default covers every route (ADR 0021). Mounted applications and
  Starlette's own `Route` and `WebSocketRoute` were skipped, so a mounted
  FastAPI application, or a `StaticFiles`, was served to anyone without
  being listed. Applications mounted with routes are now checked route by
  route, and what takes no dependencies is listed (`MOUNT /static`) unless
  marked with `mark_public`. **This may stop an application from starting
  in staging and production** until such mounts are marked or guarded:
  there is no safe way to tell which were meant to be public.
- An address held back for refused credentials could still find out which
  guess was right when a project's own class reads them from another
  header than `Authorization` (an `X-API-Key`): wrong guesses got 429, and
  the right one was served. Such credentials now get the same 429 while the
  address is held, even when they hold.
- Two staff demoting, deactivating, or deleting the last two active
  superusers at once could both succeed, leaving none: each counted two.
  The count now locks the superusers' rows until the commit, so the second
  waits for the first and sees one (PostgreSQL).
- `IdempotencyMiddleware` stores and replays header values as the Latin-1
  bytes ASGI defines them as. A stored header that was not UTF-8 (a
  `Location` with an accented letter) raised once the response had gone
  out, and the key was never answered.

## [0.6.1] - 2026-10-19

### Fixed

- The audit trail's seals (`docs/contrib.md`):
  - A row holding a lone surrogate (which JSON can escape) no longer stops
    sealing for good, and such a value in a personal field no longer fails
    the transaction that records it. Digests of every other row, and every
    other pseudonym, are unchanged. Evidence refuses such facts as not
    plain JSON.
  - A host whose clock runs ahead no longer stops anchoring for good (one
    anchor dated in the future held every later one back) or leaves the
    rows written before a future-dated row unsealable.
  - `auditverify` fails when anchoring stopped: seals that no anchor covers
    a day past `AUDIT.ANCHOR_EVERY` (`Report.unanchored`, and `verify`'s
    new `every`).
- Clients behind trusted proxies can no longer choose their address, and
  so their rate limit, login throttle, and the address the audit trail
  records: every line of `X-Forwarded-For` now counts, not only the first,
  which the client sends itself when a proxy adds a line of its own
  (HAProxy's `option forwardfor`). `X-Forwarded-Proto` is read at the
  client's place, not from its left, which the client could set. Hops with
  a port (`203.0.113.9:4711`, `[2001:db8::1]:443`) and IPv4 addresses
  mapped into IPv6 (`::ffff:10.0.0.2`) are understood, where they used to
  put every caller in the proxy's count.
- The production settings `init` creates ask for `TRUSTED_PROXIES`, so
  callers behind a load balancer are no longer all counted, and throttled,
  as one. Staging and production refuse a network that holds every address
  (`0.0.0.0/0`, `::/0`), and `check` warns about a rate limit without
  trusted proxies.
- `Require(HasObjectPermissions(...))` no longer lets anyone in, anonymous
  callers included, where a route never checked an object. The rule now
  needs an active user who logged in, as it says; each object is still
  checked with `check_object_permissions`. The deny-by-default check
  counted such routes as guarded.
- `Require(...)` and `AuthenticatedUser` work on WebSocket routes. They
  failed every handshake with an internal error (close code 1011), even
  for users they allow; a refused handshake now gets the same 401 or 403
  response as a request.
- Refused tokens are rate limited: they were answered before
  `RateLimitMiddleware` counted anything, so one address could try tokens
  without limit, each one a database lookup (2,400 a second in the stress
  test). They now count per address against `RATE_LIMIT`; past it the
  address gets 429, and its credentials are refused unread until
  `Retry-After`. Opaque tokens not of the issued form are refused without
  a lookup.
- The per-username login limit counts the account: look-alike names that
  find the same account under NFKC (full-width or mathematical letters)
  each had a count of their own. Password changes have their own count per
  user, so someone else's failed logins no longer stop a user changing
  their password.
- Throttled logins are audited once per count and period, not each time:
  a flood of attempts wrote a row per attempt to the append-only trail.
- Logging in as a user without a usable password (created without one, or
  with a hash no configured hasher reads) costs one hash, like a wrong
  password, so response times no longer tell such accounts apart.
- Idempotency kept working tokens: a login or refresh sent with an
  `Idempotency-Key` stored its response, tokens and all, in
  `domainkit_idempotency_keys` for a day (for good without messaging),
  against "the table never holds a working token". `/auth/login` and
  `/auth/refresh` now answer `Cache-Control: no-store` (as OAuth 2 asks),
  and such responses are never stored: a retry gets 409
  `idempotency_response_unavailable`.
- Callers without an actor who authenticate with another header than
  `Authorization` or `Cookie` (an `X-API-Key`) shared keys behind one
  address, so one could be replayed another's response. Their keys are
  apart now, by the new `IDEMPOTENCY.CREDENTIAL_HEADERS`. Keys sent
  without an actor before the upgrade are not found after it.
- A refusal (a 4xx) that committed nothing is no longer stored, so a retry
  runs again once the caller has logged in or the conflict is gone, and
  keyed requests to paths that do not exist store nothing. Without
  messaging, which purges expired keys hourly, each new key now deletes up
  to 100 expired ones; they used to stay until their key came back.
- Database errors that are not bugs no longer answer 500 nor go to the
  error reporters:
  - A request that waits past `POOL_TIMEOUT` for a connection, or whose
    statement `STATEMENT_TIMEOUT` or `LOCK_TIMEOUT` cancels, or that loses
    to a deadlock or a serialization failure, gets 503 `database_busy`
    with `Retry-After: 1`.
  - A value the database refuses (a NUL character, which anyone could send
    to `/auth/login`; text too long; a number out of range) gets 422
    `invalid_value`.
  - Network databases wait 10 s for a pooled connection, not 30, unless
    `POOL_TIMEOUT` says otherwise.
- With `LOG_PARAMETERS` off, the `DETAIL` PostgreSQL adds to constraint
  errors (`Failing row contains (...)`) is dropped: it repeated the row's
  values, personal data included, in logs and error reports.
- A flood of `/ready` probes runs one round of checks at a time: each
  probe that missed the cache ran every check itself (500 probes, 500
  rounds; behind a busy pool, failing probes took replicas out of
  rotation). A result is now reused for `READINESS_CACHE_SECONDS` from
  when its round ended, not began.
- `RedisBackend` lets requests through when Redis refuses the count (out of
  memory, a read-only replica after a failover), as it does when Redis is
  down: every request got a 500. Redis is then skipped for the cache's
  `FAILURE_COOLDOWN`.
- `http_requests_total` and the other request metrics count methods
  other than the nine standard ones as `OTHER`: servers that let any
  method through let callers make a label each.
- The admin API counts what every backend in `AUTH.BACKENDS` grants when
  staff change a user, and what the change itself would grant. Staff with
  `change_user` could take over a user whose permissions came from another
  backend (a role or directory backend) by setting their password. While
  other backends are configured, only superusers change inactive users.
- Deactivating a user deletes their tokens: reactivating them used to bring
  back every session, including a stolen one.
- The last active superuser can no longer be demoted, deactivated, or
  deleted (`409 last_superuser`).
- A JWT refresh token used twice, the sign of a stolen one, is logged as a
  security warning and audited (`RefreshTokenReused`), besides ending its
  login as before.
- Logins that upgrade the same password's hash at once no longer refuse
  each other with 409 `concurrent_update`: the new hash is stored without
  the user's version, and only over the hash that was checked, so a
  password changed meanwhile stays.
- The pytest plugin's database fixtures, and `restore_snapshot`, refuse
  settings whose `ENVIRONMENT` is not `"test"`. Without `domainkit_settings`
  they used to migrate the development database down to base and delete
  every row.
- Snapshots connect as the schema owner (`MIGRATION_URL` when set), as
  emptying append-only tables needs, and quote table names properly: a
  name holding `"` broke the statement that turns triggers off. An
  in-memory `MIGRATION_URL` becomes the same test file as `URL`.
- Personal data in the audit trail (`docs/contrib.md`):
  - A `Personal` marker on an optional field (`Annotated[str, Personal(...)]
    | None`) was ignored, and the value stored in clear; such fields are
    now pseudonyms. A marker inside a field (in a `list[...]`, or on a value
    object's own fields) raises `TypeError` instead of being ignored.
  - The worker no longer keeps, logs, or reports the values a pydantic
    validation error quotes: such errors become `InvalidData`, naming fields
    and rules only. A task's refused arguments used to be stored in
    `last_error` for as long as the dead letter was kept.
  - Work no request queued (periodic tasks, messages from commands) is
    audited as `system`, not `anonymous`.
- Settings keep their secrets:
  - `repr(settings)` and its dumps hide domain settings (`AUTH`, `EVIDENCE`,
    ...), whose keys (`JWT.SIGNING_KEY`, `SUBJECT_KEY`) they showed in clear.
  - `Env` errors no longer quote the value, which may come from a `_FILE`
    secret, nor chain the error that did.
  - A URL whose password holds `/` is refused without quoting it; the port
    Python read was part of the password.
- Infinity and NaN are refused in every setting and by `Env.float`:
  `STATEMENT_TIMEOUT=inf` failed at the first connection, and an infinite
  `MESSAGING.SHUTDOWN_TIMEOUT` never ended a shutdown.
- `CONNECT_ARGS` cannot turn a timeout off by spelling it differently
  (`STATEMENT_TIMEOUT`, `-c lock-timeout=0`, `--lock-timeout=0`): PostgreSQL
  reads setting names in any case, and `-` as `_`.
- `startdomain` refuses a plural that would name an existing command
  (`startdomain partition`, `startdomain message`, `--plural check`): the
  generated command silently replaced it.
- `makemigrations` refuses a message that could end the revision's
  docstring (triple quotes, a backslash, more than one line).
- `ExactDecimal` (`docs/databases.md`):
  - A value with more places or digits than the column holds is compared
    exactly (`where(price > Decimal("9.995"))`, `in_`, `between`) instead
    of failing the query with a 500, on every database.
  - On SQLite, an amount added to, subtracted from, or compared with an
    amount of another scale, or an integer column, is counted in the same
    units: `price > rate` compared cents with ten-thousandths, and
    `price + quantity` added a count to cents.
- Evidence: a record read under a key that does not end with its own id,
  or listed by `for_subject` under a prefix while about another subject, is
  `EvidenceTampered`. Anyone with the ledger's write credentials could
  copy a real record under another key or subject, and it read as intact.
  Keys from an earlier `NAMESPACE` or `SUBJECT_KEY` still read.
- The worker runs a durable handler only for the event types it is
  subscribed to; an outbox row pairing it with another event is
  dead-lettered instead of handing it an event it does not expect.
- A WebSocket message over `MAX_REQUEST_BODY_SIZE` closes the connection
  with 1009 (message too big) and is logged as a security event: messages
  were limited only by the server (16 MiB for uvicorn). The application is
  told the client left.
- Releases run one at a time and are never cancelled halfway
  (`release.yml` concurrency).

## [0.6.0] - 2026-10-18

### Added

- Routes deny by default (ADR 0017, `docs/auth.md`): with auth installed,
  every route must say who may call it, with `Require(...)`, a
  `CurrentUser` or `AuthenticatedUser` parameter, or the new `Public`.
  `check` lists the routes that do not; development and tests log them,
  and staging and production refuse to start. `AUTH.REQUIRE_ROUTE_RULES =
  False` turns this off. Routes that read the user also need
  `AuthenticationMiddleware`, and startup reports them while it is missing.
- `DomainConfig.check_application(app)`, a hook that reports problems with
  the built application, as above.
- The admin API, `domainkit.contrib.auth.admin.router`: staff list, create,
  change, and delete users and groups, set passwords, end sessions, and
  grant groups and permissions. Each endpoint needs staff (or a superuser)
  and its model permission. Only superusers make or change superusers, and
  other staff grant only the permissions they hold and cannot change users
  or groups that hold more. Changes are audited (`UserUpdated`,
  `UserDeleted`, `GroupDeleted` are new events). Custom user models work:
  staff see only the fields a model lists in `EDITABLE_FIELDS` and
  `READ_ONLY_FIELDS` and change only the first, so a new field such as a
  `risk_flag` stays out of the admin API until listed. Values are checked
  against their type annotations, and `clean_field()` tidies them.
- `client_for` and `admin_client` in `domainkit.testing`: clients that
  authenticate as new users with chosen permissions, or as a superuser.

- Requests are authenticated (ADR 0016, `docs/auth.md`):
  `AuthenticationMiddleware` sets `request.user` and makes the user the
  request's actor, so rate limits, idempotency keys, logs, and the audit
  trail are per user. Authentication classes (`AUTH.AUTHENTICATION_CLASSES`):
  opaque tokens by default, stored as digests and revocable at once, and
  JSON Web Tokens with rotating refresh tokens through the new `jwt` extra.
- `domainkit.contrib.auth.api.router`: log in (throttled by `AUTH.LOGIN_RATE`,
  refusals audited), refresh, log out (here or everywhere), `/me`, and
  password changes that end the user's other sessions.
- Route rules modelled on Django REST framework's permission classes:
  `IsAuthenticated`, `IsAdminUser`, `HasPermissions`, `ModelPermissions`,
  `HasObjectPermissions`, and more, combined with `&`, `|`, `~`, and checked
  by the FastAPI dependency `Require`; `CurrentUser` and `AuthenticatedUser`.
- The `createtoken` and `cleartokens` commands, and an hourly purge of
  expired tokens with messaging.

- `domainkit.contrib.auth` and `domainkit.contrib.contenttypes`, Django's
  users, groups, and permissions for REST APIs (ADR 0015, `docs/auth.md`):
  permissions per content type (`<domain>.<action>_<model>`, and those a
  class declares in `Meta`) created by `migrate`; groups; superusers;
  backends with object permissions; a swappable user model
  (`AUTH.USER_MODEL`, `AUTH.USER_TABLE`), including users who log in with
  their email; and the `createsuperuser` and `changepassword` commands.
- Password hashing with Django's hashers and formats (PBKDF2 by default,
  scrypt, and Argon2 through the new `argon2` extra), re-hashed on login
  when settings change and run off the event loop, and Django's four
  password validators, with its list of 20,000 common passwords.
- `DomainConfig.migrated(connection, database)`, a hook `migrate` calls
  after upgrading each database (Django's `post_migrate`), and
  `DomainConfig.holds_table(table)`, which keeps tables a domain no longer
  uses out of migrations.
- `domainkit.management.base.run_async`, for sync commands that reach the
  database between prompts.

### Changed

- The `Ledger` port has a new abstract method, `create(key, value)`: write
  revision 1 of a new key, or raise the new `LedgerKeyExists`. Custom ledger
  adapters must implement it.
- New projects (`domainkit init`) install content types and auth, list
  `AuthenticationMiddleware`, and mount `/auth` and `/admin/auth`; their
  test settings use the fast password hasher.
- Domains generated by `startdomain` guard every route with its model
  permission (`<domain>.view_<model>`, `add_`, `change_`), and their tests
  call as a superuser (`admin_client`) and check 401 and 403. Projects
  generated earlier need auth installed to use them.
- The login and refresh endpoints are marked `Public`.
- `changepassword` ends every token of the user.
- Run `makemigrations` and `migrate` in projects that install auth: tokens
  have a table, `domainkit_tokens`.
- Evidence (ADR 0018): staging and production need the new
  `EVIDENCE.SUBJECT_KEY`, a secret of 32 characters or more kept for as
  long as the evidence. `subject_digest`, `evidence_key`, and
  `LedgerEvidenceStore` take the subject key.

### Fixed

- The audit trail was not tamper-evident: the tables' owner could drop the
  append-only triggers, rewrite rows, and restore them unnoticed. Rows are
  now sealed into a hash chain (`domainkit_audit_seals`, by the worker every
  `AUDIT.SEAL_EVERY` or by `auditseal`), each chain's head is anchored on a
  ledger (`AUDIT.LEDGER`, immudb) at most once per `AUDIT.ANCHOR_EVERY`, and
  `auditverify` reports changed or missing rows, broken links, rewritten
  anchors, and rows never sealed. `protect(op, audit_seals)` makes the seals
  append-only too. Run `makemigrations` and `migrate` for the new table
  (`docs/contrib.md`, "Tamper evidence").
- The audit trail kept personal data forever: usernames and clients'
  addresses went into an append-only table, which an erasure request cannot
  touch. They are now pseudonyms (an HMAC with a random key of the person
  they are about), and forgetting a person deletes their key, so their
  pseudonyms can no longer be linked to them: `auditforget <user id>`, or
  `forget()`. Anonymous callers' addresses belong to one key per day, which
  `auditforget --anonymous-days N` drops. Event fields are classified with
  the new `domainkit.ddd.Personal` marker, and `record()` takes `personal`
  details. Run `makemigrations` and `migrate` for the new
  `domainkit_audit_keys` table (`docs/contrib.md`, "Personal data").
- SQLite lost digits of amounts: it keeps `NUMERIC` as a float, so
  `12345678901234567.89` came back as `12345678901234568.00`, silently, in
  the default development and test database. The new
  `domainkit.db.ExactDecimal(precision, scale)` column is `NUMERIC` on
  PostgreSQL and whole minor units on SQLite, and gives the same results on
  both for comparisons, ordering, sums, and arithmetic with plain numbers.
  It refuses floats and values it would have to round (`docs/databases.md`,
  "Exact amounts").
- Tests saw what earlier tests committed: the `domainkit.testing` plugin
  migrated the database once per session and never emptied it, so results
  depended on test order, and the tests `startdomain` generates had to
  filter rows and raise limits. The new `db` fixture puts every database
  back as `migrate` left it before each test (the rows `migrate` wrote, such
  as permissions, stay), and `client`, `client_for`, and `admin_client` use
  it. On PostgreSQL this works with the audit trail's append-only triggers.
- Work done over a WebSocket was audited as `"system"`: request context was
  bound for HTTP only, so the authenticated user never became the actor of
  the audit trail, logs, or outbox headers, and connections had no request
  id or access log line. WebSocket connections now run in a request context
  (method `WS`), the request id is sent with the accept, and each connection
  is logged when it ends. An error before the accept closes it with 1011
  rather than reaching the server. WebSocket handshakes were not rate
  limited either: each now counts as a request, and one over the limit gets
  the 429 as a denial response, or a 1013 close where the server cannot
  send one.
- Evidence refused `Decimal` amounts and accepted floats, the wrong way
  round for money: `facts={"amount": Decimal("10.00")}` raised, while
  `{"amount": 1e21}` was hashed into the receipt as `1e+21`. A `Decimal` is
  now written as a fixed-point string (`"10.00"`), and floats are refused
  anywhere in the facts, with a `ValueError` naming where; non-finite
  decimals are refused too. Records written with floats before still read
  and verify, and evidence read from a record writes those same bytes, so a
  record already queued in the outbox is recorded unchanged.
- A value that broke a unique constraint answered 500 `internal_error`:
  only lost optimistic-locking races were translated, so two requests that
  both checked a username before either committed failed the second with an
  unhandled `IntegrityError`. A unique or primary key violation, at commit
  or at an earlier flush in the unit of work, now raises `DuplicateValue`
  (`domainkit.ddd`), a 409 with the code `duplicate_value` that names the
  constraint and fields but never the values. A constraint, or its column,
  can name its own subclass with `info={"duplicate": ...}` for a code of its
  own; auth's usernames and group names raise `UsernameTaken` and
  `GroupNameTaken`, which are now `DuplicateValue` subclasses.
- Stopping a worker could leave messages locked for minutes. `SIGTERM` waited
  for running messages with no deadline (`TASK_TIMEOUT` is 240 seconds), so
  platforms killed the worker first, and a cancelled worker skipped the wait
  altogether: either way the messages stayed hidden until
  `VISIBILITY_TIMEOUT` and the attempt counted. Running messages now get
  `MESSAGING.SHUTDOWN_TIMEOUT` (20 seconds); then, or on a second signal,
  they are cancelled and go back to the outbox, due at once and with the
  attempt given back. Brokered workers do the same with their deliveries.
- Shutdown stopped at the first resource that failed to close: an error
  disposing one database left the others, the caches, and the brokers open,
  and a hung close stalled the rest. Every cache, database, ledger, and
  broker is now closed even when another fails, each kind within five
  seconds, and failures are logged. Management commands close ledgers too.
- A cache outage multiplied the load on the database: while a cache failed,
  `get_or_set` and `@cached` called the factory once per caller, so a
  stampede on a hot key ran the query once for every request. Concurrent
  callers now share one computation while the cache fails too, and its
  value is not written back after a failed read.
- Closing a cache left its computations running, writing to a closed store
  after shutdown. `close()` now cancels them before closing the store, and
  callers still waiting compute their own value. Caches also close before
  databases, ledgers, and brokers, which their computations use.
- With a broker, every write waited on it: publishing a commit's messages
  ran inside the request, after the commit, so a slow or stopped RabbitMQ
  added up to its connect and publish timeouts to each write. Each commit
  also built a new worker, which declared the exchange and queues again.
  Commits now hand their messages to a background publisher per event loop
  and return. It keeps one worker per outbox, so queues are declared once,
  and stops taking messages past 10,000 waiting, leaving them to the
  relays. The messaging app's shutdown gives it five seconds to finish.
- RabbitMQ brokers used for the first time by several requests at once
  opened a connection and a publishing channel for each, and kept only the
  last. A lock now makes them share one.
- `CONNECT_ARGS` could turn database TLS and timeouts off unseen. It
  replaced the driver arguments derived from the settings, so a deployment
  with `SSLMODE = "verify-full"` passed the deployment checks while
  `CONNECT_ARGS = {"ssl": False}` connected in plain text, and adding any
  `server_settings` (asyncpg) or `options` (psycopg) dropped the statement,
  lock, and idle timeouts. `CONNECT_ARGS` is now added to the derived
  arguments: `server_settings` and `options` are merged with the timeouts,
  and setting what `SSLMODE`, the TLS files, or a timeout already set is
  refused with an `ImproperlyConfigured` naming the setting to change. The
  check runs when the application is built, before any query.
- Recording the same evidence twice at once could look like tampering:
  `record` read the key, then appended, so concurrent writers both saw it
  missing and both appended. The key had two revisions, and `verify()`
  raised `EvidenceTampered`. The ledger now writes the key only if it is
  new (`Ledger.create`, a `keyMustNotExist` precondition on immudb), so one
  writer creates it and the others get its receipt, or an
  `EvidenceConflict` if their record differs.
- A keyed request whose client disconnected mid-upload ran on the part of
  the body that had arrived, as if it were all of it, and the result was
  stored under the key, so the client's retry with the whole body got 422.
  `IdempotencyMiddleware` now stops at the disconnect: no key is claimed
  and nothing runs.
- A crash left an idempotency key in progress for a day (ADR 0020): every
  retry got 409 until the key expired. Keys are now leased for
  `IDEMPOTENCY.LEASE_SECONDS` (60 by default) under a random token. Once
  the lease ends, a retry takes over a key whose request died before
  committing, and gets 409 `idempotency_response_unavailable` if it
  committed. A request that ran past its lease and lost its key cannot
  commit: its unit of work fails with 409 `idempotency_key_taken_over`.
  The idempotency table has new `token` and `locked_until` columns: run
  `makemigrations` and `migrate`.
- An idempotency key could run a request twice (ADR 0019): the key was
  released whenever the response was not stored (a 5xx, an exception, or a
  body over `MAX_RESPONSE_BYTES`), even after the request had committed its
  changes, so the retry made them again. A SQL unit of work that commits
  during a keyed request now marks the key committed in the same
  transaction (right after it, on another database), and a committed key is
  never released. A retry whose response could not be kept gets 409
  `idempotency_response_unavailable`. The idempotency table has a new
  `committed_at` column: run `makemigrations` and `migrate`.
- Evidence could keep personal data forever (L4): any subject was stored
  as written, and keys used a plain SHA-256 of it, so a guessed email
  address found its records. Subjects must now be internal ids (letters,
  digits, and `_ . : / -`), which refuses email addresses, names, and
  phone numbers with a `+`. Keys use an HMAC-SHA-256 of the subject under
  `EVIDENCE.SUBJECT_KEY`.
- A release could be published from any `v*` tag after only the unit tests
  (L3). `release.yml` now refuses tags on commits that are not on `main`,
  runs the whole CI workflow on the tagged commit (lint, types,
  integration, coverage, build, pip-audit, gitleaks), and publishes the
  wheel and sdist that CI built and smoke-tested. The `pypi` environment's
  reviewer and the `v*` tag ruleset remain GitHub settings; see
  `docs/versioning.md`.
- The production settings made by `init` published the API docs, sent no
  HSTS header, and had no rate limit (L2). They now turn the docs off
  (`API_DOCS`), send HSTS for a year (`SECURE_HSTS_SECONDS`), and allow each
  caller 300 requests a minute (`RATE_LIMIT`), each changed by an environment
  variable; the base settings list `RateLimitMiddleware`, which does nothing
  until `RATE_LIMIT` is set. In staging and production, `check` now warns
  about each of the three when it is weak.
- `ALLOWED_HOSTS` wildcards accepted malformed `Host` values (L1): with
  `*.example.com`, `evil.com/.example.com` (or with `?`, `#`, `@`) passed,
  because only the end of the value was compared. As in Django, a `Host`
  must now be a name, an IPv4 address, or a bracketed IPv6 address, with an
  optional numeric port; anything else is refused with `400 invalid_host`.
- A short database outage stopped `runworker`, and could leave broker
  messages stuck (M5). The polling worker and the relay ended on the first
  failed query, cancelling the work in hand; with RabbitMQ, a delivery whose
  outcome could not be written was never answered and kept a consumer slot,
  so after `CONCURRENCY` such errors the consumer took no more work. Workers
  now log outages, wait longer after each failure in a row (up to a minute),
  and carry on; every delivery is acknowledged or requeued; and a message
  whose outcome could not be written runs again once its lock expires. A
  periodic task whose queueing failed is no longer skipped for its interval.
- The in-memory rate limiter could slow every request and grow without
  bound (M4): past 100,000 callers it rescanned them all on each request,
  about 11 ms each, and kept those still counting. It now keeps callers in
  the order they were last seen, forgets idle ones as it goes, and keeps at
  most 100,000: about 0.01 ms per request. IPv6 clients are counted per /64
  (`RATE_LIMIT_IPV6_PREFIX`), by the rate limit and the login throttle, so
  changing address within one customer's network no longer escapes them.
- Rate limits and idempotency keys were per client address even for
  logged-in users, so users behind one address shared a limit, and one could
  be replayed another's response (M3). With `AuthenticationMiddleware` they
  are per user; without an actor, idempotency keys are also scoped by a hash
  of the request's `Authorization` and `Cookie` headers.
- A streamed body over `MAX_REQUEST_BODY_SIZE` sent with an
  `Idempotency-Key` got a 500 and was reported as an unhandled error;
  `RequestBodyLimitMiddleware` now answers 413 for middleware that reads
  bodies outside FastAPI.

## [0.5.0] - 2026-10-14

### Added

- Workers on RabbitMQ (ADR 0014): `BROKERS` by alias (`amqp://`,
  `amqps://`, or `memory://`, through the new `rabbitmq` extra) and
  `MESSAGING.BROKER`. Commits publish their messages at once
  (`PUBLISH_ON_COMMIT`), workers relay the rest and run what the broker
  delivers, and `runworker --no-relay` workers only consume. The outbox
  stays the record of every message, so retries, delays, dead letters, and
  `messages` work as before.
- `DomainConfig.committed(uow)`: a hook that runs after each SQL unit of
  work commits; its failures are reported and never fail the commit.
- Queues: `@task(queue=...)` and `@periodic(..., queue=...)`, and
  `runworker --queue` to run only some queues, with or without a broker.
- `domainkit.broker`: the `Broker` port, `MemoryBroker`, and the RabbitMQ
  adapter (durable direct exchange, quorum queues, persistent mandatory
  messages, publisher confirms).
- The `broker` command: `check` connects, `declare` creates the queues.
- `MESSAGING.REPUBLISH_AFTER`, deployment checks for brokers (TLS,
  certificate checks, a user other than `guest`, no `memory://`), and the
  metrics `messaging_relayed_total` and `messaging_deliveries_skipped_total`.

### Changed

- The outbox has a `queue` column. Run `makemigrations` and `migrate` in
  projects that install messaging.

### Fixed

- A message whose worker died during its last attempt was claimed again
  forever; it is now dead-lettered after `max_attempts`.

## [0.4.0] - 2026-10-14

### Added

- Caches (ADR 0012): `CACHES` by alias, with `memory://` (per process,
  least recently used first out), `redis://` and `rediss://` (Redis or
  Valkey, through the new `redis` extra), and `dummy://`. `default` is a
  memory cache unless set.
- `domainkit.cache.caches[alias]`: `get` and `set` with typed reads, `add`,
  `get_many`, `set_many`, `delete_many`, `incr`, `touch`, `clear` (its own
  namespace only), and `get_or_set`. Values are JSON, never pickled; an
  entry that no longer fits its type reads as a miss.
- `get_or_set` and `@cached` share one computation among concurrent misses,
  refresh hot entries early (XFetch, `EARLY_RECOMPUTE`), and compute values
  when the cache fails. A failed cache is skipped for `FAILURE_COOLDOWN`.
- `invalidate_on(Event, keys)` deletes entries when a domain event is
  published after commit.
- `domainkit.http.ratelimit.RedisBackend` (ADR 0013): rate limits shared by
  every replica, run as GCRA in Redis on the server's clock, in the cache
  named by the new `RATE_LIMIT_CACHE`. Requests pass if Redis fails.
- The `cache` command: `check` pings caches, `clear` empties them.
- Deployment checks: a Redis cache on another host needs `rediss://`,
  certificate checks, and a password or client certificate.
- `/ready` checks caches whose `READINESS` is on; metrics
  `cache_lookups_total` and `cache_errors_total`.
- The testing plugin empties memory caches before every test.

### Changed

- The layer rules forbid `redis` in the application layer, as they forbid
  the other database libraries. Use `domainkit.cache` there.

## [0.3.0] - 2026-10-14

### Added

- Several databases of two kinds (ADR 0008): SQL databases in `DATABASES`
  (PostgreSQL, SQLite), and ledgers in the new `LEDGERS` setting
  (`immudb://`, `memory://`).
- `DATABASE_ROUTING` sends each domain's tables and units of work to a
  target. Each target keeps its own migration history in
  `MIGRATIONS_DIR/<target>`, and `makemigrations`, `migrate`, and `check`
  cover every target (`--database` picks one).
- Sharding (ADR 0009): `SHARDS` groups of SQL databases sharing a schema,
  split by shard key over fixed hash buckets, with optional bucket `RANGES`
  and `PINNED` keys. `SqlAlchemyUnitOfWork(shard_key=...)`,
  `domainkit.db.routing.router`, `bucket_for`, and `for_each_shard`.
- PostgreSQL range partitioning (ADR 0010):
  `domainkit.db.partitioning.partition_by_range`, partitions created by
  `migrate` and by the new `partitions` command (`--prune` drops old ones),
  and ignored by `makemigrations`.
- Ledgers (ADR 0011): the `domainkit.ledger` port with `ImmudbLedger`
  (verified operations, TLS, signed states; the new `immudb` extra) and
  `MemoryLedger`. `/ready` checks ledgers.
- `domainkit.contrib.evidence`: canonical, receipted evidence records on a
  ledger, idempotent by id, with tampering detection, durable recording
  through the outbox (`defer_evidence`), and the `evidence` command.
- `runworker --database`, `messages --database`, and `auditlog --database`.
- `DomainConfig.databases(router)`: where an app keeps its tables.

### Changed

- The outbox and the audit trail are created in every database that hosts a
  domain. Units of work on any database can defer tasks, and their events are
  audited there; before, other databases were refused (messaging) or skipped
  (audit).
- `runworker` runs one worker per outbox, every hosting database by default.
- `startdomain` tags generated tables with `info={"domain": ...}`, so
  routing moves them with their domain.
- `SqlAlchemyUnitOfWork.database` defaults to `None`: follow
  `DATABASE_ROUTING` for the domain that defines the subclass. The resolved
  alias is `uow.alias`.

### Fixed

- The audit protection docstring said the table owner was exempt from the
  triggers; they refuse every role, as `docs/contrib.md` says.

## [0.2.0] - 2026-10-14

### Added

- `domainkit init <name> [directory]` creates a project that passes `check`
  and its tests as it is: base, local, test, and production settings, the
  root router, the ASGI application, `manage.py`, `.env`, and a first test.
  The package now installs a `domainkit` command to run it.

## [0.1.1] - 2026-10-14

### Fixed

- `domainkit.testing` needed pytest and httpx2 without declaring them, so
  the quick start's `pytest` step failed on a fresh install. They now come
  with the new `testing` extra: `pip install "domainkit[testing]"`.

## [0.1.0] - 2026-10-14

First version, extracted from a production service template.

### Added

- Django-style project structure: settings modules validated at startup,
  `INSTALLED_DOMAINS` with `DomainConfig`, `ROOT_ROUTER`, `MIDDLEWARE`, and
  `manage.py` commands (`check`, `startdomain`, `makemigrations`, `migrate`,
  `showmigrations`, `runserver`, `shell`).
- Environment variables prefixed from the project name, `.env` files, and
  `<NAME>_FILE` secrets.
- DDD building blocks: `ValueObject`, `Entity`, `AggregateRoot`,
  `DomainEvent`, `Command`, `Query`, `Repository`, `UnitOfWork`, `Page`, and
  business errors mapped to HTTP statuses.
- Layer rules between and within domains, enforced by `check`.
- Async SQLAlchemy persistence: one engine per alias and event loop,
  imperative mapping, optimistic locking with `version` columns, UTC
  timestamps, hidden bound values, libpq TLS options, server-side timeouts.
- Migrations configured from settings: per-domain tables, `--check`,
  `--merge` for parallel branches, reversible-revision testing, and refused
  downgrades in staging and production without `--allow-data-loss`.
- HTTP: one error format, request ids, access logs without query strings,
  security headers, CORS, allowed hosts, body size limits, trusted proxy
  headers, rate limiting (GCRA), health and readiness probes, and keyset
  pagination with opaque cursors.
- Observability: JSON logs with trace ids, Prometheus metrics labelled by
  route template, OpenTelemetry spans, and `ERROR_REPORTERS`.
- `domainkit.contrib.messaging`: transactional outbox, durable event
  handlers, background and periodic tasks, and a worker with retries,
  dead letters, and metrics.
- `domainkit.contrib.audit`: audit trail of committed events and explicit
  records, with database triggers that make it append-only on PostgreSQL.
- `domainkit.contrib.idempotency`: `Idempotency-Key` replay for unsafe
  requests.
- `domainkit.deprecation.DomainkitDeprecationWarning` and the deprecation
  policy.
- `domainkit.testing`: a pytest plugin with migrated `database` and `client`
  fixtures, used by the tests `startdomain` generates.

[Unreleased]: https://github.com/karimmattar/domainkit/compare/v0.9.0...HEAD
[0.9.0]: https://github.com/karimmattar/domainkit/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/karimmattar/domainkit/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/karimmattar/domainkit/compare/v0.6.2...v0.7.0
[0.6.2]: https://github.com/karimmattar/domainkit/compare/v0.6.1...v0.6.2
[0.6.1]: https://github.com/karimmattar/domainkit/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/karimmattar/domainkit/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/karimmattar/domainkit/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/karimmattar/domainkit/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/karimmattar/domainkit/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/karimmattar/domainkit/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/karimmattar/domainkit/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/karimmattar/domainkit/releases/tag/v0.1.0
