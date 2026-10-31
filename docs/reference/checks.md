# Deployment checks

Every problem `check` and startup report is a check with an id
(`domainkit.checks`): a namespace, then `E` for an error, `W` for a
warning, or `I` for a note, and a number, as `security.E001`. Each run
reports every problem it finds, not the first: one error lists every
breach of the baseline below, and `check` lists the application's
problems, layer rules, and migration heads together. Where a message does
not say how to fix it, a `Hint:` line does.

With `ENVIRONMENT` set to `staging` or `production`, settings that break
the baseline do not load, so startup and every command fail on:

| Id | Refused |
| --- | --- |
| `security.E001` | `DEBUG = True` |
| `security.E002` | `domainkit.debug.DebugToolbarMiddleware` in `MIDDLEWARE` |
| `security.E003` | `*` in `ALLOWED_HOSTS` |
| `security.E004` | a `TRUSTED_PROXIES` network that holds every address (`0.0.0.0/0`, `::/0`) |
| `security.E005`, `security.E006` | `ECHO` or `LOG_PARAMETERS` on any database |
| `security.E007` | a network database without `SSLMODE = "verify-full"` |
| `deploy.E001` | a `memory://` ledger |
| `security.E008`, `security.E009` | an immudb ledger without `TLS_CA` or `SERVER_SIGNING_KEY` |
| `security.E010`, `security.E011`, `security.E012` | a Redis cache on another host without `rediss://` (`rediss+sentinel://`, `rediss+cluster://`); any cache URL that weakens certificate checks (`ssl_cert_reqs=none` or `optional`, `ssl_check_hostname=false`); a network cache without a password or a client certificate |
| `deploy.E002` | a `memory://` broker |
| `security.E013`, `security.E014`, `security.E015` | a broker on another host without `amqps://`; a broker URL that turns certificate checks off (`no_verify_ssl`); a broker without a user of its own (none, or `guest`) |
| `deploy.E003` | a `MemoryStorage` in `STORAGES`: its files live in one process, until it ends |
| `security.E016`, `security.E017`, `security.E018` | no `SECRET_KEY`; a key (or fallback) with a development prefix (`domainkit-insecure-`, `django-insecure-`); one with fewer than 50 characters, or fewer than 5 different ones |

Installed contrib apps add their own:

- auth (`auth.E001` to `auth.E007`, every one reported at once): a
  password hasher made for tests in `AUTH.PASSWORD_HASHERS`, a cookie
  sent over plain HTTP, a network rather than one address in
  `AUTH.CLIENT_CERT.PROXIES`, and `http://` in `AUTH.PASSWORD_RESET_URL`,
  `EMAIL_VERIFICATION_URL`, or `SIGNUP_URL` ([auth.md](../auth.md)); and,
  in any environment, with `JWTAuthentication`, a key of
  `AUTH.JWT.PREVIOUS_KEYS` that is the signing key or is listed twice
  (`auth.E008`), or that retires more than a day ahead (`auth.E009`)
  ([auth.md](../auth/authentication.md#replacing-a-key)); and, in any
  environment, a setting that contradicts `AUTH.PASSWORD_PRESET`
  (`auth.E010`), each contradiction named with its PCI DSS requirement
  ([PCI DSS v4.0's passwords](../auth/pci-dss.md))
- evidence: no `EVIDENCE.SUBJECT_KEY`
  ([contrib.md](../contrib/evidence.md#evidence-records-that-provably-never-change))
- mail: the console or memory backend; and in any environment without
  `DEBUG`, SMTP without TLS ([mail.md](../mail.md))
- notifications: the console or memory backends ([mail.md](../mail.md))
- webhooks: a loopback or link-local network in
  `WEBHOOKS.ALLOWED_PRIVATE_NETWORKS`
  ([contrib.md](../contrib/webhooks.md#webhooks-signed-events-for-other-services))

The application refuses to start there with a route no rule guards
(`security.E101`), operation ids that clash (`openapi.E001`), and the
problems installed domains find (`auth.E101` to `auth.E104`; `auth.E105`,
password and code attempts counted in each process (a `MemoryBackend` as
`RATE_LIMIT_BACKEND`), unless `AUTH.REQUIRE_SHARED_THROTTLES = False`;
`idempotency.E001`; `idempotency.E002`, keyed requests without a deadline,
unless `IDEMPOTENCY.REQUIRE_DEADLINE = False`; `audit.E001` to `audit.E003`,
no `AUDIT.LEDGER`, no `AUDIT.NAMESPACE` on it, or nothing that seals on a
schedule, and at startup `audit.E004` and `audit.E005`, an audited
PostgreSQL database whose `protect()` triggers could not be checked or are
missing, unless `AUDIT.REQUIRE_BASELINE = False`;
[contrib.md](../contrib/audit.md#in-staging-and-production); and at
startup `accounting.E001` and `accounting.E002`, a ledger on PostgreSQL
whose append-only triggers are missing, disabled, or changed, or could
not be checked, unless `ACCOUNTING.REQUIRE_PROTECTION = False`
([accounting.md](../contrib/accounting.md#entries))); `setup()` refuses a domain whose `requires` is not
installed (`apps.E001`), or that defines a hook method 0.13 removed
(`apps.E002`), anywhere. `check` adds layer rules
(`architecture.E001`) and migration heads (`migrations.E001`).
`migrate --downgrade` also needs `--allow-data-loss` there.

`check` also warns about choices that are valid but weak there. It still
passes, so an API that is public on purpose can keep them; `check
--deploy` fails on them (exit status 1), for a deployment pipeline to gate
on. A project that has judged one lists its id in `SILENCED_CHECKS`, and
`check` counts it as silenced. Errors cannot be silenced: each stands for
an unsafe or broken configuration, refused until it changes.

| Id | Warned |
| --- | --- |
| `security.W001` | `API_DOCS = True`: anyone can read `/docs`, `/redoc`, and `/openapi.json` |
| `security.W002` | `SECURE_HSTS_SECONDS = 0`: browsers are not told to keep to HTTPS |
| `security.W003` | no `RATE_LIMIT`, or `RateLimitMiddleware` missing from `MIDDLEWARE` |
| `security.W004` | a rate limit with no `TRUSTED_PROXIES`: behind a load balancer, every caller would share its address and its count |
| `security.W005` | `LIVE_REAUTH_INTERVAL = None`: open WebSockets and event streams keep a revoked token |
| `security.W006` | `*` in `CORS_ALLOWED_ORIGINS`: any web site may call the API from its visitors' browsers |
| `deploy.W001` | no `REQUEST_TIMEOUT`, or `DeadlineMiddleware` missing from `MIDDLEWARE` |
| `deploy.W002` | no `MAX_CONCURRENT_REQUESTS`, or `ConcurrencyLimitMiddleware` missing |
| `deploy.W003` | a database that seems to go through PgBouncer (port 6432, or a statement cache turned off in `CONNECT_ARGS`) without `PGBOUNCER = True` |
| `deploy.W004` | a network database without `POOL_SIZE`: each process opens up to 15 connections (SQLAlchemy's 5 and 10 overflow), whatever the server's `max_connections`; size it so that processes x replicas x (`POOL_SIZE` + `MAX_OVERFLOW`) stays below it |
| `deploy.W005` | a SQLite database: each process and host sees its own file |
| `deploy.W006` | a network database whose pool holds fewer than a quarter of `MAX_CONCURRENT_REQUESTS` connections (`POOL_SIZE` + `MAX_OVERFLOW`, 5 + 10 by default): past about four requests per connection, requests wait for one, unseen by the shedding queue, up to `POOL_TIMEOUT` (10 seconds), then fail with 503 ([sizing pools](backends.md#sizing-pools)) |
| `caches.W001` | a cache without `COMPUTE_TIMEOUT` (`CACHES[alias]["COMPUTE_TIMEOUT"]`): one `get_or_set` computation that hangs holds its key until the process restarts ([cache.md](../cache.md#what-a-factory-may-use)); the `init` template sets it |
| `servers.W001` | an application that serves WebSockets under a server that reads larger messages than `MAX_REQUEST_BODY_SIZE` (from `UVICORN_WS_MAX_SIZE` or `GUNICORN_CMD_ARGS`; a deployed application also logs it at start) ([live.md](../live.md#message-size)) |
| `servers.W002` | a server on a unix socket without `"unix"` in `TRUSTED_PROXIES`: no caller has an address ([Server limits](servers.md); a deployed application also logs it at start) |
| `live.W001` | WebSockets served without `WebSocketLimitsMiddleware` ([live.md](../live.md#message-size)) |
| `blocking.W001` | a route handler or dependency of the project's domains written as `def`, run on FastAPI's thread pool ([runtime.md](../runtime.md#finding-blocking-code)); in every environment |
| `blocking.W002` | an event handler (in-process or durable) or a messaging task written as `def`: it is awaited on the event loop itself, so its work holds every request and message of the process, and the keys of the messages waiting behind it, then it fails, as it returns nothing to await ([runtime.md](../runtime.md#finding-blocking-code)); in every environment. `allow_sync` marks one that returns an awaitable on purpose |
| `auth.W001` | `AUTH.PRINCIPAL_CACHE.TTL` above 5 seconds without `STAMPS`: a revoked token works that long in the other processes ([auth.md](../auth/authentication.md#the-principal-cache)) |
| `auth.W002`, `auth.W003` | `AUTH.LOGIN_RATE` or `AUTH.MFA.RATE` set to `None`: guesses without limit |
| `auth.W004` | with `AUTH.REQUIRE_SHARED_THROTTLES = False` only: attempt throttles counted in a `RATE_LIMIT_BACKEND` each process keeps alone (`MemoryBackend`): with several processes, each allows the rates on its own; what `auth.E105` refuses ([auth.md](../auth/logging-in.md#logging-in-and-out)) |
| `auth.W005` | a session or CSRF cookie name without the `__Host-` prefix where the prefix could hold ([auth.md](../auth/authentication.md#browser-sessions-in-cookies)) |
| `audit.W001` to `audit.W005` | with `AUDIT.REQUIRE_BASELINE = False` only: what `audit.E001` to `audit.E005` refuse |
| `accounting.W001`, `accounting.W002` | with `ACCOUNTING.REQUIRE_PROTECTION = False` only, logged at startup: what `accounting.E001` and `accounting.E002` refuse |
| `accounting.W003` | the ledger without `domainkit.contrib.messaging`: no worker releases expired holds, which keep counting against what is available; silence it once `manage.py accounting expireholds` runs on the project's own schedule ([accounting.md](../contrib/accounting.md#holds)) |
| `idempotency.W001` | with `IDEMPOTENCY.REQUIRE_DEADLINE = False` only: what `idempotency.E002` refuses |

`idempotency.W002` (0.10: `IDEMPOTENCY.ANONYMOUS` left unset, so
`"per-address"`) is gone since 0.11, whose default is `"refuse"`; listed
in `SILENCED_CHECKS`, it silences nothing.

The production settings that `init` create avoid the first three, and ask
for `TRUSTED_PROXIES`: leave it empty only when callers connect directly.
They count requests, passwords, and codes in Redis, from `REDIS_URL`.

There `check` also says, as `security.I001` (information: never a
failure, nothing to silence), what it cannot check: HTTPS. The
application serves plain HTTP, and TLS ends in front of it, where no
setting reaches. The line names where a request's scheme comes from: the
`PROXY_SCHEME_HEADER` of `TRUSTED_PROXIES`, which the proxy must set over
a caller's, or the connection alone, where every request counts as plain
HTTP ([security.md](../security.md), "The edge").
