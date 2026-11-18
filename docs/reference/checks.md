# Deployment checks

A [check](../learning.md#words) is a test of your settings or your code.
domainkit runs the checks when the settings load, when the application
starts, and when you run `python manage.py check`. In staging and
production, an error stops the service from starting, so an unsafe
configuration never serves a request. This page lists every check by its
id.

On this page you find:

- how to read a check's id and its message;
- the errors that staging and production refuse when the settings load;
- the errors of the optional apps, and those found when the application
  starts;
- the warnings `check` gives, and how to silence one;
- the note `check` gives about HTTPS.

## Reading a check

Every check has an id: a namespace, then a letter, then a number. The
letter says how serious the check is:

| Letter | Means | Example |
| --- | --- | --- |
| `E` | An error. The configuration is unsafe or broken, and is refused until it changes. You cannot silence it. | `security.E001` |
| `W` | A warning. The choice is valid but weak. `check` still passes; `check --deploy` fails. You can silence it. | `security.W002` |
| `I` | Information. It never fails, and there is nothing to silence. | `security.I001` |

Each run reports every problem it finds, not only the first one. When the
message does not say how to fix the problem, a `Hint:` line does. For
example, these production settings break three rules at once:

```python
from domainkit.conf import Settings

Settings(ENVIRONMENT="production", DEBUG=True, ALLOWED_HOSTS=["*"])
```

```text
pydantic_core._pydantic_core.ValidationError: 1 validation error for Settings
  Value error, The deployment checks failed:
  security.E001: DEBUG must be False in production
    Hint: DEBUG shows tracebacks and settings to callers.
  security.E003: ALLOWED_HOSTS must list real host names in production
    Hint: List the names clients reach the service by, as 'api.example.com'.
  security.E016: SECRET_KEY is required in production: it signs cookies, codes, and other values the service hands out
    Hint: Make one with: python -c 'import secrets; print(secrets.token_urlsafe(64))'. [type=value_error, ...]
```

`check` lists the application's problems, the layer rules, and the
migration heads together. The checks live in `domainkit.checks`:
`CheckMessage(id, message, hint="")` is one finding (a `str`), and `CheckFailed`
is the error that lists them.

## Errors when the settings load

With `ENVIRONMENT` set to `staging` or `production`, settings that break
the baseline below do not load. So startup and every command fail on:

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
| `security.E016`, `security.E017`, `security.E018` | no `SECRET_KEY`; a key (or a fallback) with a development prefix (`domainkit-insecure-`, `django-insecure-`); a key with fewer than 50 characters, or fewer than 5 different ones |
| `deploy.E004` | a `MAIL.BACKEND` made for development and tests (console, memory), with `domainkit.contrib.mail` installed |
| `deploy.E005`, `deploy.E006` | a `NOTIFICATIONS.SMS_BACKEND` or `PUSH_BACKEND` made for development and tests, with `domainkit.contrib.notifications` installed |
| `deploy.E007` | `SEARCH` with `MemorySearchBackend`: its index lives in one process |
| `security.E019` | no `EVIDENCE.SUBJECT_KEY` (or `SUBJECT_KEYS`), with `domainkit.contrib.evidence` installed: the development key is public ([evidence](../contrib/evidence.md#evidence-records-that-provably-never-change)) |
| `security.E020` | an `IDENTITY.LDAP` directory reached by `ldap://` without `START_TLS`, with `domainkit.contrib.identity` installed: every password a login checks would cross the network in clear. Use an `ldaps://` URL, or set `START_TLS`. |
| `documents.E001` | a `DOCUMENTS["RENDERER"]` `URL` of plain `http://` to another host: documents, and Gotenberg's password, would cross the network unencrypted. A renderer on `localhost` or a loopback address may keep http, and `ALLOW_HTTP = True` accepts a network that encrypts by itself, such as a service mesh ([documents.md](../documents.md#hardening-gotenberg)). New in 2.5. |

`migrate --downgrade` also needs `--allow-data-loss` in staging and
production ([commands](commands.md)).

## Errors of `domainkit.contrib.auth`

`domainkit.contrib.auth` checks its own settings when it starts
([auth.md](../auth.md)). It reports every problem at once.

| Id | Where | Refused |
| --- | --- | --- |
| `auth.E001` | everywhere | `AUTH.CLIENT_CERT.HEADER` without `AUTH.CLIENT_CERT.PROXIES`: the header is believed only from the proxies listed there |
| `auth.E002` | everywhere | `AUTH.CLIENT_CERT.MATCH = "subject"` through `HEADER` without `VERIFY_HEADER`: anyone could sign a certificate that names any user |
| `auth.E003` | everywhere | `AUTH.PRINCIPAL_CACHE.STAMPS` that does not name a Redis cache of `CACHES`, which every process shares |
| `auth.E004` | staging, production | a password hasher made for tests in `AUTH.PASSWORD_HASHERS` |
| `auth.E005` | staging, production | `AUTH.COOKIE.SECURE = False`: session cookies sent over plain HTTP |
| `auth.E007` | staging, production | a network, not one address (`/32` or `/128`), in `AUTH.CLIENT_CERT.PROXIES`: any host there could name any user |
| `auth.E008` | everywhere, with `JWTAuthentication` | a key of `AUTH.JWT.PREVIOUS_KEYS` that is the signing key, or that is listed twice ([Replacing a key](../auth/authentication.md#replacing-a-key)) |
| `auth.E009` | everywhere, with `JWTAuthentication` | a key of `AUTH.JWT.PREVIOUS_KEYS` that retires more than a day ahead |
| `auth.E010` | everywhere | a setting that contradicts `AUTH.PRESET`; each contradiction is named with its PCI DSS requirement ([PCI DSS v4.0's passwords](../auth/pci-dss.md)) |
| `auth.E011` | everywhere | `AUTH.IDENTIFIERS` names a field the user model does not have, or one field twice ([Logging in with an email address or a mobile number](../auth/logging-in.md#logging-in-with-an-email-address-or-a-mobile-number)) |
| `auth.E012` | everywhere | `AUTH.NAME_FIELDS` names a field that cannot name users on the admin API's `GET /users/names`, one message a case: a field the user model does not have; one the admin API never shows (`password`, `last_login`, the ids and grants); one the model does not show (add it to `EDITABLE_FIELDS` or `READ_ONLY_FIELDS`); or one that holds no text (a date, a flag, a number). The username is taken: `/users` shows it as its own key ([The admin API](../auth/routes.md#the-admin-api)) |

Two other apps refuse a setting without an id:

- `domainkit.contrib.mail` refuses SMTP without TLS
  (`MAIL.SMTP.SECURITY = "none"`) unless `DEBUG` is on, in any
  environment ([mail.md](../mail.md)).
- `domainkit.contrib.webhooks` refuses a loopback or link-local network in
  `WEBHOOKS.ALLOWED_PRIVATE_NETWORKS`, in staging and production
  ([webhooks](../contrib/webhooks.md#webhooks-signed-events-for-other-services)).

## Errors when the application starts

These checks need the built application: its routes and middleware, or
its database. In staging and production, the application refuses to
start with any of them, and `check` fails. In `local` and `test`, the
application logs them as a warning and starts.

| Id | Refused |
| --- | --- |
| `security.E101` | a route that no rule guards, while `REQUIRE_ROUTE_RULES` is on ([Deny by default](../auth/routes.md#deny-by-default)) |
| `openapi.E001` | two routes with one operation id ([Operation ids](responses.md#operation-ids)) |
| `tenancy.E001` | a route whose `PathTenant` names a path parameter its path does not have ([A tenant from the path](../tenancy.md#a-tenant-from-the-path)) |
| `tenancy.E002` | a route whose own dependencies check callers (`Require`) before `PathTenant` serves the path's tenant |
| `tenancy.E003` | a route under the path of a strict `PathTenant` (a resource's whose items are tenants: `/companies/{company_id}/...`) that does not take its tenant from it, such as a mount or a Starlette route added to a router after it was included into the resource ([A resource whose items are tenants](../resources.md#a-resource-whose-items-are-tenants)) |
| `auth.E101` | routes that check users, but `MIDDLEWARE` lacks `AuthenticationMiddleware` |
| `auth.E102` | auth's cookie routes mounted, but `AUTH.AUTHENTICATION_CLASSES` lacks `CookieAuthentication` |
| `auth.E103` | auth's routes that send mail, without the apps they need in `INSTALLED_DOMAINS` |
| `auth.E104` | auth's routes that send mail, with a user model that has no `EMAIL_FIELD` or no `email_verified` |
| `auth.E105` | in staging and production: password and code attempts counted in each process (a `MemoryBackend` as `RATE_LIMIT_BACKEND`), unless `AUTH.REQUIRE_SHARED_THROTTLES = False` |
| `auth.E106` | a route whose `StepUp(methods=...)` asks for methods that `AUTH.STEP_UP.METHODS` never proves, so it refuses everyone |
| `auth.E107` | auth's routes that send codes (resets, signups) to mobile numbers, where `mobile_number` is in `AUTH.IDENTIFIERS`, without `domainkit.contrib.notifications` in `INSTALLED_DOMAINS` ([Password reset](../auth/logging-in.md#password-reset)) |
| `auth.E108` | an `Action` whose methods (or `AUTH.AUTHORIZATIONS.METHODS`) no installed way to confirm proves, such as `["phone"]` without the phone's app: nobody could confirm it ([Authorizing one act](../auth/actions.md#confirming-it)) |
| `auth.E109` | a route that asks `StepUp(for_=...)` for a purpose no registered action has ([Authorizing one act](../auth/actions.md#the-route-asks-stepupfor_)) |
| `auth.E110` | a route that asks for an action that nothing spends: no service decorated `requires_authorization`, and no code that said so with `spent_in_code(action)` ([Authorizing one act](../auth/actions.md#the-act-spends-it)) |
| `auth.E111` | `StepUp(for_=...)` on a WebSocket or an event stream, which is asked again and again and has no act to spend it on |
| `auth.E112` | `AUTH.AUTHORIZATIONS.ACTIONS` naming a purpose no registered action has: a name mistyped, or an act whose setting is off, whose `TTL` and `METHODS` would hold for nothing ([Authorizing one act](../auth/actions.md#each-act-its-own-methods-and-lifetime)) |
| `ciba.E002` | the OpenID Provider's CIBA routes served, but `AUTH.JWT` has no `ISSUER` or signs with a shared secret, so clients could not verify ID tokens ([Sign-in approvals](../auth/ciba.md#oidc-clients)) |
| `ciba.E003` | `CIBA.APPROVE_LOGINS` on, but the routes of `domainkit.contrib.ciba.api` or `domainkit.contrib.devices.api` not served, so users with a bound phone could not sign in on the web ([Sign-in approvals](../auth/ciba.md#the-mobile-app-as-identity)) |
| `ciba.E004` | `/app-sign-in/session` served, but `AUTH.AUTHENTICATION_CLASSES` lacks `CookieAuthentication`, so the browser would get a cookie nothing reads |
| `ciba.E005` | `CIBA.APPROVE_LOGINS = "always"`, but `DEVICES.ENROLLMENT` off, so no user could bind a first phone, nor log in ([Sign-in approvals](../auth/ciba.md#strict-mode-every-login-from-the-phone)) |
| `approvals.E001` | `APPROVALS.APPROVE_ACTION` on, beside an `APPROVE_RULE` or a policy's `approve_rule` that holds a `StepUp`: the approver would be asked twice, for a window and for the act ([Approving with an authorization](../contrib/approvals.md#approving-with-an-authorization)) |
| `idempotency.E001` | a keyed route whose own `RequestTimeout` reaches `IDEMPOTENCY.LEASE_SECONDS` or has none: a retry with the same `Idempotency-Key` could take the key while the request runs |
| `idempotency.E002` | in staging and production: keyed requests without a deadline (no `REQUEST_TIMEOUT`, or no `DeadlineMiddleware`), unless `IDEMPOTENCY.REQUIRE_DEADLINE = False` |
| `keys.E001` | in staging and production: `KEYS.PROVIDER = "local"` without `KEYS.ALLOW_LOCAL_KEK`, whose key-encryption key derives from `SECRET_KEY` ([keys.md](../contrib/keys.md#providers)) |
| `keys.E002` | in staging and production: `KEYS.VAULT.URL` over plain `http://`, which sends Vault's token and the data keys in clear |
| `keys.E003` | in any environment, at startup and from `check`: a key service with no `KEYS.DATA_KEYS`, so nothing can be sealed or opened. `manage.py keys generate` makes the first one ([keys.md](../contrib/keys.md)) |
| `audit.E001`, `audit.E002`, `audit.E003` | in staging and production: no `AUDIT.LEDGER`, no `AUDIT.NAMESPACE` on it, or nothing that seals on a schedule, for the parts `AUDIT.REQUIRE` names ([audit](../contrib/audit.md#in-staging-and-production)) |
| `audit.E004`, `audit.E005` | at startup in staging and production: an audited PostgreSQL database whose `protect()` triggers could not be checked, or are missing, while `AUDIT.REQUIRE` names `"triggers"` |
| `accounting.E001`, `accounting.E002` | at startup: a ledger on PostgreSQL whose append-only triggers are missing, disabled, or changed, or could not be checked, unless `ACCOUNTING.REQUIRE_PROTECTION = False` ([accounting.md](../contrib/accounting.md#entries)) |
| `limits.E001` | in any environment, at `setup()`: limits routed to another database than the ledger's ([limits.md](../contrib/limits.md)) |
| `limits.E002` | in any environment, at `setup()`: a default of `LIMITS.DEFAULTS` that is not a limit |
| `apps.E001` | in any environment, at `setup()`: a domain whose `requires` is not installed |
| `apps.E002` | in any environment, at `setup()`: a domain that defines a hook method 1.0 removed, in 2.0 too ([Extension points](extension-points.md)) |
| `architecture.E001` | from `check`: an import that breaks the layer rules ([Layer rules](settings.md#layer-rules)) |
| `migrations.E001` | from `check`: a target with more than one migration head (`makemigrations --merge` joins them) |

## Warnings

`check` also warns about choices that are valid but weak in staging and
production. `check` still passes, so an API that is public on purpose can
keep them. `check --deploy` fails on them (exit status 1), so a
deployment pipeline can stop on them. Some warnings, marked "in every
environment" below, are about code you must change, and appear
everywhere.

| Id | Warned |
| --- | --- |
| `apps.W001` | in every environment: a `start` or `stop` coroutine method of a `DomainConfig` that no hook point holds. Only registered functions run, so nothing calls it: register it in `ready()` with `on_startup(self.start)` or `on_shutdown(self.stop)` ([Extension points](extension-points.md)) |
| `security.W001` | `API_DOCS = True`: anyone can read `/docs`, `/redoc`, and `/openapi.json` |
| `security.W002` | `SECURE_HSTS_SECONDS = 0`: browsers are not told to keep to HTTPS |
| `security.W003` | no `RATE_LIMIT`, or `RateLimitMiddleware` missing from `MIDDLEWARE` |
| `security.W004` | a rate limit with no `TRUSTED_PROXIES`: behind a load balancer, every caller would share its address and its count |
| `security.W005` | `LIVE_REAUTH_INTERVAL = None`: open WebSockets and event streams keep a revoked token |
| `security.W006` | `*` in `CORS_ALLOWED_ORIGINS`: any web site may call the API from its visitors' browsers |
| `security.W007` | `UPLOAD_SCANNER` reaches clamd (`ClamAVScanner`, or a subclass) on another host without `tls`: clamd speaks no TLS, so every upload crosses the network in clear. Put a TLS proxy in front of it, or silence the warning where the network is the service's own ([Virus scanning](../uploads.md#virus-scanning)) |
| `deploy.W001` | no `REQUEST_TIMEOUT`, or `DeadlineMiddleware` missing from `MIDDLEWARE` |
| `deploy.W002` | no `MAX_CONCURRENT_REQUESTS`, or `ConcurrencyLimitMiddleware` missing |
| `deploy.W003` | a database that seems to go through PgBouncer (port 6432, or a statement cache turned off in `CONNECT_ARGS`) without `PGBOUNCER = True` |
| `deploy.W004` | a network database without `POOL_SIZE`: each process opens up to 15 connections (SQLAlchemy's 5 and 10 overflow), whatever the server's `max_connections`. Size it so that processes x replicas x (`POOL_SIZE` + `MAX_OVERFLOW`) stays below it |
| `deploy.W005` | a SQLite database: each process and host sees its own file |
| `deploy.W006` | a network database whose pool holds fewer than a quarter of `MAX_CONCURRENT_REQUESTS` connections (`POOL_SIZE` + `MAX_OVERFLOW`, 5 + 10 by default). Past about four requests per connection, requests wait for one, unseen by the shedding queue, up to `POOL_TIMEOUT` (10 seconds), then fail with 503 ([sizing pools](backends.md#sizing-pools)) |
| `caches.W001` | a cache without `COMPUTE_TIMEOUT` (`CACHES[alias]["COMPUTE_TIMEOUT"]`): one `get_or_set` computation that hangs holds its key until the process restarts ([cache.md](../cache.md#what-a-factory-may-use)). The `init` template sets it |
| `servers.W001` | an application that serves WebSockets under a server that reads larger messages than `MAX_REQUEST_BODY_SIZE` (from `UVICORN_WS_MAX_SIZE` or `GUNICORN_CMD_ARGS`; a deployed application also logs it at start) ([live.md](../live.md#message-size)) |
| `servers.W002` | a server on a unix socket without `"unix"` in `TRUSTED_PROXIES`: no caller has an address ([Server limits](servers.md); a deployed application also logs it at start) |
| `servers.W003` | `PROXY_IDLE_TIMEOUT` not shorter than the time the server keeps an idle connection open (`SERVE["KEEP_ALIVE"]`, or `UVICORN_TIMEOUT_KEEP_ALIVE`, `GRANIAN_HTTP1_HEADER_READ_TIMEOUT`, gunicorn's `--keep-alive`): the proxy may send a request on a connection as the server closes it, and answer 502. The hint gives the value to set, or, past Granian's 60 seconds, says to lower the proxy's ([The proxy in front](servers.md#the-proxy-in-front)) |
| `live.W001` | WebSockets served without `WebSocketLimitsMiddleware` ([live.md](../live.md#message-size)) |
| `live.W003` | `LIVE_FANOUT` names no backend (in staging and production) while domains share live event types (`live_events.share`), or while live routes (WebSockets, event streams) are served and events are committed by more than one process: the server's worker processes (from `--workers`, `WEB_CONCURRENCY`, `UVICORN_WORKERS`, `GRANIAN_WORKERS`, `GUNICORN_CMD_ARGS` or `SERVE["WORKERS"]`), or the messaging worker. A client sees only the events its own process committed. A deployed application also logs it at start ([live.md](../live.md#every-processs-events)) |
| `live.W002` | a route with an access rule that turns re-authorization off (`LiveLimits(reauth_interval=None)`): its open connections keep a revoked token |
| `blocking.W001` | in every environment: a route handler or dependency of the project's domains written as `def`, run on FastAPI's thread pool ([runtime.md](../runtime.md#finding-blocking-code)) |
| `blocking.W002` | in every environment: an event handler (in-process or durable) or a messaging task written as `def`. It is awaited on the event loop itself, so its work holds every request and message of the process, and the keys of the messages waiting behind it; then it fails, because it returns nothing to await ([runtime.md](../runtime.md#finding-blocking-code)). `allow_sync` marks one that returns an awaitable on purpose |
| `approvals.W001` | `APPROVALS.ADMIN_APPROVALS` holds `auth.set_group_permissions` but not `auth.set_groups`: a group made with the permissions, then joined, grants them unheld ([Changes of the admin API](../contrib/approvals.md#changes-of-the-admin-api)) |
| `auth.W001` | `AUTH.PRINCIPAL_CACHE.TTL` above 5 seconds without `STAMPS`: a revoked token works that long in the other processes ([auth.md](../auth/authentication.md#the-principal-cache)) |
| `auth.W002`, `auth.W003` | `AUTH.LOGIN_RATE` or `AUTH.MFA.RATE` set to `None`: guesses without a limit |
| `auth.W004` | with `AUTH.REQUIRE_SHARED_THROTTLES = False` only: attempt throttles counted in a `RATE_LIMIT_BACKEND` that each process keeps alone (`MemoryBackend`). With several processes, each one allows the rates on its own; this is what `auth.E105` refuses ([auth.md](../auth/logging-in.md#logging-in-and-out)) |
| `auth.W005` | a session or CSRF cookie name without the `__Host-` prefix, where the prefix could hold ([auth.md](../auth/authentication.md#browser-sessions-in-cookies)) |
| `auth.W006` | `AUTH.ACCOUNT_LOGIN_RATE` set to `None`, or to more than 100 wrong passwords a day at one account: past NIST SP 800-63B 5.2.2 ([auth.md](../auth/logging-in.md#wrong-passwords-and-device-proofs)) |
| `ciba.W001` | in every environment: `CIBA.APPROVE_LOGINS` on with `AUTH.CREDENTIAL_CHANGE_DEVICE_LOGINS = "end"`, so a password change or reset signs the phone out too ([Credential changes and the phone](../auth/logging-in.md#credential-changes-and-the-phone)) |
| `auth.W007` | in every environment: a backend of `AUTH.BACKENDS` other than `ModelBackend` that does not answer `object_grants`. Only superusers then change users and groups through the admin API ([auth.md](../auth/routes.md#the-admin-api)) |
| `auth.W010` | in every environment: `TENANCY.MEMBER_PERMISSIONS` without `MEMBER_TENANTS`. Only superusers then change users through the admin API ([tenancy.md](../tenancy.md#members-permissions)) |
| `architecture.W001` | in every environment: the infrastructure layer imports a name of `domainkit.http.client` or `domainkit.http.uploads` that their `__all__` does not list (Starlette's `UploadFile`, `StreamingResponse`), or `domainkit.http.uploads.serve`, which answers a request. 3.0 refuses it ([Layer rules](settings.md#layer-rules)). New in 2.9 |
| `tenancy.W001` | in every environment: a `Throttle` on a route served as a tenant (a `PathTenant`'s, or any route under `TenantMiddleware` but a tenants' collection) that says neither `per_tenant=True` nor `per_tenant=False`. It counts per tenant, so a caller who belongs to ten tenants gets ten times its rate. Say which ([tenancy.md](../tenancy.md#caches-limits-and-logs)). New in 2.7 |
| `audit.W001` to `audit.W005` | for the parts `AUDIT.REQUIRE` leaves out (all, with `REQUIRE_BASELINE = False`): what `audit.E001` to `audit.E005` refuse |
| `accounting.W001`, `accounting.W002` | with `ACCOUNTING.REQUIRE_PROTECTION = False` only, logged at startup: what `accounting.E001` and `accounting.E002` refuse |
| `accounting.W003` | the ledger without `domainkit.contrib.messaging`: no worker releases expired holds, which keep counting against what is available. Silence it once `manage.py accounting expireholds` runs on the project's own schedule ([accounting.md](../contrib/accounting.md#holds)) |
| `messaging.W001` | a task registered without `name=` (in staging and production): its name is its module's path. Moving or renaming the function renames its messages, which then wait for a task of the old name and are dead-lettered after `MESSAGING.UNKNOWN_TARGET_GRACE` ([messaging.md](../contrib/messaging.md)) |
| `messaging.W002` | a durable handler subscribed without `name=` (in staging and production): its key is its module's path. Moving or renaming the function, or making it in-process, renames its messages, which then wait for a handler of the old name and are dead-lettered after `MESSAGING.UNKNOWN_TARGET_GRACE`. `formerly=` keeps running them under the new name ([messaging.md](../contrib/messaging.md#durable-event-handlers)) |
| `messaging.W003` | a network database whose pool holds fewer connections than `MESSAGING.CONCURRENCY` + 1 (in staging and production): a worker's running messages each hold one, besides its listener's, and the rest wait up to `POOL_TIMEOUT`, then fail and are retried ([operations.md](../operations.md#the-worker)) |
| `messaging.W004` | tasks name a `priority` and `MESSAGING.BROKER` hands out messages in the order they came (in staging and production): Redis Streams, NATS JetStream, Kafka, SQS, or a `BACKEND` whose `priorities` is false. A priority orders only what waits in the outbox ([messaging.md](../contrib/messaging.md#priorities-on-rabbitmq)) |
| `limits.W001` | limits without `domainkit.contrib.messaging`: no worker purges old usage, and its table grows with every movement. Silence it once `manage.py limits purge` runs on the project's own schedule ([limits.md](../contrib/limits.md#settings)) |
| `idempotency.W001` | with `IDEMPOTENCY.REQUIRE_DEADLINE = False` only: what `idempotency.E002` refuses |
| `keys.W001` | from `manage.py keys status --check`, not `check`: values, blind indexes, or subjects' keys are still sealed under a retired key. `manage.py keys reencrypt` moves them; then remove the key ([keys.md](../contrib/keys.md#rotating)) |
| `documents.W001` | a `DOCUMENTS["RENDERER"]` without `USERNAME` and `PASSWORD` (in staging and production): Gotenberg renders, and fetches what a page links, for anyone who reaches it. Start it with `--api-enable-basic-auth`, or silence the warning where only the service reaches it ([documents.md](../documents.md#hardening-gotenberg)). New in 2.5 |

The production settings that `init` writes avoid the first three
warnings, and ask for `TRUSTED_PROXIES`: leave it empty only when callers
connect directly. They count requests, passwords, and codes in Redis,
from `REDIS_URL`.

### Silencing a warning

When your project has judged a warning and accepts the choice, list its
id in `SILENCED_CHECKS`. `check` then counts it as silenced, and
`check --deploy` passes:

```python
# An API that is public on purpose: its documentation may be read.
API_DOCS = True
SILENCED_CHECKS = ["security.W001"]
```

`SILENCED_CHECKS` takes at most 64 ids, each once. An error id is
refused there: each error stands for an unsafe or broken configuration.

`idempotency.W002` (0.10: `IDEMPOTENCY.ANONYMOUS` left unset, so
`"per-address"`) is gone since 0.11, whose default is `"refuse"`. Listed
in `SILENCED_CHECKS`, it silences nothing.

## Information

| Id | Said |
| --- | --- |
| `security.I001` | in staging and production: where a request's scheme comes from ([below](#information-https)) |
| `keys.I001` | in every environment: `KEYS.DATA_KEYS` keeps retired keys, as a rotation does until `keys reencrypt` moved what they seal; `keys status --check` says when they can go ([keys.md](../contrib/keys.md#rotating)) |
| `documents.I001` | in staging and production, with `DOCUMENTS["RENDERER"]` set: Gotenberg's flags are not checked, since they are set where it runs and its API does not show them; the hint names those to set ([documents.md](../documents.md#hardening-gotenberg)). New in 2.5 |

A domain's own infos (`<label>.I001`, from `on_deployment_warnings`) are
listed the same way: never a failure, and nothing to silence.

## Information: HTTPS

In staging and production, `check` also says what it cannot check: HTTPS.
It says so as `security.I001`, which is information: never a failure, and
nothing to silence. The application serves plain HTTP, and TLS ends in
front of it, where no setting reaches.

```text
System check identified no issues and 2 warnings. Domains: messaging, contenttypes, auth.
  Info: security.I001: HTTPS is not checked: the application serves plain HTTP, and TLS must end in front of it. It takes a request's scheme from the X-Forwarded-Proto header of TRUSTED_PROXIES only, so the proxy must set it, over a caller's.
    Hint: Redirect plain HTTP to HTTPS at the proxy, and keep SECURE_HSTS_SECONDS set (docs/security.md, 'The edge').
```

The line names where a request's scheme comes from: the
`PROXY_SCHEME_HEADER` of `TRUSTED_PROXIES`, which the proxy must set over
a caller's, or the connection alone, where every request counts as plain
HTTP ([security.md](../security.md), "The edge";
[Running behind a proxy](../recipes/behind-a-proxy.md)).

## Checks of your own

A domain adds checks in its `ready()` method ([Extension points](extension-points.md)):

- `on_check_application(hook)`: `hook(app)` returns problems of the built
  application, best as `CheckMessage("<label>.E001", ...)`. Staging and
  production refuse to start with any.
- `on_deployment_warnings(hook)`: `hook()` returns warnings, best as
  `CheckMessage("<label>.W001", ...)`, which `SILENCED_CHECKS` can name.

```python
from fastapi import FastAPI

from domainkit.apps import DomainConfig
from domainkit.apps.hooks import on_check_application
from domainkit.checks import CheckMessage


def no_public_writes(app: FastAPI) -> list[str]:
    """The catalog's routes all need a login."""
    return [CheckMessage("catalog.E001", "A catalog route is public.")]


class CatalogConfig(DomainConfig):
    def ready(self) -> None:
        on_check_application(no_public_writes)
```

## See also

- [Commands](commands.md#check-check-the-project): `check` and
  `check --deploy`.
- [Settings](settings.md): `ENVIRONMENT` and `SILENCED_CHECKS`.
- [Deploying](../deploying.md): from a project whose checks pass to one
  in production.
- [Security](../security.md): what each refusal protects.
