# domainkit reference

Settings, middleware, and commands. For the optional apps (messaging and
its broker, audit, idempotency) see [contrib.md](contrib.md); for databases, shards, and ledgers
see [databases.md](databases.md); for caches see [cache.md](cache.md); for
releases and deprecations see [versioning.md](versioning.md).

`domainkit_tests/test_docs.py` fails when a setting or a command is missing
from this page.

## Settings

A settings module is a Python module of UPPERCASE names, selected with
`<PREFIX>SETTINGS_MODULE`. The prefix comes from `[project].name` in
`pyproject.toml` (`billingAPI` gives `BILLING_API_`), or from
`[tool.domainkit] env-prefix`. `domainkit.conf.Settings` validates every name
at startup. A name the framework does not declare must belong to an installed
domain's `settings_class`, or to a project schema named by `SETTINGS_CLASS`.

Read values with `domainkit.conf.settings`. Read environment variables in a
settings module with `domainkit.conf.Env`: it adds the prefix, reads `.env`
(or `<PREFIX>ENV_FILE`), and accepts `<NAME>_FILE` for secrets mounted as
files.

### Core

| Setting | Default | Meaning |
| --- | --- | --- |
| `SETTINGS_MODULE` | set by the loader | The module these settings came from. |
| `BASE_DIR` | the project root | The directory holding `pyproject.toml`. |
| `ENVIRONMENT` | `"local"` | `local`, `test`, `staging`, or `production`. Staging and production get the [deployment checks](#deployment-checks). |
| `DEBUG` | `False` | Development conveniences; refused in staging and production. |
| `PROJECT_NAME` | `"API"` | The OpenAPI title. |
| `VERSION` | `"0.1.0"` | The OpenAPI version. |
| `INSTALLED_DOMAINS` | `()` | Domain packages (Django's `INSTALLED_APPS`), started in order and stopped in reverse. |
| `MIDDLEWARE` | see [Middleware](#middleware) | ASGI middleware by dotted path, outermost first. |
| `ROOT_ROUTER` | `None` | Dotted path to the `APIRouter` that mounts every route (Django's `ROOT_URLCONF`). |
| `ASGI_APPLICATION` | `None` | `module:attribute` that `runserver` serves. |
| `LOGGING` | minimal | A `logging.config.dictConfig` mapping, applied by `setup()`. |
| `ERROR_REPORTERS` | `()` | Callables `(exc, context) -> None` by dotted path, called for every unexpected error (500s, dead-lettered messages). A reporter that raises is logged and skipped. |

### Databases and migrations

| Setting | Default | Meaning |
| --- | --- | --- |
| `DATABASES` | `{}` | SQL databases by alias (below). If any are set, `default` is required. A ledger URL here is refused with a pointer to `LEDGERS`. |
| `LEDGERS` | `{}` | Ledgers by alias (below): write once, read many, verified. They hold evidence and are never migrated. |
| `SHARDS` | `{}` | Shard groups by name (below): SQL aliases that share a schema, split by a shard key. |
| `DATABASE_ROUTING` | `{}` | Domain label to target: a SQL alias or a shard group. Domains not listed use `default`. Their tables and units of work both go there. |
| `MIGRATIONS_DIR` | `<BASE_DIR>/migrations` | Revisions of `default`; every other target keeps its own history in `MIGRATIONS_DIR/<target>`. |

See [databases.md](databases.md) for routing, sharding, partitioning, and ledgers.

Each `DATABASES` entry:

| Key | Default | Meaning |
| --- | --- | --- |
| `URL` | required | SQLAlchemy async URL (`postgresql+asyncpg://…`, `sqlite+aiosqlite:///…`). Kept secret in logs and reprs. |
| `MIGRATION_URL` | `None` | A schema-owner URL used only by `migrate` and `makemigrations`; the app keeps a row-level role. |
| `ECHO` | `False` | Log every statement; refused in staging and production. |
| `LOG_PARAMETERS` | `False` | Show bound values in logs and errors; refused in staging and production. Without it, the `DETAIL` PostgreSQL adds to constraint errors (`Failing row contains (...)`, which repeats the row's values) is dropped too. |
| `SSLMODE` | `None` | libpq names: `disable`, `require`, `verify-ca`, `verify-full`. Staging and production need `verify-full` for network databases. |
| `SSLROOTCERT`, `SSLCERT`, `SSLKEY` | `None` | CA bundle, and client certificate and key for mutual TLS. |
| `STATEMENT_TIMEOUT` | `30.0` | Seconds; PostgreSQL cancels longer statements. Migrations are exempt. |
| `LOCK_TIMEOUT` | `10.0` | Seconds a statement waits for a lock. Migrations are exempt. |
| `IDLE_IN_TRANSACTION_TIMEOUT` | `60.0` | Seconds before PostgreSQL ends a session idling inside a transaction. |
| `POOL_SIZE`, `MAX_OVERFLOW`, `POOL_RECYCLE` | SQLAlchemy's | Connection pool sizing. |
| `POOL_TIMEOUT` | `10.0` for network databases, else SQLAlchemy's | Seconds a request waits for a connection; past it the request gets 503 `database_busy`. |
| `POOL_PRE_PING` | `True` | Test connections before use, so database restarts are survived. |
| `CONNECT_ARGS` | `{}` | Extra driver arguments, added to those derived from the options above. asyncpg's `server_settings` and libpq's `options` are merged with the timeouts. Setting what the options above already set (TLS, a timeout) is refused when the application is built; change that option instead. |

Each `LEDGERS` entry:

| Key | Default | Meaning |
| --- | --- | --- |
| `URL` | required | `immudb://user:password@host:3322/database`, or `memory://` for tests and development. Kept secret. |
| `TLS_CA` | `None` | CA certificate that signed the immudb server's; enables TLS. Required in staging and production. |
| `SERVER_SIGNING_KEY` | `None` | Public key of the server's `--signingKey`; every state is then checked. Required in staging and production. |
| `TIMEOUT` | `10.0` | Seconds each ledger call may take. |

Each `SHARDS` group:

| Key | Default | Meaning |
| --- | --- | --- |
| `DATABASES` | required | The group's SQL aliases (not `default`); `makemigrations` compares against the first. |
| `BUCKETS` | `4096` | Hash buckets keys spread over. Never change it once the group holds data. |
| `RANGES` | equal ranges | Inclusive bucket ranges per alias, such as `{"accounts_0": [[0, 2047]], "accounts_1": [[2048, 4095]]}`; they must cover every bucket once. |
| `PINNED` | `{}` | Shard keys (as text) that always go to one alias, such as a large tenant. |

### Caches

See [cache.md](cache.md) for how to use them.

| Setting | Default | Meaning |
| --- | --- | --- |
| `CACHES` | `{"default": {}}` | Caches by alias (below). `default` is required; unless set, it is a `memory://` cache. |

Each `CACHES` entry:

| Key | Default | Meaning |
| --- | --- | --- |
| `URL` | `memory://` | `memory://` (this process), `redis://` or `rediss://` (Redis or Valkey, through redis-py; the `redis` extra), or `dummy://` (keeps nothing). redis-py URL options (`?socket_keepalive=true`) apply. Kept secret. |
| `TTL` | `300.0` | Seconds an entry lives unless a call passes `ttl`; `None` keeps entries until evicted. |
| `KEY_PREFIX` | the alias | Namespace of every key (`[A-Za-z0-9_.-]`, up to 64). Required for Redis: unique among the services sharing a server. |
| `VERSION` | `1` | Part of every key. Raise it to drop every entry at once, as after changing a cached type. |
| `MAX_ENTRIES` | `10000` | `memory://` only: least recently used entries are evicted beyond it. |
| `SOCKET_TIMEOUT` | `1.0` | Seconds one Redis command may take. |
| `CONNECT_TIMEOUT` | `1.0` | Seconds to connect, or to wait for a free pooled connection. |
| `RETRIES` | `1` | Quick retries of a command after a connection error or timeout. |
| `MAX_CONNECTIONS` | `50` | Redis connections per process and event loop. |
| `FAILURE_COOLDOWN` | `5.0` | Seconds a failed cache is skipped, so requests do not each wait for timeouts. `0` retries at once. |
| `EARLY_RECOMPUTE` | `1.0` | How early `get_or_set` refreshes hot entries before they expire (XFetch's beta); `0` turns it off. |
| `TLS_CA` | system CAs | CA bundle that signed the server's certificate (`rediss://` only). |
| `TLS_CERT`, `TLS_KEY` | `None` | Client certificate and key for mutual TLS (`rediss://` only). |
| `READINESS` | `False` | Fail `/ready` while this cache is unreachable. Leave it off for caches the service can run without, or an outage takes every replica out of rotation. |

### Brokers

Message brokers carry the worker's work when `MESSAGING.BROKER` names one;
see [contrib.md](contrib.md#workers-on-rabbitmq).

| Setting | Default | Meaning |
| --- | --- | --- |
| `BROKERS` | `{}` | Message brokers by alias (below). |

Each `BROKERS` entry:

| Key | Default | Meaning |
| --- | --- | --- |
| `URL` | required | `amqps://user:password@host:5671/vhost` or `amqp://...` (RabbitMQ, through aio-pika; the `rabbitmq` extra), or `memory://` (one process, for tests and development). aiormq URL options (`?heartbeat=30`) apply. Kept secret. |
| `PREFIX` | required for RabbitMQ | Starts every exchange and queue name (`[A-Za-z0-9_.-]`, up to 64): `<PREFIX>.work`, `<PREFIX>.<queue>`. Unique among the services sharing a virtual host. |
| `TLS_CA` | system CAs | CA bundle that signed the server's certificate (`amqps://` only). |
| `TLS_CERT`, `TLS_KEY` | `None` | Client certificate and key for mutual TLS (`amqps://` only). |
| `CONNECT_TIMEOUT` | `10.0` | Seconds to connect. |
| `PUBLISH_TIMEOUT` | `10.0` | Seconds to wait for the broker to confirm a published message. |

### HTTP

| Setting | Default | Meaning |
| --- | --- | --- |
| `ALLOWED_HOSTS` | `localhost`, `127.0.0.1`, `[::1]` | Accepted `Host` values; `*.example.com` matches subdomains. A `Host` that is not a name, an IPv4 address, or a bracketed IPv6 address (with an optional port) is refused before matching. A wildcard `*` is refused in staging and production. |
| `API_DOCS` | `True` | Serve `/docs`, `/redoc`, and `/openapi.json`. `check` warns when it is on in staging and production. |
| `MAX_REQUEST_BODY_SIZE` | `1048576` | Bytes; larger bodies get 413, whether declared or streamed. A larger WebSocket message, text counted in UTF-8 bytes, closes the connection with 1009. |
| `READINESS_TIMEOUT` | `5.0` | Seconds each readiness check may take. |
| `READINESS_CACHE_SECONDS` | `1.0` | How long a `/ready` result is reused, from when its checks ended; probes that come while checks run wait for them, so probe floods cannot load the database. |
| `READINESS_DETAILS` | `False` | Name failing components in `/ready` responses (always logged). |
| `ACCESS_LOG_QUIET_PATHS` | `/health`, `/ready` | Successful requests to these paths are logged at DEBUG. |
| `TRUSTED_PROXIES` | `()` | Networks (`10.0.0.0/8`) whose `X-Forwarded-For` and `X-Forwarded-Proto` are believed. The client is the right-most address that is not a trusted proxy, across every line of the header; hops may carry a port. The scheme is the `X-Forwarded-Proto` value at the client's place from the right, or the last one. Empty: the socket peer is the client. |
| `RATE_LIMIT` | `None` | Requests per caller, such as `"300/minute"` (`second`, `minute`, `hour`, `day`). `None` disables it. |
| `RATE_LIMIT_EXEMPT_PATHS` | `/health`, `/ready`, `/metrics` | Exact paths never counted. |
| `RATE_LIMIT_BACKEND` | `domainkit.http.ratelimit.MemoryBackend` | Where counts live. The default is per process, and keeps the 100,000 most recently seen callers; one seen longer ago starts afresh. Behind several replicas, use `domainkit.http.ratelimit.RedisBackend`: one count per caller in Redis, on the server's clock. If Redis fails, or refuses the count (out of memory, a read-only replica after a failover), requests are let through, and Redis is skipped for the cache's `FAILURE_COOLDOWN`. |
| `RATE_LIMIT_CACHE` | `"default"` | The Redis cache `RedisBackend` counts in; keys are `<KEY_PREFIX>:ratelimit:<caller>`. |
| `RATE_LIMIT_IPV6_PREFIX` | `64` | IPv6 clients are counted per network of this prefix length, since one customer usually holds a whole /64; login throttling counts the same way. IPv4 clients are counted per address. |
| `METRICS_TOKEN` | `None` | Bearer token for `/metrics`. Without it, `/metrics` answers 403 in staging and production. |
| `SECURE_HSTS_SECONDS` | `0` | `Strict-Transport-Security` max-age; 0 omits the header. |
| `SECURE_HSTS_INCLUDE_SUBDOMAINS` | `False` | Adds `includeSubDomains`. |
| `SECURE_CONTENT_TYPE_NOSNIFF` | `True` | `X-Content-Type-Options: nosniff`. |
| `SECURE_REFERRER_POLICY` | `"no-referrer"` | `Referrer-Policy`; `None` omits it. |
| `X_FRAME_OPTIONS` | `"DENY"` | Or `"SAMEORIGIN"`. |
| `CORS_ALLOWED_ORIGINS` | `()` | Origins allowed to call the API from a browser. |
| `CORS_ALLOWED_METHODS` | `GET POST PUT PATCH DELETE` | |
| `CORS_ALLOWED_HEADERS` | `Authorization Content-Type X-Request-ID` | |
| `CORS_ALLOW_CREDENTIALS` | `False` | Cannot be combined with origin `*`. |

### Server limits

The application limits bodies, WebSocket messages, and request rates. Slow
and oversized requests are the server's to stop, before the application
sees them. Run uvicorn behind a reverse proxy or load balancer that limits
header size and the time to send headers and body (nginx:
`large_client_header_buffers`, `client_header_timeout`,
`client_body_timeout`), and set uvicorn's own limits:

```bash
uvicorn config.asgi:application --limit-concurrency 200 --timeout-keep-alive 5 \
  --h11-max-incomplete-event-size 16384 --ws-max-size 1048576
```

`--ws-max-size` stops a WebSocket message before it is read whole;
`MAX_REQUEST_BODY_SIZE` then closes the connection on one that got through.

### Deployment checks

With `ENVIRONMENT` set to `staging` or `production`, startup fails on:

- `DEBUG = True`
- `*` in `ALLOWED_HOSTS`
- a `TRUSTED_PROXIES` network that holds every address (`0.0.0.0/0`, `::/0`)
- `ECHO` or `LOG_PARAMETERS` on any database
- a network database without `SSLMODE = "verify-full"`
- a `memory://` ledger, or an immudb ledger without `TLS_CA` or
  `SERVER_SIGNING_KEY`
- a `memory://` broker, a broker on another host without `amqps://`, a
  broker URL that turns certificate checks off (`no_verify_ssl`), and a
  broker without a user of its own (none, or `guest`)
- a Redis cache on another host without `rediss://`, or without a password
  or a client certificate, and any cache URL that weakens certificate checks
  (`ssl_cert_reqs=none` or `optional`, `ssl_check_hostname=false`)

Installed contrib apps add their own:

- auth: a password hasher made for tests in `AUTH.PASSWORD_HASHERS`
  ([auth.md](auth.md))
- evidence: no `EVIDENCE.SUBJECT_KEY`
  ([contrib.md](contrib.md#evidence-records-that-provably-never-change))

`migrate --downgrade` also needs `--allow-data-loss` there.

`check` also warns about choices that are valid but weak there. It still
passes, so an API that is public on purpose can keep them:

- `API_DOCS = True`: anyone can read `/docs`, `/redoc`, and `/openapi.json`
- `SECURE_HSTS_SECONDS = 0`: browsers are not told to keep to HTTPS
- no `RATE_LIMIT`, or `RateLimitMiddleware` missing from `MIDDLEWARE`
- a rate limit with no `TRUSTED_PROXIES`: behind a load balancer, every
  caller would share its address and its count

The production settings that `init` create avoid the first three, and ask
for `TRUSTED_PROXIES`: leave it empty only when callers connect directly.

## Middleware

The order matters; this is the order the project template uses, outermost
first:

| Middleware | Why here |
| --- | --- |
| `domainkit.http.middleware.ProxyHeadersMiddleware` | First, so everything after it sees the real client address and scheme. |
| `domainkit.http.metrics.MetricsMiddleware` | Counts every response, including ones refused below. |
| `domainkit.http.middleware.SecurityHeadersMiddleware` | Adds the `SECURE_*` headers to every response. |
| `domainkit.http.middleware.CORSMiddleware` | Answers preflights before any work. |
| `domainkit.http.middleware.RequestContextMiddleware` | Request id (`X-Request-ID`), access log, the context that logs and audit rows read. WebSocket connections get one too: method `WS`, the request id sent with the accept, one access log line when the connection ends (101 if accepted, 403 if closed first). |
| `domainkit.http.middleware.AllowedHostsMiddleware` | Refuses unknown `Host` headers with 400. |
| `domainkit.contrib.auth.middleware.AuthenticationMiddleware` | Finds the user from the request's token and makes it the actor, so what follows counts users (auth; see [auth.md](auth.md)). |
| `domainkit.http.ratelimit.RateLimitMiddleware` | Counts per actor, or per client address (IPv6: per /64). A WebSocket handshake counts once; over the limit it gets the 429, or a 1013 close where the server cannot send one. |
| `domainkit.http.middleware.RequestBodyLimitMiddleware` | Refuses bodies over `MAX_REQUEST_BODY_SIZE` with 413, and closes WebSockets with 1009 on a larger message. |
| `domainkit.contrib.idempotency.middleware.IdempotencyMiddleware` | Last: reads whole bodies, which the limit above has already bounded. |

`DEFAULT_MIDDLEWARE` (proxy headers, security headers, request context,
allowed hosts, body limit) applies when a settings module sets no
`MIDDLEWARE`.

## Errors

Every error response is JSON: `{"detail", "code", "request_id"}`, plus
`errors` (location, message, type) for validation errors. Submitted values
are never echoed back.

| Raised | Status | `code` |
| --- | --- | --- |
| `NotFound` | 404 | from the class, such as `invoice_not_found` |
| `Conflict`, `ConcurrentUpdate`, `DuplicateValue` | 409 | from the class; a unique constraint may name its own (see [databases.md](databases.md#unique-values)) |
| `BusinessRuleViolation` | 422 | from the class |
| `PermissionDenied` | 403 | from the class |
| other `DomainError` | 400 | from the class |
| `domainkit.http.APIError(status, detail, code=…)` | as given | as given |
| request validation | 422 | `validation_error` |
| no pooled connection within `POOL_TIMEOUT`, or a statement cancelled by `STATEMENT_TIMEOUT` or `LOCK_TIMEOUT`, or lost to a deadlock or a serialization failure | 503 | `database_busy`, with `Retry-After: 1`; logged as a warning, not reported |
| a value the database refuses (SQLSTATE class 22: a NUL character, text too long, a number out of range) | 422 | `invalid_value`; logged as a warning, not reported |
| unexpected exception | 500 | `internal_error`; logged and sent to `ERROR_REPORTERS` |

WebSockets the database refuses are closed with 1013 (busy) or 1007 (a
value it refuses).

## Observability

**Logs** are JSON lines with `request_id`, and with `trace_id` and `span_id`
when a span is active. Query strings and bound SQL values are never logged.

**Metrics** (`domainkit[metrics]`, mounted at `/metrics`):

| Metric | Labels |
| --- | --- |
| `http_requests_total` | `method` (`OTHER` for any but the nine standard ones), `route` (the template, never the raw path), `status` |
| `http_request_duration_seconds` | `method`, `route` |
| `http_requests_in_progress` | `method` |
| `db_pool_connections` | `database`, `state` |
| `messaging_messages_total` | `kind`, `outcome` |
| `messaging_message_duration_seconds` | `kind` |
| `messaging_outbox_messages` | `status` |
| `messaging_relayed_total` | `outcome` (`published`, `failed`) |
| `messaging_deliveries_skipped_total` | |
| `cache_lookups_total` | `cache`, `result` (`hit`, `miss`, `invalid`, `early`) |
| `cache_errors_total` | `cache` |

**Traces** (`domainkit[tracing]`): unit-of-work commits and worker messages
get spans. domainkit only uses the OpenTelemetry API; configure the SDK and
exporter in the deployment, or run under `opentelemetry-instrument`.

## Testing

`domainkit.testing` is a pytest plugin, like pytest-django. Install it with
the `testing` extra, which brings pytest and httpx2 (Starlette's `TestClient`
needs it), as a development dependency:

```toml
[dependency-groups]
dev = ["domainkit[testing]"]
```

Then enable it in the project's `pyproject.toml`:

```toml
[tool.pytest.ini_options]
addopts = "-p domainkit.testing"
domainkit_settings = "config.settings.test"
```

It selects the test settings, never reads `.env`, and provides these fixtures:

| Fixture | Scope | Gives |
| --- | --- | --- |
| `database` | session | The test database's URL, after migrating it up, down to base, and up again. That proves every revision is reversible and empties a reused database. In-memory SQLite becomes a temporary file. |
| `db` | test | That database put back as `migrate` left it: rows earlier tests committed are gone, and the ones `migrate` wrote (content types, permissions) are there. Every database in `DATABASES` is put back. Request it in tests that reach the database without a client. |
| `client` | test | A `TestClient` for the project's application, with its lifespan, on the `db` database. It sends no credentials. |
| `client_for` | test | `client_for("billing.view_invoice", ...)`: a client of the same application that authenticates as a new user granted those permissions. Keyword arguments set the user's fields (`is_staff=True`, `username="carol"`); the username is unique otherwise. Needs `domainkit.contrib.auth`. |
| `admin_client` | test | A client that authenticates as a new superuser. The tests `startdomain` generates use it. |

So a test sees only the rows it writes, in any order, however it wrote
them: through a client, a unit of work, a command, or the worker. The
database is put back before each test that asks for it, by deleting every
row and inserting the rows read once after migrating; on PostgreSQL, tables
with triggers of their own (an append-only audit trail) have them off while
this runs. Sequences keep counting, so do not assert on generated ids.

Memory caches are emptied before every test, so no test sees what another
cached. Redis caches are left alone: give the test settings their own
`KEY_PREFIX`.

Point the test settings at PostgreSQL (for example from a
`TEST_DATABASE_URL` variable) to run the same suite there. Never point them
at a database you want to keep. The database fixtures refuse settings whose
`ENVIRONMENT` is not `"test"`, so a missing `domainkit_settings` cannot
empty the development database. They read and restore rows as the schema
owner (`MIGRATION_URL` when set), which append-only tables need.

## Commands

Run as `python manage.py <command>`; `init` runs before a project exists, as
`domainkit init`. Domains add their own in `management/commands/<name>.py`
with a `Command(BaseCommand)` class; `handle` may be `async`.

| Command | Options | Purpose |
| --- | --- | --- |
| `init` | `name`, `[directory]` | Create a project: base, local, test, and production settings, routes, `manage.py`, `.env`, and a first test. The name sets the environment prefix; the directory (default `./<name>`) must be empty or missing. |
| `check` | | Settings, domains, routes and their access rules, middleware, layer rules, and a single migration head. In staging and production it also lists [weak choices](#deployment-checks) as warnings. |
| `broker` | `check\|declare`, `[ALIAS ...]` | Connect to brokers (all by default), or declare the exchange and every queue the worker's tasks use on `MESSAGING.BROKER`. |
| `cache` | `check\|clear`, `[ALIAS ...]`, `--all` | Ping caches (all by default), or delete every key of the named caches, all versions, in their namespace only. Clearing needs aliases or `--all`. |
| `startdomain` | `name`, `--aggregate`, `--plural`, `--parent`, `--tests-dir` | Generate a bounded context and its tests. |
| `makemigrations` | `-m/--message`, `--check`, `--merge`, `--database` | Write a revision per target with changes; `--check` fails if one is missing; `--merge` joins two heads. Without `--database`, every target. |
| `migrate` | `[revision]`, `--downgrade`, `--allow-data-loss`, `--database` | Apply revisions (or revert down to one) on every database of every target, then create partitions. A revision id needs `--database` when there are several targets. |
| `showmigrations` | `--database` | Applied and pending revisions; a shard group lists each database. |
| `partitions` | `--database`, `--prune`, `--dry-run` | Create the current and upcoming partitions of partitioned tables; `--prune` drops those past `retain`. |
| `runserver` | `[addr:port]`, `--noreload` | Development server. Production runs uvicorn directly. |
| `shell` | | Python console with settings, the registry, and connections loaded. |
| `runworker` | `--once`, `--metrics-port`, `--metrics-address`, `--database`, `--queue`, `--no-relay` | Run outbox messages (messaging): one worker per outbox, every hosting database by default; `--queue` (repeatable) limits them to some queues. With `MESSAGING.BROKER`, relay to the broker and run what it delivers; `--no-relay` only consumes. `--once` always works from the outbox. |
| `messages` | `--dead`, `--limit`, `--retry ID\|all`, `--database` | Inspect an outbox, and requeue dead letters (messaging). |
| `auditlog` | `--actor`, `--action`, `--limit`, `--database` | Read the audit trail (audit). |
| `auditseal` | | Seal new audit rows into each database's hash chain and anchor its head on `AUDIT.LEDGER` (audit). |
| `auditverify` | `--database` | Check the audit trail against its seals and anchors; fails if anything changed (audit). |
| `auditforget` | `subject ...`, `--anonymous-days` | Make people's pseudonyms in the audit trail unlinkable, in every audited database (audit). |
| `evidence` | `--subject`, `--verify KEY`, `--limit` | List a subject's evidence, or verify one record (evidence). |
| `createsuperuser` | `--<USERNAME_FIELD>`, `--<field>` per `REQUIRED_FIELDS`, `--noinput` | Create a superuser, asking for what the options leave out; `--noinput` reads `<PREFIX>SUPERUSER_<FIELD>` (auth; see [auth.md](auth.md)). |
| `changepassword` | `[username]` | Set a user's password, asked twice and validated; the user's tokens end (auth). |
| `createtoken` | `username`, `--name`, `--days`, `--no-expiry` | Print a new token for a user's scripts, shown once (auth). |
| `cleartokens` | `--user` | Delete expired tokens, or every token of one user (auth). |

## Extension points

| To add | Implement | Register with |
| --- | --- | --- |
| A bounded context | a package with `apps.py` (`DomainConfig`) | `INSTALLED_DOMAINS` |
| Domain settings | a Pydantic model | `DomainConfig.settings_class` |
| Startup wiring | `DomainConfig.ready()` | (called by `setup()`) |
| Process resources (clients, pools) | `async DomainConfig.startup()` / `shutdown()` | (called by the app lifespan; after the domains stop, the framework closes its caches, databases, ledgers, and brokers, in that order, each within 5 seconds, and a failure is logged without keeping the rest open; management commands close them too) |
| A readiness check | `async DomainConfig.check()` | (called by `/ready`) |
| A check of the built application (routes, middleware) | `DomainConfig.check_application(app)`, returning problems | (called when the app is built and by `check`; staging and production refuse to start with any) |
| Work staged with each commit | `async DomainConfig.stage(uow, events, tasks)` | (called by the unit of work) |
| Work after each commit | `async DomainConfig.committed(uow)` | (called by the unit of work; failures are reported, never raised) |
| Rows after each `migrate` (`post_migrate`) | `async DomainConfig.migrated(connection, database)` | (called by `migrate` for each database it upgraded, in one transaction as the schema owner) |
| Tables a domain stops using | `DomainConfig.holds_table(table)` | (asked by migrations and content types; a swapped user table answers no) |
| A user model | a subclass of `AbstractUser` or `AbstractBaseUser`, and its table | `AUTH.USER_MODEL`, `AUTH.USER_TABLE` |
| An authentication or permission backend | `auth.backends.BaseBackend` | `AUTH.BACKENDS` |
| A way requests prove who sent them | `auth.authentication.BaseAuthentication` | `AUTH.AUTHENTICATION_CLASSES` |
| A rule for who may call a route | `auth.access.BasePermission` | `Depends(Require(rule))` on the route, or `Public` for anyone |
| A password hasher | `auth.hashers.PasswordHasher` | `AUTH.PASSWORD_HASHERS` |
| A password rule | a class with `validate(password, user)` and `help_text()` | `AUTH.PASSWORD_VALIDATORS` |
| Middleware | an ASGI class taking `app` | `MIDDLEWARE` |
| An error tracker | `(exc, context) -> None` | `ERROR_REPORTERS` |
| A rate-limit store other than memory or Redis | `ratelimit.Backend` | `RATE_LIMIT_BACKEND` |
| A command | `BaseCommand` | `management/commands/<name>.py` |
