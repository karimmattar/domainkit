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
files. Its errors name the variable and what it must be, never the value,
which may be a secret. Numbers must be finite here and in every setting:
infinity and NaN are refused.

`repr(settings)` and its dumps show domain settings (`AUTH`, `EVIDENCE`, ...)
as `**********`: they may hold keys, which their own models keep as
secrets. Percent-encode `/ ? # @ :` in the user names and passwords of
URLs; an unreadable port is refused without quoting the URL.

### Core

| Setting | Default | Meaning |
| --- | --- | --- |
| `SETTINGS_MODULE` | set by the loader | The module these settings came from. |
| `BASE_DIR` | the project root | The directory holding `pyproject.toml`. |
| `ENVIRONMENT` | `"local"` | `local`, `test`, `staging`, or `production`. Staging and production get the [deployment checks](#deployment-checks). |
| `DEBUG` | `False` | Development conveniences; refused in staging and production. |
| `SLOW_CALLBACK_DURATION` | `0.1` | With `DEBUG`, the application's loop runs in asyncio's debug mode and logs each step that holds it this many seconds or more ([runtime.md](runtime.md#finding-blocking-code)); `None` turns it off. |
| `PROJECT_NAME` | `"API"` | The OpenAPI title. |
| `VERSION` | `"0.1.0"` | The OpenAPI version. |
| `SECRET_KEY` | `None` | Signs what the service hands out: reset links, cookies, MFA challenges ([Signing](#signing)). Required in staging and production, with 50 random characters or more; shown as `**********`. |
| `SECRET_KEY_FALLBACKS` | `()` | Earlier keys: what they signed still verifies while `SECRET_KEY` signs, so a key can be replaced without logging anyone out. Held to the same rules. |
| `INSTALLED_DOMAINS` | `()` | Domain packages (Django's `INSTALLED_APPS`), started in order and stopped in reverse. |
| `MIDDLEWARE` | see [Middleware](#middleware) | ASGI middleware by dotted path, outermost first. |
| `ROOT_ROUTER` | `None` | Dotted path to the `APIRouter` that mounts every route (Django's `ROOT_URLCONF`). |
| `ASGI_APPLICATION` | `None` | `module:attribute` that `runserver` serves. |
| `EVENT_LOOP` | `"auto"` | The event loop of `runserver` and of commands with an `async` handle (`runworker` among them): uvloop when the `uvloop` extra is installed (`auto`), always asyncio's own (`asyncio`), or uvloop or a configuration error (`uvloop`). |
| `LOGGING` | minimal | A `logging.config.dictConfig` mapping, applied by `setup()`. `domainkit.log.build_logging_config(log_format="json", level="INFO")` makes one that writes from a thread, through a bounded queue ([runtime.md](runtime.md#logging-and-error-reports)); the settings `init` writes use it. |
| `ERROR_REPORTERS` | `()` | Callables `(exc, context) -> None` by dotted path, called for every unexpected error (500s, dead-lettered messages), on a reporting thread and in the caller's context; 100 reports may wait, and more are dropped and counted. A reporter that raises is logged and skipped. |
| `EVENT_HANDLER_TIMEOUT` | `10.0` | Seconds an in-process event handler may run after a commit, up to 3600; one still running then is cancelled, logged, and reported (`HandlerTimedOut`), and the request goes on. `event_bus.subscribe(..., timeout=30)` sets one handler's limit; `durable=True` runs a handler from the outbox instead ([runtime.md](runtime.md#event-handlers)). |
| `SHUTDOWN_TIMEOUT` | `15.0` | Seconds a shutdown may take in all, from 0 to 300: domain hooks, work in flight, background tasks, then connections, each phase within its share ([runtime.md](runtime.md#shutting-down-within-a-budget)). Add the server's graceful shutdown (`uvicorn --timeout-graceful-shutdown 10`) and keep the sum under the orchestrator's grace period: 30 s in Kubernetes. |

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
| `STATEMENT_TIMEOUT` | `30.0` | Seconds; PostgreSQL cancels longer statements. Migrations are exempt. This and the four timeouts below are at most 2147483.647 seconds (24.8 days), the longest PostgreSQL takes; a transaction's `statement_timeout` from a longer deadline asks for that much. |
| `LOCK_TIMEOUT` | `10.0` | Seconds a statement waits for a lock. Migrations are exempt. |
| `IDLE_IN_TRANSACTION_TIMEOUT` | `60.0` | Seconds before PostgreSQL ends a session idling inside a transaction. |
| `CONNECT_TIMEOUT` | `10.0` | Seconds to open a connection (asyncpg's `timeout`; libpq's `connect_timeout`, in whole seconds), migrations included. A request that needs a new connection waits this long at most, after its `POOL_TIMEOUT`. |
| `COMMAND_TIMEOUT` | `STATEMENT_TIMEOUT` plus 5 | Seconds asyncpg waits for one statement before giving up on it (a network or server that stopped answering). Longer than `STATEMENT_TIMEOUT`, so the server ends a slow statement first, with an error that keeps the connection. None without `STATEMENT_TIMEOUT`; migrations are exempt. |
| `PGBOUNCER` | `False` | The database is reached through PgBouncer's transaction pooling: no statement cache, a unique name for every prepared statement, and the timeouts above set in each transaction (`SET LOCAL`) rather than for the session. PostgreSQL URLs only ([runtime.md](runtime.md#database-timeouts-and-pgbouncer)). |
| `POOL_SIZE`, `MAX_OVERFLOW`, `POOL_RECYCLE` | SQLAlchemy's | Connection pool sizing. |
| `POOL_TIMEOUT` | `10.0` for network databases, else SQLAlchemy's | Seconds a request waits for a connection; past it the request gets 503 `database_busy`. |
| `POOL_PRE_PING` | `True` | Test connections before use, so database restarts are survived. |
| `TEST_NAME` | `None` | The PostgreSQL database `domainkit.testing` creates for the tests, and drops after them; `test_<name>` when unset. It must start with `test_` ([docs/testing.md](testing.md)). |
| `CONNECT_ARGS` | `{}` | Extra driver arguments, added to those derived from the options above. asyncpg's `server_settings` and libpq's `options` are merged with the timeouts. Setting what the options above already set (TLS, a timeout, PgBouncer's statement cache) is refused when the application is built, however the server setting is spelled (`STATEMENT_TIMEOUT`, `--statement-timeout=`); change that option instead. `timeout`, `command_timeout`, and `connect_timeout` still replace `CONNECT_TIMEOUT` and `COMMAND_TIMEOUT`, with a `DomainkitDeprecationWarning`; they are refused from 0.9. |
| `REPLICAS` | `()` | Aliases of read replicas of this database; read-only units of work take turns on them ([databases.md](databases.md#read-replicas)). Each is an alias of its own, other than `default`, of one primary, outside every shard group, with no replicas of its own, and never a routing target. |

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
| `URL` | `memory://` | `memory://` (this process), `redis://` or `rediss://` (Redis or Valkey, through redis-py; the `redis` extra), `redis+sentinel://` or `redis+cluster://` (a Sentinel group or a Cluster, by several addresses; `rediss+` for TLS; see [cache.md](cache.md#sentinel-and-cluster)), or `dummy://` (keeps nothing). redis-py URL options (`?socket_keepalive=true`) apply to one server. Kept secret. |
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
| `SENTINEL_PASSWORD` | `None` | `redis+sentinel://` only: the Sentinels' own password, when they ask for one (the URL's is the primary's). Kept secret. |

### Storages

Where files live; see [uploads.md](uploads.md).

| Setting | Default | Meaning |
| --- | --- | --- |
| `STORAGES` | `{}` | File storages by alias (below), as Django's `STORAGES`. `storages["default"]` is the one `save_upload` and `serve` use unless told otherwise. |

Each `STORAGES` entry:

| Key | Default | Meaning |
| --- | --- | --- |
| `BACKEND` | `domainkit.storage.FileSystemStorage` | Dotted path to a `domainkit.storage.Storage` class: `FileSystemStorage`, `MemoryStorage` (tests; refused in staging and production), or your own. |
| `OPTIONS` | `{}` | The class's keyword arguments. `FileSystemStorage` takes `location` (an absolute directory), `base_url` (where a web server serves it, if one does), `file_mode` (`0o600`), and `directory_mode` (`0o700`). |

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
| `CONNECT_TIMEOUT` | `10.0` | Seconds to connect; also the deadline of opening channels, declaring, binding, setting the prefetch, and starting or stopping consumers. |
| `PUBLISH_TIMEOUT` | `10.0` | Seconds to wait for the broker to confirm a published message. Relays lock a message for this, `CONNECT_TIMEOUT`, and five seconds while they publish it. |

### HTTP

| Setting | Default | Meaning |
| --- | --- | --- |
| `ALLOWED_HOSTS` | `localhost`, `127.0.0.1`, `[::1]` | Accepted `Host` values; `*.example.com` matches subdomains. A `Host` that is not a name, an IPv4 address, or a bracketed IPv6 address (with an optional port) is refused before matching. A wildcard `*` is refused in staging and production. |
| `API_DOCS` | `True` | Serve `/docs`, `/redoc`, and `/openapi.json`. `check` warns when it is on in staging and production. |
| `OPENAPI_OPERATION_IDS` | `"fastapi"` | `"route_name"`: each operation id is the route's name, stable when routers move (see [The API surface](#the-api-surface)). `init` sets it for new projects. |
| `ERROR_FORMAT` | `"domainkit"` | `"problem+json"`: errors are RFC 9457 problem details (see [Errors](#errors)). |
| `ERROR_TYPE_BASE_URL` | `None` | With problem details, each code's `type` is this absolute URL plus the code (`https://errors.example.com/` gives `https://errors.example.com/not_found`); unset, it is `about:blank`. |
| `MAX_REQUEST_BODY_SIZE` | `1048576` | Bytes; larger bodies get 413, whether declared or streamed. A larger WebSocket message, text counted in UTF-8 bytes, closes the connection with 1009. |
| `REQUEST_BODY_TIMEOUT` | `10.0` | Seconds (at most 3600) the application may wait for a request's body, all its chunks together (`RequestBodyLimitMiddleware`); past it the request is answered 408 `request_timeout` and the connection closed. Time spent working between chunks, or after the response starts, does not count. |
| `REQUEST_TIMEOUT` | `30.0` | Seconds (at most 3600) an HTTP request may take until its response starts (`DeadlineMiddleware`); past it the work is cancelled and answered 503 `deadline_exceeded`. A route sets its own with `RequestTimeout`. `None`: no deadline ([runtime.md](runtime.md#request-deadlines)). |
| `MAX_CONCURRENT_REQUESTS` | `1000` | HTTP requests each event loop works on at once (`ConcurrencyLimitMiddleware`), at most 100,000; a request waiting for its body is not counted meanwhile. `None`: no cap ([runtime.md](runtime.md#load-shedding)). |
| `MAX_QUEUED_REQUESTS` | `100` | Requests past the cap that may wait for a slot (at most 100,000); the next are answered 503 `overloaded` at once. |
| `REQUEST_QUEUE_TIMEOUT` | `1.0` | Seconds (at most 60) a request past the cap waits for a slot before it is answered 503 `overloaded`. |
| `CONCURRENCY_EXEMPT_PATHS` | `/health`, `/ready`, `/metrics` | Exact paths never counted against the cap, so probes and scrapes are never refused; compared without the root path the application is mounted at (`--root-path`). |
| `HTTP_CLIENT` | see [Outgoing HTTP calls](#outgoing-http-calls) | Defaults of outgoing calls through `domainkit.http.client`. |
| `READINESS_TIMEOUT` | `5.0` | Seconds each readiness check may take (at most 60). |
| `READINESS_CACHE_SECONDS` | `1.0` | How long a `/ready` result is reused, from when its checks ended; probes that come while checks run wait for them, so probe floods cannot load the database. |
| `READINESS_DETAILS` | `False` | Name failing components in `/ready` responses (always logged). |
| `ACCESS_LOG_QUIET_PATHS` | `/health`, `/ready` | Successful requests to these paths are logged at DEBUG. |
| `ACCESS_LOG_SAMPLE_RATE` | `1.0` | Share of access lines written, from 0 to 1: `0.1` writes about one request in ten, each drawn on its own. Errors (500 and up, or no response), refusals (401, 403, 429), and requests that take `ACCESS_LOG_SLOW_DURATION` or longer are always written; `domainkit.security` and the audit trail are never sampled ([runtime.md](runtime.md#sampling-the-access-log)). |
| `ACCESS_LOG_SLOW_DURATION` | `1.0` | Seconds (at most 3600): a request this slow or slower has its access line written whatever `ACCESS_LOG_SAMPLE_RATE` says. |
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

### Outgoing HTTP calls

`HTTP_CLIENT` keys: the defaults of every `domainkit.http.client.HttpClient`
(the `httpclient` extra), which may change its own timeout and retries. See
[runtime.md](runtime.md#outgoing-http-calls).

| `HTTP_CLIENT` key | Default | Meaning |
| --- | --- | --- |
| `TIMEOUT` | `10.0` | Seconds (at most 3600) one call may take, its retries included, and never past the deadline of the work it serves. |
| `CONNECT_TIMEOUT` | `5.0` | Seconds to open a connection. |
| `RETRIES` | `2` | Retries (0 to 10) of a call that failed in a way worth trying again. |
| `BACKOFF` | `0.1` | Seconds before the first retry; the bound doubles for each next one, and the wait is random below it. |
| `MAX_BACKOFF` | `2.0` | The longest wait before a retry (at most 600 seconds); a longer `Retry-After` ends the retries. |
| `BREAKER_FAILURES` | `5` | Failures in a row that open a host's circuit: its calls then fail at once with `CircuitOpen`. |
| `BREAKER_COOLDOWN` | `30.0` | Seconds (at most 3600) a host's circuit stays open; then one call at a time tries it. A client made with `breaker=False` keeps no circuits. |
| `MAX_CONNECTIONS` | `100` | Connections per client and event loop. |
| `MAX_KEEPALIVE_CONNECTIONS` | `20` | Idle connections each client keeps for the next calls. |
| `MAX_RESPONSE_BYTES` | `10485760` | The largest answer read (at most 1 GiB), counted after decompression; a longer one raises `ResponseTooLarge`. |
| `USER_AGENT` | `"domainkit"` | The `User-Agent` of every call (printable ASCII). |
| `CA_BUNDLE` | `None` | CA certificates of the servers called; the system's (or httpx2's) when unset. Guarded clients never trust the CAs `SSL_CERT_FILE` or `SSL_CERT_DIR` name, unless listed here. |

### Languages and time zones

See [i18n.md](i18n.md).

| Setting | Default | Meaning |
| --- | --- | --- |
| `LANGUAGE_CODE` | `"en"` | The language of responses when `Accept-Language` asks for none of `LANGUAGES`, and outside requests. It must be one of them. |
| `LANGUAGES` | `(("en", "English"), ("ar", "Arabic"))` | `(code, name)` of each language responses may be in; `LocaleMiddleware` picks one per request. Codes are lower case (`ar`, `pt-br`). |
| `LOCALE_PATHS` | `()` | Directories of `<locale>/LC_MESSAGES/messages.mo` catalogs, looked up before each installed domain's `locale` directory, then domainkit's own. |
| `TIME_ZONE` | `"UTC"` | The zone dates are shown in (`localtime`, `localdate`, and the date formats). Dates are stored and compared in UTC whatever it says. |

### Server limits

The application limits bodies, WebSocket messages, request rates, how long
a request may take (`REQUEST_TIMEOUT`), and how many it works on at once
(`MAX_CONCURRENT_REQUESTS`). Slow and oversized requests are the server's
to stop, before the application sees them. Run the server behind a reverse
proxy or load balancer that limits header size and the time to send
headers and body (nginx:
`large_client_header_buffers`, `client_header_timeout`,
`client_body_timeout`), and set the server's own limits. domainkit runs on
[uvicorn](https://www.uvicorn.org/) or [Granian](https://github.com/emmett-framework/granian)
(`pip install 'domainkit[granian]'`); [runtime.md](runtime.md#servers)
compares them.

uvicorn, one process per core:

```bash
uvicorn config.asgi:application --workers 4 --loop uvloop \
  --limit-concurrency 1200 --backlog 2048 --timeout-keep-alive 5 \
  --timeout-graceful-shutdown 10 \
  --h11-max-incomplete-event-size 16384 --ws-max-size 1048576 \
  --no-proxy-headers --no-server-header
```

Granian, the same:

```bash
granian --interface asgi config.asgi:application --host 0.0.0.0 --port 8000 \
  --workers 4 --loop uvloop --backpressure 1200 --backlog 2048 \
  --http1-header-read-timeout 5000 --workers-kill-timeout 10 \
  --respawn-failed-workers --no-ws
```

| Concern | uvicorn | Granian |
| --- | --- | --- |
| Requests at once, per process | `--limit-concurrency` (more get 503) | `--backpressure` |
| Idle keep-alive connections | `--timeout-keep-alive` seconds | `--http1-keep-alive` / `--no-http1-keep-alive` |
| Slow headers | the proxy | `--http1-header-read-timeout` milliseconds |
| Request line and headers | `--h11-max-incomplete-event-size` (16 KiB) | `--http1-buffer-size` |
| WebSocket message | `--ws-max-size` bytes (16 MiB unless set) | no setting: `--no-ws` if unused, else the proxy |
| Graceful shutdown | `--timeout-graceful-shutdown` seconds, then open connections are closed | `--workers-kill-timeout` seconds, then workers are killed |

Keep the shutdown timeout below the orchestrator's grace period (30
seconds on Kubernetes) less the time the application takes to close its
resources, so a stop never ends in `SIGKILL`.

Keep `--limit-concurrency` (Granian: `--backpressure`) above
`MAX_CONCURRENT_REQUESTS` plus `MAX_QUEUED_REQUESTS`, and the WebSockets
you expect: it is the backstop.

A request whose body is slow to arrive holds no slot of
`MAX_CONCURRENT_REQUESTS` while it waits, and is answered 408 after
`REQUEST_BODY_TIMEOUT`; but it still holds a connection, the server's
place in `--limit-concurrency`, and what it has sent so far. A buffering
proxy in front is still recommended: nginx reads the whole body before it
passes the request on (`proxy_request_buffering on`, its default), within
`client_body_timeout` and `client_max_body_size`, so slow clients cost
the proxy, not the application.
The application refuses first, with a JSON 503 and `Retry-After`, and never
refuses the probes in `CONCURRENCY_EXEMPT_PATHS`; uvicorn's 503 is plain, and
refuses probes too. Keep `REQUEST_TIMEOUT` below the load balancer's idle
timeout (60 seconds on most), so a slow request gets the application's 503,
not a 504 from the balancer.

`--ws-max-size` stops a WebSocket message before it is read whole;
`MAX_REQUEST_BODY_SIZE` then closes the connection on one that got through.
`check` warns, in staging and production, when an application that serves
WebSockets runs under a server that reads messages larger than
`MAX_REQUEST_BODY_SIZE`: uvicorn without
`--ws-max-size` (or with a larger one), or Granian with WebSockets on. It
reads the server's environment variables (`UVICORN_WS_MAX_SIZE`,
`GRANIAN_WEBSOCKETS`, which both servers accept in place of options, and
`GUNICORN_CMD_ARGS`); a deployed application logs the same warning when it
starts, from the server's own command line. A value it cannot read is
taken as unknown, and never stops the application or `check`.

uvicorn started by gunicorn (`-k uvicorn.workers.UvicornWorker`, or
`uvicorn_worker.UvicornWorker` from the `uvicorn-worker` package) or by
`fastapi run` reads neither `--ws-max-size` nor `UVICORN_WS_MAX_SIZE`: it
always reads messages of up to 16 MiB, and `check` warns about it. Under
gunicorn, give a worker class of your own the limit, and start gunicorn
with `-k config.workers.LimitedWorker`:

```python
from uvicorn_worker import UvicornWorker


class LimitedWorker(UvicornWorker):
    CONFIG_KWARGS = {**UvicornWorker.CONFIG_KWARGS, "ws_max_size": 1_048_576}
```

`check` does not look inside such a class, so it says nothing about it. For
`fastapi run`, run uvicorn itself. Hypercorn, which reads up to 16 MiB as
well (`websocket_max_message_size`, in its configuration file), and
gunicorn with a worker class named only in its configuration file, are not
recognised: `check` says nothing about them, so set their limits yourself.

`--no-proxy-headers` leaves `X-Forwarded-*` to `TRUSTED_PROXIES`: uvicorn
otherwise reads them itself, from `127.0.0.1` or from whatever
`FORWARDED_ALLOW_IPS` allows (`*` lets any client choose its address, and
so its rate limit and login throttle). Granian reads none. `runserver`
runs this way, with either server.

### Live connections

WebSockets and Server-Sent Events streams are capped, queued, timed, and
re-authorized while they stay open; see [live.md](live.md).

| Setting | Default | Meaning |
| --- | --- | --- |
| `LIVE_MAX_CONNECTIONS` | `1000` | Open WebSockets and event streams per process; one more is refused with 503 and `Retry-After` (a WebSocket where the server cannot answer a handshake with a status: a 1013 close). |
| `LIVE_MAX_CONNECTIONS_PER_CALLER` | `10` | The same per user, or per address for anonymous callers (IPv6 per `RATE_LIMIT_IPV6_PREFIX` network). At most `LIVE_MAX_CONNECTIONS`. |
| `LIVE_SEND_QUEUE` | `64` | Messages or events that may wait for one client (1 to 10,000); a sender waits while they do. Live events count only those the client may see; 16 times as many may wait for its rule ([live.md](live.md#live-committed-events)). |
| `LIVE_SEND_TIMEOUT` | `10.0` | Seconds one message may take to reach the client; a slower client is closed (1013). |
| `LIVE_REAUTH_INTERVAL` | `60.0` | Seconds between re-checks of an open connection's credentials and its route's rules (each between three quarters of it and all of it; at most 3,600); closed with 1008 when they fail. `None` turns them off; `check` warns about that in staging and production. What re-checks cannot do: [live.md](live.md#what-re-checks-cannot-do). |
| `WEBSOCKET_IDLE_TIMEOUT` | `300.0` | Seconds without a message either way before a WebSocket is closed (1001). |
| `WEBSOCKET_MAX_LIFETIME` | `3600.0` | Seconds a WebSocket may stay open (1001). |
| `WEBSOCKET_MESSAGE_RATE` | `"20/second"` | Messages a client may send per connection, counted in the process; one more closes it (1008). `None`: any. |
| `SSE_KEEPALIVE` | `15.0` | Seconds without an event before an event stream sends a keep-alive comment. |
| `SSE_MAX_LIFETIME` | `3600.0` | Seconds an event stream may stay open. |

### Debug toolbar

`DebugToolbarMiddleware` (`domainkit.debug`) shows what each request cost
the database, while `DEBUG` is on. The local settings `init` creates list
it last in `MIDDLEWARE`; right after `RequestContextMiddleware`, it also
counts the queries that authenticate the request.

Every response gets `Server-Timing: db;dur=12.3;desc="7 queries", app;dur=45.6`
(browsers show it in their network panel). A statement run
`REPEATED_QUERIES` times or more in one request is logged as a likely N+1,
by its shape: the SQL with every value and placeholder replaced by `?`.
Bound values are never read. Staging and production refuse to start with
it listed.

It counts statements as `assert_num_queries` does
([testing.md](testing.md#counting-queries)), but only the request's own,
so concurrent requests do not mix. Find a request's queries with the
toolbar, then keep them in check with `assert_max_queries` in a test.

| `DEBUG_TOOLBAR` key | Default | Meaning |
| --- | --- | --- |
| `REPEATED_QUERIES` | `5` | Runs of one statement shape in one request that log a likely N+1. |
| `HEADERS` | `False` | Also send `X-Debug-Queries`, `X-Debug-Query-Time` (milliseconds), and `X-Debug-Repeated-Queries`. |
| `PROFILE_DIR` | `None` | A directory: a request sent with `X-Debug-Profile: 1` leaves a cProfile file there, named in the response's `X-Debug-Profile` (one request at a time; read it with `python -m pstats`). |

### Deployment checks

With `ENVIRONMENT` set to `staging` or `production`, startup fails on:

- `DEBUG = True`
- `domainkit.debug.DebugToolbarMiddleware` in `MIDDLEWARE`
- `*` in `ALLOWED_HOSTS`
- no `SECRET_KEY`, or a key (or fallback) with fewer than 50 characters,
  fewer than 5 different ones, or a development prefix
  (`domainkit-insecure-`, `django-insecure-`)
- a `TRUSTED_PROXIES` network that holds every address (`0.0.0.0/0`, `::/0`)
- `ECHO` or `LOG_PARAMETERS` on any database
- a network database without `SSLMODE = "verify-full"`
- a `memory://` ledger, or an immudb ledger without `TLS_CA` or
  `SERVER_SIGNING_KEY`
- a `memory://` broker, a broker on another host without `amqps://`, a
  broker URL that turns certificate checks off (`no_verify_ssl`), and a
  broker without a user of its own (none, or `guest`)
- a `MemoryStorage` in `STORAGES`: its files live in one process, until
  it ends
- a Redis cache on another host without `rediss://` (`rediss+sentinel://`,
  `rediss+cluster://`), or without a password or a client certificate, and
  any cache URL that weakens certificate checks (`ssl_cert_reqs=none` or
  `optional`, `ssl_check_hostname=false`)

Installed contrib apps add their own:

- auth: a password hasher made for tests in `AUTH.PASSWORD_HASHERS`, a
  network rather than one address in `AUTH.CLIENT_CERT.PROXIES`, and
  `http://` in `AUTH.PASSWORD_RESET_URL`, `EMAIL_VERIFICATION_URL`, or
  `SIGNUP_URL` ([auth.md](auth.md))
- evidence: no `EVIDENCE.SUBJECT_KEY`
  ([contrib.md](contrib.md#evidence-records-that-provably-never-change))
- mail: the console or memory backend; and in any environment without
  `DEBUG`, SMTP without TLS ([mail.md](mail.md))
- notifications: the console or memory backends ([mail.md](mail.md))
- webhooks: a loopback or link-local network in
  `WEBHOOKS.ALLOWED_PRIVATE_NETWORKS`
  ([contrib.md](contrib.md#webhooks-signed-events-for-other-services))

`migrate --downgrade` also needs `--allow-data-loss` there.

`check` also warns about choices that are valid but weak there. It still
passes, so an API that is public on purpose can keep them:

- `API_DOCS = True`: anyone can read `/docs`, `/redoc`, and `/openapi.json`
- `SECURE_HSTS_SECONDS = 0`: browsers are not told to keep to HTTPS
- no `RATE_LIMIT`, or `RateLimitMiddleware` missing from `MIDDLEWARE`
- a rate limit with no `TRUSTED_PROXIES`: behind a load balancer, every
  caller would share its address and its count
- no `REQUEST_TIMEOUT`, or `DeadlineMiddleware` missing from `MIDDLEWARE`;
  no `MAX_CONCURRENT_REQUESTS`, or `ConcurrencyLimitMiddleware` missing
- a database that seems to go through PgBouncer (port 6432, or a statement
  cache turned off in `CONNECT_ARGS`) without `PGBOUNCER = True`
- auth: `AUTH.LOGIN_RATE` or `AUTH.MFA.RATE` set to `None` (guesses
  without limit), and a session or CSRF cookie name without the `__Host-`
  prefix where the prefix could hold
  ([auth.md](auth.md#browser-sessions-in-cookies))
- `LIVE_REAUTH_INTERVAL = None`: open WebSockets and event streams keep a
  revoked token
- an application that serves WebSockets under a server that reads larger
  messages than `MAX_REQUEST_BODY_SIZE` (from `UVICORN_WS_MAX_SIZE` or
  `GUNICORN_CMD_ARGS`; a deployed application also logs it at start), or
  without
  `WebSocketLimitsMiddleware`
  ([live.md](live.md#message-size))

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
| `domainkit.http.locale.LocaleMiddleware` | Picks the request's language from `Accept-Language`, before anything below answers, so their errors are translated; adds `Content-Language` and `Vary: Accept-Language` ([i18n.md](i18n.md)). |
| `domainkit.http.middleware.CORSMiddleware` | Answers preflights before any work. |
| `domainkit.http.middleware.RequestContextMiddleware` | Request id (`X-Request-ID`), access log, the context that logs and audit rows read. WebSocket connections get one too: method `WS`, the request id sent with the accept, one access log line when the connection ends (101 if accepted, 403 if closed first). |
| `domainkit.http.deadlines.DeadlineMiddleware` | Starts the request's deadline (`REQUEST_TIMEOUT`) before any work, and answers 503 `deadline_exceeded` when it passes; lifted once the response starts ([runtime.md](runtime.md#request-deadlines)). |
| `domainkit.http.shedding.ConcurrencyLimitMiddleware` | Sheds requests past `MAX_CONCURRENT_REQUESTS` with 503 `overloaded`, before authentication looks anything up; never the probes ([runtime.md](runtime.md#load-shedding)). |
| `domainkit.http.middleware.AllowedHostsMiddleware` | Refuses unknown `Host` headers with 400. |
| `domainkit.contrib.auth.middleware.AuthenticationMiddleware` | Finds the user from the request's token and makes it the actor, so what follows counts users (auth; see [auth.md](auth.md)). |
| `domainkit.http.ratelimit.RateLimitMiddleware` | Counts per actor, or per client address (IPv6: per /64). A WebSocket handshake counts once; over the limit it gets the 429, or a 1013 close where the server cannot send one. |
| `domainkit.http.websockets.WebSocketLimitsMiddleware` | Caps open WebSockets per process and per caller, queues what they send, limits their messages, closes idle and old ones, and re-checks their credentials and rules ([live.md](live.md)). After the two above, so it counts users and only handshakes the rate limit let through. |
| `domainkit.http.middleware.RequestBodyLimitMiddleware` | Refuses bodies over `MAX_REQUEST_BODY_SIZE` with 413, and bodies slower than `REQUEST_BODY_TIMEOUT` with 408; closes WebSockets with 1009 on a larger message. |
| `domainkit.contrib.idempotency.middleware.IdempotencyMiddleware` | Last: reads whole bodies, which the limit above has already bounded. |
| `domainkit.debug.DebugToolbarMiddleware` | Development only: times each request's SQL, from where it is listed inwards (see [Debug toolbar](#debug-toolbar)). |

The application also wraps its routes in a middleware of its own,
innermost, that adds the headers of [deprecated
routes](#versions-and-deprecations).

`DEFAULT_MIDDLEWARE` (proxy headers, security headers, request context,
deadlines, the concurrency cap, allowed hosts, WebSocket limits, body
limit) applies when a settings module sets no `MIDDLEWARE`.

## Signing

`domainkit.signing` is Django's `signing`: values the service hands out and
takes back unchanged, such as a link in an email.

```python
from datetime import timedelta

from domainkit import signing

signer = signing.Signer(salt="newsletter.unsubscribe")
value = signer.sign("42")  # "42:<signature>"
signer.unsign(value)  # "42", or BadSignature if anything changed

token = signing.dumps({"invite": 7}, salt="invites")  # timestamped JSON
signing.loads(token, salt="invites", max_age=timedelta(days=7))
```

| Name | What it does |
| --- | --- |
| `Signer(salt=, key=, fallback_keys=)` | `sign`, `unsign`, `sign_object`, `unsign_object`. |
| `TimestampSigner` | The same, with the time signed: `unsign(value, max_age=)` raises `SignatureExpired` past it. |
| `dumps(obj, salt=)`, `loads(value, salt=, max_age=)` | Compact, URL-safe, signed, timestamped JSON. |
| `BadSignature`, `SignatureExpired` | Refusals; the second is a kind of the first. Neither quotes the value. |
| `derive_key(purpose)`, `derive_keys(purpose)` | A key of its own for `purpose` (HKDF-SHA256), from `SECRET_KEY` and then each fallback. |
| `constant_time_compare(a, b)` | Compare secrets without telling how close they came. |

A signature is HMAC-SHA256, compared in constant time, under a key
derived from `SECRET_KEY` for its salt, so a value signed for one purpose
never verifies for another. Signed values are readable by anyone: sign
ids, not secrets. To replace `SECRET_KEY`, put the old key first in
`SECRET_KEY_FALLBACKS` and the new one in `SECRET_KEY`; drop the old one
once what it signed has expired. A project without `SECRET_KEY` starts
locally, and fails the first time something signs. See
[ADR 0028](adr/0028-one-secret-key-signs-and-derives.md).

### Encryption

Secrets the database must give back, such as TOTP secrets or a signing
secret shared with another service, are kept encrypted with
`domainkit.encryption.SecretBox` (it needs `cryptography`: the `mfa`
extra):

```python
from domainkit.encryption import SecretBox

box = SecretBox("billing.webhook_secret")  # a purpose of its own
sealed = box.encrypt(secret, associated=subscription_id.bytes)  # bytes to store
secret, current = box.decrypt(sealed, associated=subscription_id.bytes)
```

It is AES-256-GCM under a key derived from `SECRET_KEY` for the purpose,
with a random nonce per value. `associated` binds the value to its owner:
a value copied to another row does not decrypt. Values encrypted under a
key now in `SECRET_KEY_FALLBACKS` still decrypt, with `current` False:
encrypt them again then. Anything else raises `DecryptionFailed`, which
quotes nothing. See [ADR 0030](adr/0030-encrypt-totp-secrets-under-a-derived-key.md).

## Errors

Every error response is JSON: `{"detail", "code", "request_id"}`, plus
`errors` (location, message, type) for validation errors and for values a
constraint refused. Submitted values are never echoed back. With
`LocaleMiddleware`, `detail` and each `msg` are in the request's language;
`code` and `type` never change ([i18n.md](i18n.md)).

With `ERROR_FORMAT = "problem+json"`, every error, the middleware's
included, is RFC 9457 problem details, as `application/problem+json`:

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

`instance` is the request's path, without its query. `code`, `request_id`,
and `errors` are extension members, so clients can switch on `code` either
way. With `ERROR_TYPE_BASE_URL` each code is a problem type of its own:
`type` is that URL plus the code, and `title` the code in words; serve a
page there that describes each code. OpenAPI documents the format in use.
See ADR 0032.

| Raised | Status | `code` |
| --- | --- | --- |
| `NotFound` | 404 | from the class, such as `invoice_not_found` |
| `Conflict`, `ConcurrentUpdate`, `DuplicateValue`, `LockNotAvailable` | 409 | from the class; a unique constraint may name its own (see [databases.md](databases.md#unique-values)). A `DuplicateValue` adds one entry to `errors` per field, `loc` `["body", <field>]`; `lock_not_available` for `get_for_update(nowait=True)` ([queries.md](queries.md#row-locks)) |
| `BusinessRuleViolation` | 422 | from the class |
| `PermissionDenied` | 403 | from the class |
| other `DomainError` | 400 | from the class |
| `domainkit.http.APIError(status, detail, code=…)` | as given | as given |
| request validation | 422 | `validation_error` |
| a cursor this API did not issue for the request's ordering, or one that was altered | 422 | `invalid_cursor` ([queries.md](queries.md#pages)) |
| a filter a `FilterSet` does not offer, given twice, or over its bounds | 422 | `invalid_filter` ([queries.md](queries.md#filters-over-http)) |
| `PreconditionFailed`: a write whose `If-Match` names another version than the stored one | 412 | `precondition_failed` ([resources.md](resources.md#versions-and-conditional-requests)) |
| a write without `If-Match` to a resource that requires it | 428 | `precondition_required` |
| a partial change of a field the caller may not change (`FieldNotEditable`) | 403 | `field_not_editable` ([resources.md](resources.md#field-policies)) |
| no pooled connection within `POOL_TIMEOUT`, or a statement cancelled by `STATEMENT_TIMEOUT` or `LOCK_TIMEOUT`, or lost to a deadlock or a serialization failure | 503 | `database_busy`, with `Retry-After: 1`; logged as a warning, not reported |
| a value the database refuses (SQLSTATE class 22: a NUL character, text too long, a number out of range) | 422 | `invalid_value`; logged as a warning, not reported |
| a foreign key (SQLSTATE 23503, or SQLite's `FOREIGN KEY constraint failed`): the row is still referenced, or refers to one that does not exist | 409 | `referenced`; the detail says which side when the database does (PostgreSQL), and `errors` names the field of a missing reference |
| a NOT NULL column (23502) | 422 | `missing_value`, with the field in `errors` |
| a CHECK constraint (23514) | 422 | `check_violation`, with the fields of the columns it names |
| a unique constraint outside a unit of work (23505) | 409 | as `DuplicateValue` |
| a request past its deadline (`REQUEST_TIMEOUT`, or the route's `RequestTimeout`) | 503 | `deadline_exceeded`, with `Retry-After: 1`; logged once, as a warning |
| no slot under `MAX_CONCURRENT_REQUESTS`: the queue was full, or the wait ran out, for a new request or for the next chunk of a body it waited for | 503 | `overloaded`, with `Retry-After: 1`; warned about at most every 10 seconds |
| a request body that took longer than `REQUEST_BODY_TIMEOUT` to arrive | 408 | `request_timeout`, with `Connection: close`; logged as a security event |
| a URL a guarded `HttpClient` may not call, or one that leaves a client's base URL | 422 | `unsafe_url` ([runtime.md](runtime.md#outgoing-http-calls)) |
| unexpected exception | 500 | `internal_error`; logged and sent to `ERROR_REPORTERS` |

WebSockets the database refuses are closed with 1013 (busy) or 1007 (a
value it refuses).

Fields are named as the API knows them: a column's `info={"field": ...}`,
else the attribute of the class mapped onto its table, else the column's
name. Constraint errors are logged with their SQLSTATE and the
constraint's name, never with the row's values. SQLite checks foreign keys
only on connections that turn `PRAGMA foreign_keys` on.

## JSON responses

`domainkit.http.JSONResponse` is Starlette's `JSONResponse` with pydantic's
encoder (`pydantic_core.to_json`): the same bytes, two to three times
sooner on a page of resources. Resources, error bodies, and readiness
answers use it. Where pydantic writes a value otherwise than `json.dumps`
(a float from 1e-9 up to 1e-4, NaN or Infinity, a `None` key), the content
goes through `json.dumps` as before, so JSON content gets the same bytes
and the same errors. Values JSON has no type for, which Starlette refuses,
pydantic encodes (a `Decimal` as a string); see ADR 0063.

Routes with a response model (a return annotation, or `response_model=`)
are encoded by pydantic in FastAPI already: leave them FastAPI's default,
as any response class set turns that off and changes how their floats are
written. Give it to routes without one, or build it from JSON content:

```python
from domainkit.http import JSONResponse


@router.get("/totals", response_model=None, response_class=JSONResponse)
async def totals() -> dict[str, object]:
    return {"count": 3, "amount": "12.50"}
```

FastAPI passes such a route's value through `jsonable_encoder` first,
which costs more than encoding it (about 1 ms for 50 resources); a response
model skips both, but writes a `Decimal` as a string, where
`jsonable_encoder` makes a float of one returned outside a model.

## The API surface

### Operation ids

FastAPI makes each operation id from the route's name, path, and method
(`list_products_api_v1_products_get`), so moving a router to another
prefix renames every method of generated clients. With
`OPENAPI_OPERATION_IDS = "route_name"`, the id is the route's name alone:
its function's name, or `name=`. Names must then be unique: `check` lists
clashes, and staging and production refuse to start with any. A route with
several methods has one id, as FastAPI makes it (and warns). See ADR 0031.

`python manage.py openapi` prints the schema (`--output openapi.json`
writes it), and `openapi --diff openapi.json` lists what changed since in
ways that break clients, and exits 1 if anything did:

- a path or an operation removed, or a success status no longer answered
  (or no longer answered with JSON);
- a new required parameter, request body, or body field;
- a request value accepted before and refused now: a narrower type, fewer
  enum values, a tighter bound, or fields a closed object no longer takes;
- a response field removed or no longer always present, or one that may
  now hold another type or enum value.

Renaming a path parameter breaks nothing. Run it in CI against the schema
of the last release.

The old schema must be a JSON file of at most 64 MiB whose arrays and
objects nest at most 256 deep (`MAX_SCHEMA_BYTES`, `MAX_SCHEMA_DEPTH`);
other files are refused before they are parsed.

### Versions and deprecations

```python
from datetime import UTC, datetime

from fastapi import Depends

from domainkit.http.versioning import Deprecation, versioned_router

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
async def old_report() -> Report: ...
```

A version's operation ids are the version and the route's name
(`v1_list_products`), whatever `OPENAPI_OPERATION_IDS` says, so versions
can share route names. A `Deprecation` on a router, an `include_router`
call, or a route makes every response of those routes, errors included,
carry `Deprecation: @<seconds>` (RFC 9745), `Sunset: <HTTP date>`
(RFC 8594), and `Link: <...>; rel="deprecation"`; the most specific one
wins. OpenAPI marks the operations `deprecated`, with the headers on each
response, `x-deprecated-since`, `x-sunset`, and the link as `externalDocs`.
Times need a time zone; a date means midnight UTC.

## Observability

**Logs** are JSON lines with `request_id`, and with `trace_id` and `span_id`
when a span is active. Query strings and bound SQL values are never logged.
They are written by a thread, never by the event loop; a burst past the
queue's 10,000 records is dropped and counted, never waited for
([runtime.md](runtime.md#logging-and-error-reports)).

**Metrics** (`domainkit[metrics]`, mounted at `/metrics`):

| Metric | Labels |
| --- | --- |
| `http_requests_total` | `method` (`OTHER` for any but the nine standard ones), `route` (the template, never the raw path), `status` |
| `http_request_duration_seconds` | `method`, `route` |
| `http_requests_in_progress` | `method` |
| `db_pool_connections` | `database`, `state` |
| `http_requests_admitted` | (requests holding a slot under `MAX_CONCURRENT_REQUESTS`) |
| `http_requests_queued` | (requests waiting for a slot) |
| `http_requests_shed_total` | `reason` (`queue_full`, `queue_timeout`) |
| `http_request_deadlines_exceeded_total` | |
| `messaging_messages_total` | `kind`, `outcome` |
| `messaging_message_duration_seconds` | `kind` |
| `messaging_outbox_messages` | `status` |
| `messaging_outbox_lag_seconds` | `queue` (seconds its oldest due message has waited for a worker) |
| `messaging_relayed_total` | `outcome` (`published`, `failed`) |
| `messaging_deliveries_skipped_total` | |
| `cache_lookups_total` | `cache`, `result` (`hit`, `miss`, `invalid`, `early`) |
| `cache_errors_total` | `cache` |

**Traces** (`domainkit[tracing]`): unit-of-work commits and worker messages
get spans. domainkit only uses the OpenTelemetry API; configure the SDK and
exporter in the deployment, or run under `opentelemetry-instrument`. Outbox
messages carry the W3C trace context of their commit, so a worker's span
continues the trace of the request that queued its work
([contrib.md](contrib.md#traces)). FastAPI (0.142 and later) has telemetry
of its own, which `get_asgi_application` turns off: its spans record query
strings, and its log records exception messages, which domainkit keeps out
of logs and error reports. To trace requests, run under
`opentelemetry-instrument`, as before.

## Testing

`domainkit.testing` is a pytest plugin, like pytest-django. Install it with
the `testing` extra, which brings pytest and httpx2, as a development
dependency, and enable it in the project's `pyproject.toml`:

```toml
[tool.pytest.ini_options]
addopts = "-p domainkit.testing"
domainkit_settings = "config.settings.test"
```

It selects the test settings, never reads `.env`, and refuses settings whose
`ENVIRONMENT` is not `"test"`. Its fixtures make the test databases
(`test_<name>` on PostgreSQL, dropped after the run unless `--keepdb`; see
`TEST_NAME`), put them back before each test (`db`), and serve the
application to sync and async tests (`client`, `client_for`, `admin_client`,
`async_client`, `async_client_for`, `async_admin_client`). It also brings
factories, a clock to freeze (`freeze_time`), and query-count assertions
(`assert_num_queries`, `assert_max_queries`). `--detect-blocking` fails
tests that hold their event loop. [docs/testing.md](testing.md) covers them
all.

Fixtures for published events and queued work (`captured_events`,
`outbox`, `execute_deferred`) come with messaging's own plugin
([contrib.md](contrib.md#testing)).

## Commands

Run as `python manage.py <command>`; `init` runs before a project exists, as
`domainkit init`. Domains add their own in `management/commands/<name>.py`
with a `Command(BaseCommand)` class; `handle` may be `async`.

| Command | Options | Purpose |
| --- | --- | --- |
| `init` | `name`, `[directory]` | Create a project: base, local, test, and production settings, routes, `manage.py`, `.env`, and a first test. The name sets the environment prefix; the directory (default `./<name>`) must be empty or missing. |
| `check` | | Settings, domains, routes and their access rules, middleware, layer rules, and a single migration head. It warns about route handlers and dependencies of the installed domains written as `def`, which run on FastAPI's thread pool ([runtime.md](runtime.md#finding-blocking-code)); in staging and production it also lists [weak choices](#deployment-checks) as warnings. |
| `broker` | `check\|declare`, `[ALIAS ...]` | Connect to brokers (all by default), or declare the exchange and every queue the worker's tasks use on `MESSAGING.BROKER`. |
| `compilemessages` | `[DIRECTORY ...]`, `-l/--locale`, `--check` | Compile translation catalogs (`.po`) to `.mo` files: in the directories named, or in `LOCALE_PATHS` and each installed domain's `locale` directory. `--check` writes nothing and fails if one is missing or out of date ([i18n.md](i18n.md)). |
| `cache` | `check\|clear`, `[ALIAS ...]`, `--all` | Ping caches (all by default), or delete every key of the named caches, all versions, in their namespace only. Clearing needs aliases or `--all`. |
| `startdomain` | `name`, `--aggregate`, `--plural`, `--parent`, `--tests-dir`, `--manual` | Generate a bounded context and its tests ([resources.md](resources.md#generators)). In a project laid out as `init` makes it, it also adds the domain to `INSTALLED_DOMAINS` and mounts its router; `--manual`, or a layout it does not recognise, prints those steps instead. |
| `startaggregate` | `domain`, `aggregate`, `--field name:kind[:options][?]`, `--plural`, `--parent`, `--tests-dir` | Add an aggregate to a domain: model, events, commands, use cases, table, repository, resource, fakes, and tests. `--help` lists the field kinds. |
| `addcommand` | `domain`, `aggregate`, `command`, `--field`, `--event`, `--plural`, `--parent`, `--tests-dir` | Add a task command: the aggregate's method and event, the command, its use case, its route, and tests. |
| `addquery` | `domain`, `aggregate`, `query`, `--by`, `--path`, `--plural`, `--parent`, `--tests-dir` | Add a query that finds aggregates by fields: the message and handler (`application/queries.py`), its route, and tests. |
| `addevent` | `domain`, `aggregate`, `event`, `--field`, `--no-handler`, `--plural`, `--parent`, `--tests-dir` | Add a domain event, and a durable handler subscribed in `apps.py`, with its test. |
| `makemigrations` | `-m/--message`, `--check`, `--merge`, `--empty`, `--database` | Write a revision per target with changes; `--check` fails if one is missing; `--merge` joins two heads; `--empty` writes a revision with nothing in it, to fill in by hand (a data migration; it needs `--database` when there are several targets). Without `--database`, every target. The message is one line, without backslashes or triple quotes. |
| `migrate` | `[revision]`, `--downgrade`, `--allow-data-loss`, `--database`, `--plan`, `--sql`, `--from`, `--fake` | Apply revisions (or revert down to one) on every database of every target, then create partitions. A revision id needs `--database` when there are several targets. `--plan` lists the revisions that would run; `--sql` prints their SQL, rendered offline by Alembic, from the database's revision or `--from`; `--fake` records the revision without running anything, for a schema made by other means. None of the three changes the schema, but `--fake --downgrade` still needs `--allow-data-loss` in staging and production. |
| `squashmigrations` | `[end]`, `--start`, `--database`, `-m/--message` | Replace a target's revisions, `--start` (default: the first) to `end` (default: the head), with one; see [Squashing migrations](#squashing-migrations). |
| `showmigrations` | `--database` | Applied and pending revisions; a shard group lists each database. |
| `partitions` | `--database`, `--prune`, `--dry-run` | Create the current and upcoming partitions of partitioned tables; `--prune` drops those past `retain`. |
| `runserver` | `[addr:port]`, `--noreload`, `--server uvicorn\|granian` | Development server, on uvicorn or Granian (the `granian` extra). Production runs the server directly ([Server limits](#server-limits)). |
| `shell` | `-c/--command`, `--no-imports` | An asyncio Python console, where `await` works at the top level, with settings, the registry, connections, `asyncio`, and every installed domain's models, repositories, and units of work by name (unless `--no-imports`). `-c` runs code and exits. |
| `dbshell` | `--database`, `--owner`, `--allow-production`, `-- [ARG ...]` | Open `psql` or `sqlite3` on an alias, as its `URL`'s user (`--owner`: `MIGRATION_URL`'s). The connection and password reach `psql` in libpq's environment variables, never on its command line; TLS follows `SSLMODE` and its files. Production refuses without `--allow-production`. Arguments after `--` go to the client. |
| `diffsettings` | `--all` | The settings that differ from the defaults; `--all` lists every one, defaults marked `###`. Secrets stay hidden as in `repr(settings)`, and so do entries of free-form settings named like secrets (`password`, `token`, `key`, ...) and passwords in URLs. |
| `dumpdata` | `[TABLE_OR_DOMAIN ...]`, `--database`, `--output`, `--indent` | Write rows as a JSON fixture; see [Data: fixtures, flush, and seeds](#data-fixtures-flush-and-seeds). |
| `loaddata` | `FIXTURE ...`, `--database`, `--dry-run`, `--force` | Load fixtures, saying first what each table gets and how many stored rows it replaces. Staging and production refuse without `--force`. |
| `flush` | `--database`, `--dry-run`, `--force` | Delete every row but the audit trail's, saying first what goes, then rerun the `migrated` hooks (content types, permissions). Staging and production refuse without `--force`. |
| `seed` | `[LABEL ...]` | Run installed domains' `DomainConfig.seed` hooks, in `INSTALLED_DOMAINS` order. |
| `openapi` | `--output`, `--indent`, `--diff OLD` | Print or write the OpenAPI schema; `--diff` lists breaking changes since `OLD` and exits 1 if any (see [The API surface](#the-api-surface)). |
| `runworker` | `--once`, `--metrics-port`, `--metrics-address`, `--database`, `--queue`, `--no-relay` | Run outbox messages (messaging): one worker per outbox, every hosting database by default; `--queue` (repeatable) limits them to some queues. With `MESSAGING.BROKER`, relay to the broker and run what it delivers; `--no-relay` only consumes. `--once` always works from the outbox. Whatever it serves, the process runs at most `MESSAGING.CONCURRENCY` messages at once. |
| `messages` | `--pending\|--locked\|--dead`, `--queue`, `--limit`, `--retry ID\|all`, `--cancel ID`, `--database` | Count an outbox's messages and show each queue's lag, list them, requeue dead letters, or cancel a message no worker runs (messaging). |
| `webhooks` | `subscriptions\|deliveries\|replay`, `[ID ...]`, `--failed`, `--status`, `--subscription`, `--limit` | List webhook subscriptions or deliveries, or send finished deliveries again (webhooks). |
| `auditlog` | `--actor`, `--action`, `--limit`, `--database`, `--target`, `--aggregate FIELD=ID` | Read the audit trail, or one record's history with what each entry changed (audit; see [contrib.md](contrib.md#history)). |
| `auditseal` | `--late` | Seal new audit rows into each database's hash chain and anchor its head on `AUDIT.LEDGER`; `--late` also seals rows that committed later than `AUDIT.SEAL_LATE_WINDOW`, flagged as sealed late (audit). |
| `auditverify` | `--database` | Check the audit trail against its seals and anchors; fails if anything changed (audit). |
| `auditforget` | `subject ...`, `--anonymous-days` | Make people's pseudonyms in the audit trail unlinkable, in every audited database (audit). |
| `privacy` | `export --subject ID [--output FILE]`, `erase --subject ID [--reason] [--noinput]`, `purge [--dry-run]` | Export or erase what every installed domain holds about a person, from `Personal` markers on aggregates, or apply the domains' retention policies; each is audited (privacy; see [privacy.md](privacy.md)). |
| `evidence` | `--subject`, `--verify KEY`, `--limit` | List a subject's evidence, or verify one record (evidence). |
| `createsuperuser` | `--<USERNAME_FIELD>`, `--<field>` per `REQUIRED_FIELDS`, `--noinput` | Create a superuser, asking for what the options leave out; `--noinput` reads `<PREFIX>SUPERUSER_<FIELD>` (auth; see [auth.md](auth.md)). |
| `changepassword` | `[username]` | Set a user's password, asked twice and validated; the user's tokens end (auth). |
| `createtoken` | `username`, `--name`, `--days`, `--no-expiry`, `--scope` | Print a new token for a user's scripts, shown once, limited to the `--scope` permissions if given (auth). |
| `cleartokens` | `--user` | Delete expired tokens, or every token of one user (auth). |
| `resetmfa` | `username` | Turn a user's second factor off, and end their sessions (auth). |
| `addcertificate` | `username`, `certificate`, `--name` | Let a client certificate log in as a user, by its SHA-256 (auth). |
| `flags` | `list`, `set NAME`, `delete NAME`, `settings`, `setting KEY [VALUE] [--reset]` | List and change feature flags and dynamic settings (flags; see [contrib.md](contrib.md#flags-feature-flags-and-dynamic-settings)). |

### Data: fixtures, flush, and seeds

A fixture is one JSON document, its tables parents first:

```json
{"format": "domainkit.fixture", "version": 1, "tables": [
  {"table": "shop_products", "rows": [{"id": "6f1c7e6a-...", "name": "Lamp", "version": 1}]}
]}
```

Numbers and text are written as they are; decimals, times, durations, ids,
and enums as text; bytes as base64; JSON columns as JSON. `loaddata`
converts each value back by its column's type, and refuses one that does
not fit without quoting it. A row whose primary key is stored replaces the
stored row. Rows load in one transaction, and on PostgreSQL serial ids then
continue after the highest one loaded.

State is never dumped or loaded: the audit trail (a copy of its keys would
outlive `auditforget`), tokens, the outbox, idempotency keys, webhook
subscriptions (they hold signing secrets: set them up in each environment)
and deliveries, and any table with `info={"fixtures": False}`. A fixture can still hold personal data and
password hashes: keep it as safe as the database. `dumpdata --output`
writes a new file only its owner can read (mode `0600`), and refuses a
path that exists, a symbolic link too, so a link planted in a shared
directory cannot redirect it: delete an old fixture before dumping again.

`flush` keeps append-only tables, those with `info={"append_only": True}`
or, on PostgreSQL, triggers of their own, and those with
`info={"flush": False}`: the audit trail survives it. Serial ids start
again on PostgreSQL.

Each command works on one alias (`--database`, default `default`), as the
schema owner (`MIGRATION_URL` when set).

`seed` calls each installed domain's `async DomainConfig.seed()`: sample
data for development, or defaults a deployment starts with. Write rows
through the domain's units of work, and make a second run harmless.

### Squashing migrations

`squashmigrations` writes one revision that does what a run of a target's
revisions did: its `upgrade` runs each one's `upgrade`, oldest first, and
its `downgrade` each one's `downgrade`, newest first. Each body is copied
as written, comments included, into a function of its own, and imports are
merged. Then the run's files are deleted.

The new revision keeps the id of the newest revision it replaces, and the
`down_revision` of the oldest. So databases at or past the newest revision,
and revisions written after it, need nothing. A database at a revision
inside the run would be at one that no longer exists:

- The command refuses while a database in `DATABASES` is there.
- It cannot see other environments: deploy the squash only once every
  database (staging, production, developers' copies) is at or past the
  newest revision of the run.

It also refuses what it cannot copy and keep correct:

- a merge, a branch, a branch label, or `depends_on` in the run;
- code outside `upgrade` and `downgrade` other than imports (move helpers
  into them first), or two revisions importing different things under one
  name;
- a `downgrade` that raises: the squashed revision could not be reverted
  either.

Comments outside the two functions are not kept. Check the result as any
revision: the test database migrates up, down, and up again.

## Extension points

| To add | Implement | Register with |
| --- | --- | --- |
| A bounded context | a package with `apps.py` (`DomainConfig`) | `INSTALLED_DOMAINS` |
| Domain settings | a Pydantic model | `DomainConfig.settings_class` |
| Startup wiring | `DomainConfig.ready()` | (called by `setup()`) |
| Process resources (clients, pools) | `async DomainConfig.startup()` / `shutdown()` | (called by the app lifespan; after the domains stop, the framework closes its caches, databases, ledgers, and brokers, in that order, each within 5 seconds, and a failure is logged without keeping the rest open; management commands close them too) |
| A readiness check | `async DomainConfig.check()` | (called by `/ready`) |
| A check of the built application (routes, middleware) | `DomainConfig.check_application(app)`, returning problems | (called when the app is built and by `check`; staging and production refuse to start with any) |
| Warnings about a domain's weak settings where deployed | `DomainConfig.deployment_warnings()`, returning warnings (none outside staging and production) | (listed by `check` after the framework's own) |
| Work staged with each commit | `async DomainConfig.stage(uow, events, tasks)` | (called by the unit of work) |
| Work after each commit | `async DomainConfig.committed(uow)` | (called by the unit of work; failures are reported, never raised) |
| Rows after each `migrate` (`post_migrate`) | `async DomainConfig.migrated(connection, database)` | (called by `migrate` for each database it upgraded, in one transaction as the schema owner, and by `flush`) |
| Sample or default data | `async DomainConfig.seed()` | (called by `seed`, in `INSTALLED_DOMAINS` order) |
| How long a domain keeps rows | `DomainConfig.RETENTION`, a sequence of `privacy.Retention` | (checked when the app is built; applied by `privacy purge`; see [privacy.md](privacy.md)) |
| Erasing a personal field no default fits | `erase_personal_data(fields)` on the aggregate | (called by `privacy erase` and retention erasure) |
| How errors name a column | `info={"field": "<API name>"}` on the `Column` | (read by unique, foreign key, NOT NULL, and CHECK errors) |
| A table fixtures or `flush` leave alone | `info={"fixtures": False}`, `{"flush": False}`, or `{"append_only": True}` on the `Table` | (read by `dumpdata`, `loaddata`, and `flush`) |
| A deprecated route or version | `Depends(Deprecation(...))`, `versioned_router(...)` | `domainkit.http.versioning` |
| Tables a domain stops using | `DomainConfig.holds_table(table)` | (asked by migrations and content types; a swapped user table answers no) |
| A user model | a subclass of `AbstractUser` or `AbstractBaseUser`, and its table | `AUTH.USER_MODEL`, `AUTH.USER_TABLE` |
| An authentication or permission backend | `auth.backends.BaseBackend` | `AUTH.BACKENDS` |
| A way requests prove who sent them | `auth.authentication.BaseAuthentication` | `AUTH.AUTHENTICATION_CLASSES` |
| A rule for who may call a route | `auth.access.BasePermission` | `Depends(Require(rule))` on the route, or `Public` for anyone |
| A password hasher | `auth.hashers.PasswordHasher` | `AUTH.PASSWORD_HASHERS` |
| A password rule | a class with `validate(password, user)` and `help_text()` | `AUTH.PASSWORD_VALIDATORS` |
| An email backend (a provider's API) | `mail.BaseEmailBackend` | `MAIL.BACKEND` |
| A text-message or push provider | `notifications.SmsBackend`, `notifications.PushBackend` | `NOTIFICATIONS.SMS_BACKEND`, `NOTIFICATIONS.PUSH_BACKEND` |
| Middleware | an ASGI class taking `app` | `MIDDLEWARE` |
| An error tracker | `(exc, context) -> None` | `ERROR_REPORTERS` |
| A rate-limit store other than memory or Redis | `ratelimit.Backend` | `RATE_LIMIT_BACKEND` |
| A command | `BaseCommand` | `management/commands/<name>.py` |
