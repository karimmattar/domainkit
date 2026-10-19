# Changelog

All notable changes to domainkit. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/); see `docs/versioning.md`.

## [Unreleased]

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
- `ExactDecimal` (`docs/databases.md`):
  - A value with more places or digits than the column holds is compared
    exactly (`where(price > Decimal("9.995"))`, `in_`, `between`) instead
    of failing the query with a 500, on every database.
  - On SQLite, an amount added to, subtracted from, or compared with an
    amount of another scale, or an integer column, is counted in the same
    units: `price > rate` compared cents with ten-thousandths, and
    `price + quantity` added a count to cents.

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

[Unreleased]: https://github.com/karimmattar/domainkit/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/karimmattar/domainkit/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/karimmattar/domainkit/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/karimmattar/domainkit/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/karimmattar/domainkit/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/karimmattar/domainkit/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/karimmattar/domainkit/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/karimmattar/domainkit/releases/tag/v0.1.0
