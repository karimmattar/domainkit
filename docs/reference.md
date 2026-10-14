# domainkit reference

Settings, middleware, and commands. For the optional apps (messaging, audit,
idempotency) see [contrib.md](contrib.md); for releases and deprecations see
[versioning.md](versioning.md).

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
| `DATABASES` | `{}` | Aliases to connection options (below). If any are set, `default` is required. |
| `MIGRATIONS_DIR` | `<BASE_DIR>/migrations` | Where `makemigrations` writes revisions. |

Each `DATABASES` entry:

| Key | Default | Meaning |
| --- | --- | --- |
| `URL` | required | SQLAlchemy async URL (`postgresql+asyncpg://…`, `sqlite+aiosqlite:///…`). Kept secret in logs and reprs. |
| `MIGRATION_URL` | `None` | A schema-owner URL used only by `migrate` and `makemigrations`; the app keeps a row-level role. |
| `ECHO` | `False` | Log every statement; refused in staging and production. |
| `LOG_PARAMETERS` | `False` | Show bound values in logs and errors; refused in staging and production. |
| `SSLMODE` | `None` | libpq names: `disable`, `require`, `verify-ca`, `verify-full`. Staging and production need `verify-full` for network databases. |
| `SSLROOTCERT`, `SSLCERT`, `SSLKEY` | `None` | CA bundle, and client certificate and key for mutual TLS. |
| `STATEMENT_TIMEOUT` | `30.0` | Seconds; PostgreSQL cancels longer statements. Migrations are exempt. |
| `LOCK_TIMEOUT` | `10.0` | Seconds a statement waits for a lock. Migrations are exempt. |
| `IDLE_IN_TRANSACTION_TIMEOUT` | `60.0` | Seconds before PostgreSQL ends a session idling inside a transaction. |
| `POOL_SIZE`, `MAX_OVERFLOW`, `POOL_TIMEOUT`, `POOL_RECYCLE` | SQLAlchemy's | Connection pool sizing. |
| `POOL_PRE_PING` | `True` | Test connections before use, so database restarts are survived. |
| `CONNECT_ARGS` | `{}` | Driver arguments; they override the options above. |

### HTTP

| Setting | Default | Meaning |
| --- | --- | --- |
| `ALLOWED_HOSTS` | `localhost`, `127.0.0.1`, `[::1]` | Accepted `Host` values; `*.example.com` matches subdomains. A wildcard `*` is refused in staging and production. |
| `API_DOCS` | `True` | Serve `/docs`, `/redoc`, and `/openapi.json`. |
| `MAX_REQUEST_BODY_SIZE` | `1048576` | Bytes; larger bodies get 413, whether declared or streamed. |
| `READINESS_TIMEOUT` | `5.0` | Seconds each readiness check may take. |
| `READINESS_CACHE_SECONDS` | `1.0` | How long a `/ready` result is reused, so probe floods cannot load the database. |
| `READINESS_DETAILS` | `False` | Name failing components in `/ready` responses (always logged). |
| `ACCESS_LOG_QUIET_PATHS` | `/health`, `/ready` | Successful requests to these paths are logged at DEBUG. |
| `TRUSTED_PROXIES` | `()` | Networks (`10.0.0.0/8`) whose `X-Forwarded-For` and `X-Forwarded-Proto` are believed. Empty: the socket peer is the client. |
| `RATE_LIMIT` | `None` | Requests per caller, such as `"300/minute"` (`second`, `minute`, `hour`, `day`). `None` disables it. |
| `RATE_LIMIT_EXEMPT_PATHS` | `/health`, `/ready`, `/metrics` | Exact paths never counted. |
| `RATE_LIMIT_BACKEND` | `domainkit.http.ratelimit.MemoryBackend` | Where counts live. The default is per process: behind several replicas, limit at the gateway or plug in a shared store implementing `Backend`. |
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

### Deployment checks

With `ENVIRONMENT` set to `staging` or `production`, startup fails on:

- `DEBUG = True`
- `*` in `ALLOWED_HOSTS`
- `ECHO` or `LOG_PARAMETERS` on any database
- a network database without `SSLMODE = "verify-full"`

`migrate --downgrade` also needs `--allow-data-loss` there.

## Middleware

The order matters; this is the order the project template uses, outermost
first:

| Middleware | Why here |
| --- | --- |
| `domainkit.http.middleware.ProxyHeadersMiddleware` | First, so everything after it sees the real client address and scheme. |
| `domainkit.http.metrics.MetricsMiddleware` | Counts every response, including ones refused below. |
| `domainkit.http.middleware.SecurityHeadersMiddleware` | Adds the `SECURE_*` headers to every response. |
| `domainkit.http.middleware.CORSMiddleware` | Answers preflights before any work. |
| `domainkit.http.middleware.RequestContextMiddleware` | Request id (`X-Request-ID`), access log, the context that logs and audit rows read. |
| `domainkit.http.middleware.AllowedHostsMiddleware` | Refuses unknown `Host` headers with 400. |
| `domainkit.http.ratelimit.RateLimitMiddleware` | Counts per actor, or per client address. |
| `domainkit.http.middleware.RequestBodyLimitMiddleware` | Refuses bodies over `MAX_REQUEST_BODY_SIZE` with 413. |
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
| `Conflict`, `ConcurrentUpdate` | 409 | from the class |
| `BusinessRuleViolation` | 422 | from the class |
| `PermissionDenied` | 403 | from the class |
| other `DomainError` | 400 | from the class |
| `domainkit.http.APIError(status, detail, code=…)` | as given | as given |
| request validation | 422 | `validation_error` |
| unexpected exception | 500 | `internal_error`; logged and sent to `ERROR_REPORTERS` |

## Observability

**Logs** are JSON lines with `request_id`, and with `trace_id` and `span_id`
when a span is active. Query strings and bound SQL values are never logged.

**Metrics** (`domainkit[metrics]`, mounted at `/metrics`):

| Metric | Labels |
| --- | --- |
| `http_requests_total` | `method`, `route` (the template, never the raw path), `status` |
| `http_request_duration_seconds` | `method`, `route` |
| `http_requests_in_progress` | `method` |
| `db_pool_connections` | `database`, `state` |
| `messaging_messages_total` | `kind`, `outcome` |
| `messaging_message_duration_seconds` | `kind` |
| `messaging_outbox_messages` | `status` |

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

It selects the test settings, never reads `.env`, and provides two fixtures:

| Fixture | Scope | Gives |
| --- | --- | --- |
| `database` | session | The test database's URL, after migrating it up, down to base, and up again. That proves every revision is reversible and empties a reused database. In-memory SQLite becomes a temporary file. |
| `client` | test | A `TestClient` for the project's application, with its lifespan, on that database. The tests `startdomain` generates use it. |

Point the test settings at PostgreSQL (for example from a
`TEST_DATABASE_URL` variable) to run the same suite there. Never point them
at a database you want to keep.

## Commands

Run as `python manage.py <command>`; `init` runs before a project exists, as
`domainkit init`. Domains add their own in `management/commands/<name>.py`
with a `Command(BaseCommand)` class; `handle` may be `async`.

| Command | Options | Purpose |
| --- | --- | --- |
| `init` | `name`, `[directory]` | Create a project: base, local, test, and production settings, routes, `manage.py`, `.env`, and a first test. The name sets the environment prefix; the directory (default `./<name>`) must be empty or missing. |
| `check` | | Settings, domains, routes, middleware, layer rules, and a single migration head. |
| `startdomain` | `name`, `--aggregate`, `--plural`, `--parent`, `--tests-dir` | Generate a bounded context and its tests. |
| `makemigrations` | `-m/--message`, `--check`, `--merge`, `--database` | Write a revision; `--check` fails if one is missing; `--merge` joins two heads. |
| `migrate` | `[target]`, `--downgrade`, `--allow-data-loss`, `--database` | Apply revisions, or revert down to a target. |
| `showmigrations` | `--database` | Applied and pending revisions. |
| `runserver` | `[addr:port]`, `--noreload` | Development server. Production runs uvicorn directly. |
| `shell` | | Python console with settings, the registry, and connections loaded. |
| `runworker` | `--once`, `--metrics-port`, `--metrics-address` | Run outbox messages (messaging). |
| `messages` | `--dead`, `--limit`, `--retry ID\|all` | Inspect the outbox, and requeue dead letters (messaging). |
| `auditlog` | `--actor`, `--action`, `--limit` | Read the audit trail (audit). |

## Extension points

| To add | Implement | Register with |
| --- | --- | --- |
| A bounded context | a package with `apps.py` (`DomainConfig`) | `INSTALLED_DOMAINS` |
| Domain settings | a Pydantic model | `DomainConfig.settings_class` |
| Startup wiring | `DomainConfig.ready()` | (called by `setup()`) |
| Process resources (clients, pools) | `async DomainConfig.startup()` / `shutdown()` | (called by the app lifespan) |
| A readiness check | `async DomainConfig.check()` | (called by `/ready`) |
| Work staged with each commit | `async DomainConfig.stage(uow, events, tasks)` | (called by the unit of work) |
| Middleware | an ASGI class taking `app` | `MIDDLEWARE` |
| An error tracker | `(exc, context) -> None` | `ERROR_REPORTERS` |
| A shared rate-limit store | `ratelimit.Backend` | `RATE_LIMIT_BACKEND` |
| A command | `BaseCommand` | `management/commands/<name>.py` |
