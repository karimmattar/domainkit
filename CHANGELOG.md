# Changelog

All notable changes to domainkit. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/); see `docs/versioning.md`.

## [Unreleased]

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
  - `POST /auth/signup`, off until `AUTH.SIGNUP_ENABLED`, checks the fields
    and password rules at once, then answers 202 and mails the address a
    link (`AUTH.SIGNUP_URL`), or word that the username or address is
    taken. `POST /auth/signup/confirm` with the link's token makes the
    account, its address verified: until then no account exists, so no
    one but the address's owner learns whether it has one, and a pending
    signup reserves no username. Pending signups, with the password's
    hash, wait in the new `domainkit_pending_signups` table (run
    `makemigrations` and `migrate`), never in the outbox or in fixtures,
    and expired ones are purged hourly.
  - Requests that send mail count against the new `AUTH.EMAIL_RATE`, and
    token checks against `AUTH.LOGIN_RATE`; resets, verifications,
    signups, and refusals are audited.
- Browser sessions in cookies (DJ-43): `CookieAuthentication` reads an
  opaque token from an HttpOnly, Secure, SameSite cookie that
  `POST /auth/session` sets and `DELETE /auth/session` clears. Unsafe
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
  limited to scopes (permission keys they hold) if they wish. Permission rules allow a scoped token only what both its user and
  its scopes allow; `HasScope` and `IsUnscoped` are new rules, and
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

### Fixed

- Autogenerated revisions keep SQLAlchemy's own `Interval` and `PickleType`
  columns as they are. An `Interval` was written as `DateTime`, a timestamp
  on PostgreSQL instead of an `INTERVAL`.
- `client_for` and `admin_client` work in async tests. They called
  `asyncio.run`, which fails inside a running event loop (N-01).
- Clients from `client_for` and `admin_client` run on the `client`
  fixture's event loop. Each of their requests started an event loop of its
  own, with a database engine nothing closed (ASYNC-24).

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

[Unreleased]: https://github.com/karimmattar/domainkit/compare/v0.6.2...HEAD
[0.6.2]: https://github.com/karimmattar/domainkit/compare/v0.6.1...v0.6.2
[0.6.1]: https://github.com/karimmattar/domainkit/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/karimmattar/domainkit/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/karimmattar/domainkit/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/karimmattar/domainkit/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/karimmattar/domainkit/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/karimmattar/domainkit/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/karimmattar/domainkit/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/karimmattar/domainkit/releases/tag/v0.1.0
