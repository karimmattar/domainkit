# Public API

This is the public API of domainkit, by module: every name a module lists
in `__all__`, one line each. A name listed here is covered by the
compatibility policy of [versioning.md](versioning.md): a patch release
never removes or breaks it, and a minor release removes it only after
deprecating it ([ADR 0076](adr/0076-public-api-is-docs-api-md.md)).

Anything else is internal, even when it can be imported: names starting
with `_`, modules without `__all__`, and names a module defines but does
not list. Import public names from the module that lists them here; a name
listed in a package and in one of its modules is the same object.

Besides these names, settings and their defaults ([reference.md](reference.md)),
management commands and their options, the generated domain layout, and
the contrib apps' table schemas are public too.

`domainkit_tests/test_api.py` checks that this page and the code agree:
every name here imports from the module it is listed under, and every name
in an `__all__` is here.

## `domainkit`

domainkit: a Django-style framework for domain-driven FastAPI services.

- `__version__`: The installed distribution's version, as `importlib.metadata` reports it.
- `setup`: Configure logging and load the installed domains (`django.setup()`).

## `domainkit.apps`

Installed domains (`django.apps`): configuration classes and the registry.

- `DomainConfig`: Describe one installed domain and its lifecycle hooks.
- `DomainRegistry`: Installed domains in `INSTALLED_DOMAINS` order.
- `Requirement`: A domain package another needs in `INSTALLED_DOMAINS`, and why.
- `registry`: The process-wide registry populated by `domainkit.setup()`.

## `domainkit.architecture`

Enforce the dependency rules of a domain-driven package layout.

- `DOMAIN_ALLOWED_PREFIXES`: Non-stdlib modules a domain layer may import (besides its own layer).
- `FORBIDDEN_LAYERS`: Layers of the same domain that each layer must not import.
- `FORBIDDEN_LIBRARIES`: Libraries each layer must not import.
- `FRAMEWORK_PACKAGES`: Installed framework apps (`domainkit.contrib.*`) are services, not domains: they are neither checked nor protected by the cross-domain rule.
- `LAYERS`: Layer packages of a domain, innermost first.
- `PUBLIC_SURFACE`: Parts of a domain that other domains may import.
- `LayerRules`: The rules one check applies; the defaults are the tables above.
- `Violation`: One import that breaks a rule.
- `check_domain`: Return the rule violations in domain `package`.
- `check_domains`: Check every domain in `installed`, under `rules` (the project's).
- `iter_imports`: Yield `(module, imported)` for every import in `package`'s files.
- `project_rules`: The rules of the loaded settings' `ARCHITECTURE`, or the defaults.

## `domainkit.broker`

Message brokers: durable work queues for the worker (RabbitMQ, or in memory).

- `Broker`: Durable work queues.
- `BrokerError`: Base class of broker errors.
- `BrokerHandler`: Create each configured broker on first use, per event loop.
- `BrokerRefused`: The broker refused a message (no queue took it, or it said no).
- `BrokerUnavailable`: The broker could not be reached, or did not confirm in time.
- `Delivery`: One message handed to a consumer; `ack` or `requeue` it exactly once.
- `Handler`: A consumer of deliveries: `async def handle(delivery) -> None`.
- `MemoryBroker`: Queues of this process: one consumer gets each message, as in RabbitMQ.
- `Subscription`: Deliveries flowing from queues to a handler.
- `brokers`: The process-wide brokers: `brokers["default"]`.

## `domainkit.cache`

Caches: in process memory, or in Redis or Valkey shared by every replica.

- `DEFAULT_TTL`: Use the cache's `TTL` setting.
- `TTL`: How long an entry lives: seconds or a `timedelta`; None keeps it.
- `Cache`: One configured cache; get it from `domainkit.cache.caches`.
- `CacheError`: Base class of cache errors.
- `CacheHandler`: Create each configured cache on first use, per event loop.
- `CacheUnavailable`: The cache could not be reached in time, or is skipped after a failure.
- `CachedFunction`: An async function whose results are cached by its arguments.
- `DummyStore`: Keep nothing: every read misses.
- `MemoryStore`: Entries in this process, evicted least recently used first.
- `Store`: Bytes under full keys; `ttl` is in milliseconds, None keeps the entry.
- `cache_page`: Cache what an async GET endpoint returns, for `ttl`, in `cache`.
- `cached`: Cache an async function's results, keyed by its arguments.
- `caches`: The process-wide caches: `caches["default"]`.
- `invalidate_on`: Delete the entries `keys(event)` names after each `event_type` event.

## `domainkit.clock`

The framework's clock: the one place domainkit reads the time.

- `FrozenClock`: A clock that stands at a moment, or ticks on from it, until moved.
- `Moment`: A time to freeze at: an ISO 8601 string, a date (its midnight), or a datetime.
- `freeze_time`: Return a clock frozen at `when` (default: now), for a `with` block.
- `frozen`: Return the frozen clock in use in this context, or None for real time.
- `moment`: Return `when` as an aware datetime in UTC; None means `now()`.
- `monotonic`: Return seconds from an arbitrary start, for intervals (`time.monotonic`).
- `now`: Return the current time, aware and in UTC (the frozen clock's, if any).
- `today`: Return today's date in UTC (the frozen clock's, if any).

## `domainkit.conf`

Django-style settings access.

- `DEFAULT_CACHE_ALIAS`: The alias of the cache used when none is named: `"default"`.
- `DEFAULT_DB_ALIAS`: The alias of the database used when none is named: `"default"`.
- `SETTINGS_MODULE_VARIABLE`: The environment variable naming the settings module, after the project's prefix.
- `UNSET`: Pass to `override_settings` to remove a setting (Django's `del settings.X`).
- `BrokerConfig`: One `BROKERS` entry: a message broker the worker takes its work from.
- `CacheConfig`: One `CACHES` entry: where cached values live, and for how long.
- `DatabaseConfig`: One `DATABASES` entry: an SQLAlchemy async URL plus connection options.
- `Env`: Read prefixed variables and convert them, failing with the variable name.
- `LedgerConfig`: One `LEDGERS` entry: write once, read many, verified on the client.
- `Settings`: Validated, read-only settings; extra names are checked at `setup()`.
- `ShardConfig`: One `SHARDS` group: SQL databases that share a schema, split by key.
- `StorageConfig`: One `STORAGES` entry: a `Storage` class and its arguments (Django's).
- `configure`: Load `module_name` now instead of `<PREFIX>SETTINGS_MODULE`.
- `describe_errors`: List failing setting paths and reasons, never the submitted values.
- `load_settings`: Validate every public UPPERCASE name of `module_name`.
- `override_settings`: Temporarily replace settings, re-running schema validation.
- `reset_settings`: Forget loaded settings; the next access loads them again.
- `settings`: Typed, lazily loaded settings for the selected module.
- `unwrap_settings`: Return the loaded `Settings` object itself (loading it if needed).
- `use_settings`: Temporarily load another settings module, then restore the current one.

## `domainkit.contrib.admin`

A generic admin over REST: Django's `admin.site.register`, without HTML.

- `AdminCommand`: A command the admin runs on a model, through its application service.
- `AdminMeta`: What the admin serves, for a generic client.
- `AdminQuery`: A query the admin answers, through its handler (`query_route`).
- `AdminSite`: Registered models, served under one prefix with `GET /meta`.
- `ModelAdmin`: One registered model: how the admin lists, reads, and changes it.
- `command`: An admin action: `command` run by `service` (see `command_route`).
- `query`: An admin query: `query` answered by `handler` (see `query_route`).
- `register`: Serve `model` in the admin; see the package for the options.
- `site`: The default site; `register` registers with it.

## `domainkit.contrib.audit`

An append-only audit trail: who did what, when, from where.

- `AuditSettings`: What to record; every key is optional.
- `Outcome`: Result of an audited action.
- `action_for`: Name an event's action `<domain label>.<event in snake case>`.
- `forget`: Delete the keys of `subjects` in every audited database.
- `forget_anonymous`: Delete the keys of anonymous callers' days before `before`.
- `pseudonym_of`: The pseudonym `value` of `field` has in the trail for `subject`.
- `record`: Record an action in its own transaction (denials, failures, reads).
- `seal`: Seal the rows of `database`'s trail not sealed yet; count them.
- `seal_all`: Seal, then anchor, every audited database; what each one got.
- `verify`: Check `database`'s chain against its rows, and against `ledger`.

## `domainkit.contrib.audit.api`

Records' history over HTTP, for staff (Django admin's "History").

- `router`: The routes: records' history over HTTP, for staff (Django admin's "History").

## `domainkit.contrib.audit.history`

A record's history: the audit rows about one target or one aggregate.

- `history`: The newest `limit` entries about `target`, or about `aggregate`.

## `domainkit.contrib.audit.protection`

Make the audit trail append-only in the database itself (PostgreSQL).

- `protect`: Refuse UPDATE, DELETE, and TRUNCATE on `table` (the audit trail).
- `unprotect`: Remove the protection (downgrades, retention jobs run as the owner).

## `domainkit.contrib.audit.tables`

The audit trail: append-only, one row per recorded action, sealed in a chain.

- `audit_seals`: Each row's place in the hash chain (see `seals`); append-only too.

## `domainkit.contrib.auth`

Users, groups, and permissions for REST APIs (Django's `contrib.auth`).

- `AbstractBaseUser`: Credentials, status, groups, and permissions of a user.
- `AbstractUser`: A user with a username, an email, names, and a staff flag (Django's).
- `AnonymousUser`: Whoever sends a request without credentials: no id, no permissions.
- `AnyUser`: A user that logged in, or the anonymous one.
- `AuthSettings`: Users, hashing, validation, and backends; every key is optional.
- `AuthToken`: A stored token: its digest (the `id`), owner, kind, and lifetime.
- `AuthUnitOfWork`: One transaction on the auth tables, wherever `auth` is routed.
- `AuthenticatedUser`: The request's user; anonymous callers get 401.
- `AuthenticationFailed`: Credentials were presented and do not hold (401, unless a subclass says).
- `BaseAuthentication`: Recognizes no credentials and issues no tokens; override what you need.
- `BaseBackend`: A backend that knows no one and grants nothing; override what you need.
- `Credentials`: Who sent a request, and with what (`request.auth`).
- `CurrentUser`: The request's user, or `AnonymousUser`: the core's dependency, typed so.
- `Group`: A named set of permissions; every member has them all.
- `GroupId`: NewType creates simple unique types with almost zero runtime overhead.
- `GroupNameTaken`: Another group has that name.
- `GroupNotFound`: No group has that id.
- `InvalidEmail`: The email address is not one plain address mail can go to.
- `InvalidGroupName`: The group name is empty or too long.
- `InvalidPassword`: The password breaks the configured rules (`PASSWORD_VALIDATORS`).
- `InvalidUserField`: A value does not fit the type of the user field it is for.
- `InvalidUsername`: The username is empty, too long, or uses characters it may not.
- `IsAdminUser`: Staff users (`is_staff`), with credentials that are not limited to scopes.
- `IsSuperuser`: Active superusers, with credentials that are not limited to scopes.
- `IssuedTokens`: What logging in or refreshing returns (OAuth 2's token response).
- `JWTAuthentication`: Signed access tokens (JSON Web Tokens) and opaque refresh tokens.
- `JWTSettings`: `AUTH["JWT"]`: signed access tokens (`JWTAuthentication`).
- `ModelBackend`: Passwords from the user table; permissions of users and their groups.
- `PasswordHash`: A stored password: a hasher's `<algorithm>$...`, or unusable (`!...`).
- `Permission`: Permission to do something to one content type: `<domain>.<codename>`.
- `TokenAuthentication`: Opaque tokens: `Authorization: Bearer <AUTH.TOKEN_PREFIX>...`.
- `TokenKind`: What a token may be used for.
- `UnknownGroup`: No group has that name.
- `UnknownPermission`: No permission has that key (`<domain>.<codename>`).
- `UnknownUserField`: The user model has no such field, or it changes another way.
- `User`: The default user; `AUTH.USER_MODEL` swaps in a project's own.
- `UserId`: NewType creates simple unique types with almost zero runtime overhead.
- `UserNotFound`: No user has that id.
- `UsernameTaken`: Another user has that username.
- `acheck_password`: `check_password` on a hashing thread; raises `PasswordChecksBusy`.
- `acurrent_user`: The core's `acurrent_user`, typed as this app's users: the same function.
- `amake_password`: `make_password` on a hashing thread; raises `PasswordChecksBusy`.
- `authenticate`: The user the credentials prove, by the first backend that accepts them.
- `check_password`: Whether `password` matches `encoded`.
- `current_user`: The core's `current_user`, typed as this app's users: the same function.
- `get_all_permissions`: Keys of every permission any backend grants `user` (on `obj`).
- `get_backends`: The configured backends, in order.
- `get_user`: The user with `user_id`, from the first backend that has it.
- `get_user_model`: The user class (Django's `get_user_model`).
- `has_perm`: Whether `user` has `perm` (on `obj`).
- `has_perms`: Whether `user` has every one of `perms` (on `obj`).
- `make_password`: Hash `password` with `hasher` (default: the preferred one).
- `password_help_texts`: What each configured rule asks of a password, for API clients.
- `validate_password`: Raise `InvalidPassword` listing every rule `password` breaks.

## `domainkit.contrib.auth.admin`

The admin API: users, groups, and permissions over REST (Django's admin).

- `router`: The routes: the admin API: users, groups, and permissions over REST (Django's admin).

## `domainkit.contrib.auth.api`

Endpoints to log in and out, refresh tokens, and change passwords.

- `password_holds`: Whether `password` is the stored password of `user`.
- `router`: The routes: endpoints to log in and out, refresh tokens, and change passwords.

## `domainkit.contrib.auth.authentication`

How requests prove who sent them (Django REST framework's authentication).

- `AuthenticationFailed`: Credentials were presented and do not hold (401, unless a subclass says).
- `BaseAuthentication`: Recognizes no credentials and issues no tokens; override what you need.
- `Credentials`: Who sent a request, and with what (`request.auth`).
- `JWTAuthentication`: Signed access tokens (JSON Web Tokens) and opaque refresh tokens.
- `TokenAuthentication`: Opaque tokens: `Authorization: Bearer <AUTH.TOKEN_PREFIX>...`.
- `active_user`: The active user `user_id`, loaded with groups and permissions.

## `domainkit.contrib.auth.backends`

Who a user is, and what they may do (Django's authentication backends).

- `BaseBackend`: A backend that knows no one and grants nothing; override what you need.
- `ModelBackend`: Passwords from the user table; permissions of users and their groups.
- `authenticate`: The user the credentials prove, by the first backend that accepts them.
- `get_all_permissions`: Keys of every permission any backend grants `user` (on `obj`).
- `get_user`: The user with `user_id`, from the first backend that has it.
- `has_perm`: Whether `user` has `perm` (on `obj`).
- `has_perms`: Whether `user` has every one of `perms` (on `obj`).

## `domainkit.contrib.auth.certificates`

Client certificates (mutual TLS): a certificate someone checked names its user.

- `ClientCertificateAuthentication`: Users named by a client certificate the server or a trusted proxy checked.

## `domainkit.contrib.auth.cookies`

Browser sessions: an opaque token in an HttpOnly cookie, guarded against CSRF.

- `CookieAuthentication`: Browser sessions: the session cookie, checked against CSRF.
- `router`: The routes: browser sessions: an opaque token in an HttpOnly cookie, guarded against CSRF.

## `domainkit.contrib.auth.hashers`

Password hashing (Django's `hashers`, with the same stored formats).

- `Argon2PasswordHasher`: Argon2id (RFC 9106), the Password Hashing Competition's winner.
- `FastPBKDF2PasswordHasher`: One PBKDF2 iteration: for tests only, where hashing time adds up.
- `PBKDF2PasswordHasher`: PBKDF2 with HMAC-SHA256 (the default; NIST SP 800-132), from the stdlib.
- `PBKDF2SHA1PasswordHasher`: PBKDF2 with HMAC-SHA1, to check hashes moved from older systems.
- `PasswordHasher`: One hashing algorithm; `algorithm` starts every hash it makes.
- `ScryptPasswordHasher`: scrypt (RFC 7914): memory-hard, from the stdlib.

## `domainkit.contrib.auth.mfa`

Logins with a second factor: an authenticator app (TOTP), and recovery codes.

- `router`: The routes: logins with a second factor: an authenticator app (TOTP), and recovery codes.

## `domainkit.contrib.auth.middleware`

`AuthenticationMiddleware`: who sent each request, before anything counts it.

- `AuthenticationMiddleware`: Set `scope["user"]`, `scope["auth"]`, and the request's actor.

## `domainkit.contrib.auth.models`

Users, groups, and permissions: the domain model of `domainkit.contrib.auth`.

- `AbstractBaseUser`: Credentials, status, groups, and permissions of a user.
- `AbstractUser`: A user with a username, an email, names, and a staff flag (Django's).
- `AnonymousUser`: Whoever sends a request without credentials: no id, no permissions.
- `AnyUser`: A user that logged in, or the anonymous one.
- `Group`: A named set of permissions; every member has them all.
- `GroupId`: NewType creates simple unique types with almost zero runtime overhead.
- `PasswordHash`: A stored password: a hasher's `<algorithm>$...`, or unusable (`!...`).
- `Permission`: Permission to do something to one content type: `<domain>.<codename>`.
- `User`: The default user; `AUTH.USER_MODEL` swaps in a project's own.
- `UserId`: NewType creates simple unique types with almost zero runtime overhead.

## `domainkit.contrib.auth.personal_tokens`

Personal tokens: users list, create, and revoke their own.

- `router`: The routes: personal tokens: users list, create, and revoke their own.

## `domainkit.contrib.auth.principals`

Principals: who credentials name, kept between requests (ADR 0058).

- `invalidate`: Drop the principals that rest on `targets` in this process; how many.
- `invalidate_all`: Drop every principal of this process; how many.
- `invalidate_everywhere`: Drop `targets` here, and stamp them for every other process.

## `domainkit.contrib.auth.recovery`

Password resets and email verification, by links sent by email.

- `router`: The routes: password resets and email verification, by links sent by email.

## `domainkit.contrib.auth.signup`

Signing up: anyone may ask for an account, if `AUTH.SIGNUP_ENABLED` says so.

- `router`: The routes: signing up: anyone may ask for an account, if `AUTH.SIGNUP_ENABLED` says so.

## `domainkit.contrib.auth.validators`

Rules for new passwords (Django's `password_validation`).

- `CommonPasswordValidator`: Not on a list of common passwords (default: Django's 20,000).
- `MinimumLengthValidator`: At least `min_length` characters (default 8).
- `NumericPasswordValidator`: Not only digits.
- `UserAttributeSimilarityValidator`: Not too much like the user's username, names, or email.

## `domainkit.contrib.contenttypes`

Content types: one row per kind of stored object (Django's `contenttypes`).

- `DEFAULT_PERMISSIONS`: Actions that get a permission for every content type, unless `Meta` says.
- `ContentType`: A kind of stored object: its domain label and lower-case class name.
- `InstalledModel`: A mapped class of an installed domain, and what it declares.
- `ModelOptions`: What a mapped class declares about itself (Django's `Meta`).
- `content_type_key`: The `(domain, model)` of a mapped class.
- `installed_models`: Every class mapped onto a table that an installed domain holds now.
- `model_options`: Read `cls.Meta`: `verbose_name`, `default_permissions`, `permissions`.
- `sync_content_types`: Insert a row for every installed model; return ids by `(domain, model)`.

## `domainkit.contrib.evidence`

Evidence: facts kept on a ledger, provably unchanged (see ADR 0011).

- `Evidence`: What happened, about whom, and the digests that back it up.
- `EvidenceConflict`: A different record already uses that evidence id.
- `EvidenceNotFound`: No evidence is stored under that key.
- `EvidenceReceipt`: Proof of where a record is and what it says; keep it with your data.
- `EvidenceSettings`: Where evidence is kept, how large a record may be, how keys hide subjects.
- `EvidenceStore`: Record evidence once; read it back verified (the application's port).
- `EvidenceTampered`: The ledger copy does not match its receipt, or was written again.
- `LedgerEvidenceStore`: Evidence on a `Ledger`: immudb in deployments, `MemoryLedger` in tests.
- `RecordedEvidence`: Evidence read back from the ledger, with its receipt.
- `evidence_key`: `<namespace>/<subject digest>/<evidence id>`.
- `evidence_store`: The configured store, on the ledger `EVIDENCE.LEDGER` names.
- `sha256`: `sha256:<hex>` of `data`: how evidence refers to artifacts and records.
- `subject_digest`: The key part for `subject`, so keys never show it.

## `domainkit.contrib.flags`

Feature flags and dynamic settings, stored in the database and cached.

- `DynamicSetting`: The stored value of a dynamic setting; `id` is its key.
- `DynamicSettingDefinition`: A setting changed at runtime, declared with `dynamic_setting`.
- `Flag`: A feature switched on for everyone, no one, or some users; `id` is its name.
- `FlagChanged`: A flag was created, or its rules changed.
- `FlagDeleted`: A flag was deleted; it is off for everyone again.
- `FlagNotFound`: No flag has that name.
- `FlagsSettings`: Where flags are cached, and for how long; every key is optional.
- `FlagsUnitOfWork`: One transaction on the flags tables, wherever `flags` is routed.
- `InvalidFlag`: A flag's rules are out of range.
- `InvalidSettingValue`: The value does not fit the dynamic setting's type.
- `RequireFlag`: A FastAPI dependency: 404 unless flag `name` is on for the caller.
- `SettingChanged`: A dynamic setting got a new value (JSON).
- `SettingReset`: A dynamic setting went back to its default.
- `UnknownSetting`: No dynamic setting is declared with that key.
- `declared_settings`: Every declared dynamic setting, by key.
- `dynamic_setting`: Declare a setting staff can change at runtime, typed and validated.
- `flag_enabled`: Whether flag `name` is on for `user` (anyone without one).

## `domainkit.contrib.flags.admin`

Flags and dynamic settings over REST, for staff.

- `router`: The routes: flags and dynamic settings over REST, for staff.

## `domainkit.contrib.idempotency`

Replay-safe unsafe requests with the `Idempotency-Key` header.

- `IdempotencySettings`: How long keys live and which requests may carry one.

## `domainkit.contrib.idempotency.middleware`

`Idempotency-Key`: run a POST once, replay its response on retries.

- `IdempotencyMiddleware`: Store and replay responses of requests carrying `Idempotency-Key`.

## `domainkit.contrib.mail`

Email through the outbox (Django's `django.core.mail`).

- `BaseEmailBackend`: Sends messages somewhere; subclasses say where.
- `ConsoleBackend`: Writes each message to standard output, for development.
- `EmailMessage`: An email: a plain-text body, and an HTML one if it has one.
- `MailDeliveryFailed`: The server did not take a message; the worker tries again later.
- `MailSettings`: Where mail goes, who sends it, and how often sending is retried.
- `MemoryBackend`: Keeps messages in `outbox`, for tests: nothing leaves the process.
- `SmtpBackend`: Hands messages to an SMTP server (`MAIL.SMTP`) on a worker thread.
- `SmtpSettings`: `MAIL["SMTP"]`: the server `SmtpBackend` hands messages to.
- `TemplateDoesNotExist`: No directory holds the template.
- `TemplateError`: A template cannot be rendered: bad syntax, or a value it lacks.
- `deliver`: Send `messages` now, through the backend; return how many were sent.
- `get_backend`: A new instance of `MAIL.BACKEND`.
- `outbox`: Messages `MemoryBackend` sent, oldest first (Django's `mail.outbox`).
- `render_mail`: A message from the template `template`, for `to`.
- `send`: Queue `message`; return it with its sender and id.
- `send_mail`: Queue one message to `recipient_list` (Django's `send_mail`).

## `domainkit.contrib.mail.backends`

Where mail goes: an SMTP server, the console, or a list for tests.

- `BaseEmailBackend`: Sends messages somewhere; subclasses say where.
- `ConsoleBackend`: Writes each message to standard output, for development.
- `MailDeliveryFailed`: The server did not take a message; the worker tries again later.
- `MemoryBackend`: Keeps messages in `outbox`, for tests: nothing leaves the process.
- `SmtpBackend`: Hands messages to an SMTP server (`MAIL.SMTP`) on a worker thread.
- `outbox`: Messages `MemoryBackend` sent, oldest first (Django's `mail.outbox`).

## `domainkit.contrib.messaging`

Transactional outbox, durable event handlers, and background tasks.

- `Attempt`: The run a worker is making of a message, as its task sees it.
- `Backoff`: Seconds to wait before running a failed message again.
- `Budget`: Slots for the messages a worker process runs at once.
- `Chain`: Tasks that run one after another, each once the one before succeeds.
- `MessagingSettings`: Delivery, retry, and worker options; every key is optional.
- `Worker`: Run outbox messages of one database.
- `chain`: Run `first`, then each task of `then` once the one before succeeds.
- `current_attempt`: The run of the message being processed, or None outside the worker.
- `enqueue`: Queue `function(**kwargs)` now, in its own transaction.
- `periodic`: Register an argument-free task that runs on a schedule, cluster-wide.
- `task`: Register an async function as a background task (`uow.defer(task)`).

## `domainkit.contrib.messaging.admin`

Operator queries: queue depth, lag, listings, retries, and cancelling.

- `blanked_dead_letters`: Count dead messages (one, or all) whose arguments were blanked.
- `cancel`: Cancel a pending message that no worker is running; report whether it was.
- `list_messages`: List messages in `state`, most urgent (or, for dead ones, newest) first.
- `queue_counts`: Count messages by status in alias `database` (default: the home outbox).
- `queue_lag`: Seconds the oldest due message of each queue has waited to start.
- `retry_dead`: Return dead messages (one, or all) to the queue with fresh attempts.

## `domainkit.contrib.messaging.dbclock`

The clock that decides when outbox work is due and when locks expire.

- `Moment`: A time to compare or store: a value, or the database's clock in SQL.
- `at`: `seconds` from now (earlier when negative), on the database's clock.
- `now`: The current time as a database of `dialect` should see it.
- `uses_database`: Report whether the database's clock decides on `dialect` right now.

## `domainkit.contrib.messaging.testing`

pytest fixtures for code that publishes events and queues background work.

- `CapturedEvents`: Domain events published after commits, in order.
- `DeferredExecutor`: Runs committed messages right after each commit, in the committing loop.
- `Drained`: What one `drain` ran.
- `Outbox`: Messages committed during a test, and a way to run them.
- `QueuedMessage`: A message committed to an outbox during a test.
- `QueuedWorkFailed`: Queued work failed, or kept queueing more, while a test ran it.
- `drain`: Run every due message of every outbox here, until none is left.

## `domainkit.contrib.notifications`

Text messages and push notifications through the outbox.

- `ConsolePushBackend`: Writes push notifications to standard output, for development.
- `ConsoleSmsBackend`: Writes text messages to standard output, for development.
- `MemoryPushBackend`: Keeps notifications in `push_outbox`, for tests.
- `MemorySmsBackend`: Keeps text messages in `sms_outbox`, for tests.
- `NotificationFailed`: The provider did not take a message; the worker tries again later.
- `NotificationsSettings`: Where text messages and push notifications go, and how often to retry.
- `PushBackend`: Sends push notifications through a provider.
- `PushMessage`: A push notification to one device, by the token its platform gave it.
- `SmsBackend`: Sends text messages through a provider.
- `SmsMessage`: A text message to a phone number in E.164 form (`+33612345678`).
- `deliver_push`: Send push notifications now, skipping the outbox (from the worker).
- `deliver_sms`: Send text messages now, skipping the outbox (from the worker).
- `get_push_backend`: A new instance of `NOTIFICATIONS.PUSH_BACKEND`.
- `get_sms_backend`: A new instance of `NOTIFICATIONS.SMS_BACKEND`.
- `push_outbox`: Notifications `MemoryPushBackend` sent, oldest first.
- `send_push`: Queue a push notification to the device `token` names.
- `send_sms`: Queue a text message to `to` (E.164: `+33612345678`).
- `sms_outbox`: Text messages `MemorySmsBackend` sent, oldest first.

## `domainkit.contrib.notifications.backends`

Ports for text messages and push notifications, with development adapters.

- `ConsolePushBackend`: Writes push notifications to standard output, for development.
- `ConsoleSmsBackend`: Writes text messages to standard output, for development.
- `MemoryPushBackend`: Keeps notifications in `push_outbox`, for tests.
- `MemorySmsBackend`: Keeps text messages in `sms_outbox`, for tests.
- `NotificationFailed`: The provider did not take a message; the worker tries again later.
- `PushBackend`: Sends push notifications through a provider.
- `SmsBackend`: Sends text messages through a provider.
- `push_outbox`: Notifications `MemoryPushBackend` sent, oldest first.
- `sms_outbox`: Text messages `MemorySmsBackend` sent, oldest first.

## `domainkit.contrib.objectpermissions`

Permissions on single objects (django-guardian's grants), for users or groups.

- `Grants`: What `VisibleTo` reads in memory (`InMemoryObjectPermissions`).
- `InMemoryObjectPermissions`: Grants in a dictionary, for tests; `VisibleTo(grants=...)` reads it.
- `InvalidGrant`: A grant names a permission of another model, or no one or two at once.
- `ObjectPermission`: `permission` on one object, for one user or one group.
- `ObjectPermissionBackend`: Permissions granted on an object to a user or to one of their groups.
- `ObjectPermissionGranted`: A user or a group may now do something to one object.
- `ObjectPermissionId`: NewType creates simple unique types with almost zero runtime overhead.
- `ObjectPermissionRepository`: Grants: find them with `about(...)`.
- `ObjectPermissionRevoked`: A grant on one object was taken back.
- `ObjectPermissionsUnitOfWork`: One transaction on the grants of one database (the objects').
- `VisibleTo`: Objects on which `permission` is granted to a user or their groups.
- `grant_permission`: Let `user` (or the members of `group`) have `permission` on `obj`.
- `grants_on`: Every grant on `obj`; who may see them is the caller's to check.
- `revoke_permission`: Take back `permission` on `obj` from `user` or `group`; how many.
- `visible_scope`: A resource's `scope`: what the caller may see by `permission`.
- `visible_to`: What `user` may see of `model`: None (everything), or `VisibleTo`.

## `domainkit.contrib.privacy`

Privacy requests and retention across installed domains (GDPR's rights).

- `PersonalDataErased`: An aggregate's personal fields were erased, at a request or by retention.
- `PersonalModel`: An aggregate of an installed domain with fields marked `Personal`.
- `Policy`: A domain's `Retention`, resolved to a table and its dating column.
- `Purged`: What `purge` did to one table on one database.
- `Retention`: How long rows of one table, or one aggregate's personal data, are kept.
- `check_reason`: Return `reason` if it can go into the audit trail: one line, 1 to 255.
- `check_subject`: Return `subject` if it can name someone, written as its id prints.
- `erase_subject`: Erase every field marked `Personal` about `subject`; return how many changed.
- `export_subject`: Every field marked `Personal` about `subject`, as JSON-ready data.
- `personal_models`: Every installed aggregate with personal fields.
- `problems`: What keeps privacy requests or retention from working, as sentences.
- `purge`: Apply every domain's `RETENTION`, on every database it uses.
- `retention_policies`: Every installed domain's `RETENTION`, resolved.

## `domainkit.contrib.webhooks`

Outgoing webhooks: committed events, signed and delivered to subscribed URLs.

- `InvalidSignature`: A delivery that is not signed with the secret, or is stale.
- `UnsafeTarget`: A webhook URL that may not be called; the message never quotes it.
- `WebhookSettings`: Where deliveries run, how often they are tried, and what they may reach.
- `expose`: Send committed `event_type` events to the subscribers of `name`.
- `new_secret`: Return a new random secret, `whsec_` and 32 bytes in base64.
- `queue_deliveries`: Queue a delivery of one event to each subscriber; return how many.
- `sign`: Return `v1,<signature>` of `body` sent as `message_id` at `timestamp`.
- `verify`: Check a delivery as a receiver would; return its id.

## `domainkit.contrib.webhooks.admin`

The webhooks admin API: subscriptions and their deliveries, for staff.

- `router`: The routes: the webhooks admin API: subscriptions and their deliveries, for staff.

## `domainkit.contrib.webhooks.guard`

Which webhook URLs may be called: the guard against server-side request forgery.

- `MAX_URL_LENGTH`: The longest URL a guarded call accepts, in characters.
- `METADATA_ADDRESSES`: Build an immutable unordered collection of unique elements.
- `METADATA_HOSTS`: Build an immutable unordered collection of unique elements.
- `NAT64`: This class represents and manipulates 128-bit IPv6 networks.
- `Address`: An IPv4 or IPv6 address.
- `Network`: An IPv4 or IPv6 network.
- `UnsafeTarget`: A webhook URL that may not be called; the message never quotes it.
- `checked_address`: The address to connect to for `url`, once all of its host's pass.
- `checked_url`: Parse `url` and check everything that does not need a lookup.
- `judged`: The address whose kind decides: an IPv4 address inside an IPv6 one.
- `lookup`: Every address `host` resolves to, on the guard's threads (`resolver`).
- `refusal`: Why `address` may not be called, or None if it may.

## `domainkit.contrib.webhooks.signing`

Signatures in the Standard Webhooks scheme (https://www.standardwebhooks.com).

- `InvalidSignature`: A delivery that is not signed with the secret, or is stale.
- `sign`: Return `v1,<signature>` of `body` sent as `message_id` at `timestamp`.
- `verify`: Check a delivery as a receiver would; return its id.

## `domainkit.db`

SQLAlchemy integration: connections, ORM metadata, repositories, migrations.

- `ConnectionHandler`: Lazily created engines and session factories per alias and event loop.
- `EnumValue`: An `Enum` stored by its members' values, so order follows them.
- `ExactDecimal`: Fixed-point amounts that come back exactly as stored, also on SQLite.
- `Reads`: How a read-only session reads (`SqlAlchemyUnitOfWork(reads=...)`).
- `SqlAlchemyRepository`: Store one imperatively mapped aggregate type; set `model` in subclasses.
- `SqlAlchemyUnitOfWork`: Open a session on enter, commit on `commit()`, close on exit.
- `UTCDateTime`: Timezone-aware UTC timestamps, also on SQLite (which has no time zones).
- `ValueObjectType`: A single-field value object, stored as its field (`ProductName.value`).
- `child`: A relationship to the one child entity an aggregate owns, or None.
- `children`: A relationship to the child entities an aggregate owns (`list[Line]`).
- `connections`: Process-wide connection handler.
- `create_engine`: Build a pooled engine; bound values stay out of logs and errors by default.
- `map_imperatively`: Map `class_` to `table` once; later calls are ignored.
- `mapper_registry`: Generalized registry for mapping classes.
- `metadata`: Every table of every domain; the target of `makemigrations`.
- `register_column_type`: Store fields of `cls` (and its subclasses) in `column_type`.
- `table_for`: The table that stores `cls`: one column per field (see the module).

## `domainkit.db.models`

Installed models: the mapped classes of installed domains, and their `Meta`.

- `DEFAULT_PERMISSIONS`: Actions that get a permission for every model, unless `Meta` says.
- `InstalledModel`: A mapped class of an installed domain, and what it declares.
- `ModelOptions`: What a mapped class declares about itself (Django's `Meta`).
- `installed_models`: Every class mapped onto a table that an installed domain holds now.
- `model_key`: The `(domain, model)` of a mapped class.
- `model_options`: Read `cls.Meta`: `verbose_name`, `default_permissions`, `permissions`.

## `domainkit.db.partitioning`

Range partitioning of large PostgreSQL tables, managed by the framework.

- `Interval`: The length of one partition.
- `partition_by_range`: Declare `table` range-partitioned by the timestamp `column`.

## `domainkit.db.query`

Compile domain queries (`domainkit.ddd.query`) to SQLAlchemy expressions.

- `register_compiler`: Compile criteria of `kind` (a `Criteria` of your own) with `compiler`.

## `domainkit.db.routing`

Where each domain's data lives: routing targets, aliases, and shards.

- `bucket_for`: The bucket of `key` among `buckets`; stable across processes and hosts.
- `for_each_shard`: Run `function(alias)` on every database of `group` at once.
- `router`: The process-wide router; it reads settings on every call.

## `domainkit.ddd`

Domain-driven design building blocks ("seedwork").

- `UNSET`: A field left out of a partial change (`PatchModel`, `UpdateX` commands).
- `AggregateRoot`: Entry point to a consistency boundary; records events as it changes.
- `BusinessRuleViolation`: The request is well-formed but breaks an invariant of the model.
- `Command`: Request to change state, named in the imperative.
- `ConcurrentUpdate`: Someone else changed the aggregate first; reload it and try again.
- `Conflict`: The operation clashes with the current state, such as a duplicate.
- `Counted`: `Annotated` metadata: the audit trail keeps how many items, not which.
- `Criteria`: A condition on aggregates; combine them with `&`, `|`, and `~`.
- `DeferredTask`: Background work queued by `UnitOfWork.defer`.
- `Digits`: `Annotated` metadata: `precision` digits, `scale` of them decimal.
- `DomainError`: A business rule refused the operation; the message is safe to show.
- `DomainEvent`: Something that happened in the domain, named in the past tense.
- `DuplicateValue`: A value that must be unique is taken (a unique constraint refused it).
- `Entity`: Object with a lifecycle, equal to another only when the ids match.
- `EventBus`: Deliver events to handlers subscribed to their class or a base class.
- `EventHandler`: An event handler: `async def handle(event) -> None`.
- `F`: A field of an aggregate, by name, to build conditions and orderings.
- `HandlerTimedOut`: An in-process handler ran past its timeout and was cut off.
- `InMemoryRepository`: Dictionary-backed repository for unit tests and prototypes.
- `InvalidQuery`: A query names a field, lookup, or value its aggregate cannot take.
- `LockNotAvailable`: Another transaction holds the row's lock, and the caller would not wait.
- `MaxLength`: `Annotated` metadata: text of at most `length` characters.
- `NotFound`: The requested aggregate does not exist.
- `Ordering`: Sort by one field of the aggregate itself.
- `Page`: Up to `limit` items and whether more follow the last one.
- `PermissionDenied`: The actor may not perform the operation.
- `Personal`: `Annotated` metadata: the field holds personal data about someone.
- `PreconditionFailed`: The caller expected another version of the aggregate than the stored one.
- `Query`: Request for data that changes nothing.
- `ReadOnlyError`: A read-only unit of work was asked to commit.
- `Reference`: The id of another aggregate, which is referenced by id only (`Reference[XId]`).
- `Repository`: Load and store aggregates of one type; subclasses implement `_` methods.
- `RowLock`: Lock the rows a query returns until the transaction ends (`FOR UPDATE`).
- `RowLocks`: Row locks for in-memory repositories, held until the unit of work ends.
- `Selection`: Which aggregates to load, in which order, and how many.
- `Sensitive`: A type whose values no schema shows or takes, such as a password hash.
- `SoftDeletable`: Mixin for aggregates that are marked deleted rather than removed.
- `Specification`: A named business rule about aggregates, for Python and for queries.
- `UnitOfWork`: Transaction boundary; subclasses expose repositories as attributes.
- `UnknownField`: A query names a field the class does not have.
- `Unset`: The type of `UNSET`: a field the request left out, as opposed to None.
- `ValueObject`: Immutable value defined only by its attributes; equal values are equal.
- `event_bus`: Process-wide bus used by units of work unless they are given another.

## `domainkit.ddd.preconditions`

Expected versions: change an aggregate only if it is still as the caller saw it.

- `expect_version`: Expect aggregate `id_` of `model` at one of `versions` in the block.

## `domainkit.ddd.query`

Queries over aggregates: criteria, ordering, and specifications.

- `Criteria`: A condition on aggregates; combine them with `&`, `|`, and `~`.
- `F`: A field of an aggregate, by name, to build conditions and orderings.
- `Ordering`: Sort by one field of the aggregate itself.
- `Query`: Which aggregates to load, in which order, and how many.
- `RowLock`: Lock the rows a query returns until the transaction ends (`FOR UPDATE`).
- `Selection`: Which aggregates to load, in which order, and how many.
- `Specification`: A named business rule about aggregates, for Python and for queries.

## `domainkit.deadlines`

Deadlines: how long the work of a request, or of a block, may take.

- `Deadline`: When the work of a request, or of a `timeout` block, must be done.
- `DeadlineExceeded`: The deadline ran out: the request's, or that of a `timeout` block.
- `Timeout`: A block bounded by its seconds and the deadline (see `timeout`).
- `current_deadline`: Return the deadline of the work being done, or None outside one.
- `enforced`: Whether a timer cuts the running task off when its deadline passes.
- `remaining`: Return the seconds left before the deadline; None without one.
- `timeout`: Bound an `async with` block by `seconds` and by the deadline.
- `within`: Await `awaitable`, bounded by `seconds` and by the deadline.

## `domainkit.debug`

Development aids: SQL per request, likely N+1 queries, and profiles.

- `DebugToolbarMiddleware`: Time the SQL of each request; warn about likely N+1 queries.
- `QueryLog`: The statements run while it was recording.
- `record_queries`: Record the statements run in this context (and tasks it starts).
- `statement_shape`: `statement` without its values: literals and placeholders become `?`.

## `domainkit.deprecation`

How domainkit retires an API (the policy is in `docs/versioning.md`).

- `DomainkitDeprecationWarning`: A domainkit API that a later release removes; the message says what to use.
- `moved`: A module `__getattr__` for `names` that moved: name to its new module.

## `domainkit.encryption`

Secrets the database keeps, encrypted: AES-256-GCM under a key from `SECRET_KEY`.

- `DecryptionFailed`: No key decrypts the value: wrong key, other associated data, or tampering.
- `SecretBox`: Encrypts and decrypts values for one purpose.

## `domainkit.http`

FastAPI integration: application factory, middleware, and error mapping.

- `CURSOR_PATTERN`: Cursors are base64url; use as `Query(pattern=CURSOR_PATTERN)`.
- `MAX_CURSOR_LENGTH`: Longest cursor accepted; use as `Query(max_length=MAX_CURSOR_LENGTH)`.
- `APIError`: An HTTP error outside the domain (a bad cursor, an oversized body).
- `ErrorResponse`: Body of every error response; also the schema documented in OpenAPI.
- `FieldError`: One invalid part of a request; the submitted value is left out.
- `Filter`: One filtered field of a `FilterSet` and the lookups it allows.
- `FilterSet`: Declarative filters, ordering, and search for one aggregate's listing.
- `InvalidCursor`: The cursor was not issued by this API, or was altered.
- `InvalidFilter`: The query string asks for a filter the endpoint does not offer.
- `JSONResponse`: Starlette's `JSONResponse`, rendered by `encode_json`: the same bytes.
- `PageOut`: A page of items: pass `next_cursor` back as `cursor` for the next.
- `PageRequest`: The page a request asks for; `Pagination` makes it from the query string.
- `Pagination`: A FastAPI dependency that reads a `PageRequest` from the query string.
- `cursor_length`: The longest cursor `encode_position` makes for `ordering` over `model`.
- `decode_cursor`: Decode a cursor made by `encode_cursor` with `size` values.
- `encode_cursor`: Encode a sort key as an opaque, URL-safe cursor.
- `error_response`: Build an error body that carries the request id for support cases.
- `error_responses`: OpenAPI `responses` for the error statuses a route may answer with.
- `get_asgi_application`: Set up the framework and return the configured application.

## `domainkit.http.access`

Who may call an endpoint: access rules, for any authentication (ADR 0080).

- `MODEL_ACTIONS`: The permission each method needs from `ModelPermissions`.
- `SAFE_METHODS`: Methods that only read: `IsAuthenticatedOrReadOnly` lets anyone use them.
- `AllOf`: Every rule must allow.
- `AllowAny`: Anyone, logged in or not.
- `AnonymousPrincipal`: A caller who proved nothing: no id, no permissions, for any middleware.
- `AnyOf`: One rule must allow, for the request and for the object alike.
- `BasePermission`: Allows everything; subclasses override one hook or both.
- `CurrentUser`: The request's principal, or an anonymous one.
- `HasObjectPermissions`: Principals granted every permission named on the object itself.
- `HasPermissions`: Principals granted every permission named (`<domain>.<codename>`).
- `HasScope`: Users whose credentials allow every scope named; unscoped ones allow all.
- `IsAuthenticated`: Principals who proved who they are (users who logged in).
- `IsAuthenticatedOrReadOnly`: Anyone may read (`GET`, `HEAD`, `OPTIONS`); users may write.
- `IsUnscoped`: Users whose credentials are not limited to scopes (a login, not a key).
- `ModelPermissions`: Django's model permissions, by method, for one model.
- `Not`: Allows what `rule` refuses.
- `NotAuthenticated`: The route needs a principal who proved who they are, and none did (401).
- `Principal`: Who sent a request, as the access rules read them (`scope["user"]`).
- `Public`: `dependencies=[Public]`: the route needs no rule (`REQUIRE_ROUTE_RULES`).
- `Require`: A FastAPI dependency: the rules must allow the request; returns the principal.
- `Rule`: A rule, or a rule class that takes no arguments.
- `acurrent_user`: `current_user` as a dependency FastAPI awaits, with no thread to hop to.
- `check_object_permissions`: Refuse the request unless every rule allows it on `obj`.
- `current_user`: The principal the authentication middleware found (anonymous if none).
- `mark_public`: Mark a mounted application or a Starlette endpoint public on purpose.
- `open_routes`: `METHOD /path` of every route that declares no access rule.
- `reads_principal`: Mark `dependency` as one that reads the principal, as `CurrentUser` does.
- `scopes_of`: The scopes the request's credentials are limited to; None: no limit.

## `domainkit.http.application`

Build the FastAPI application from settings (Django's `get_asgi_application`).

- `Application`: The application `get_asgi_application` builds: telemetry off below it too.
- `get_asgi_application`: Set up the framework and return the configured application.

## `domainkit.http.blocking`

Find code that blocks the event loop before production does.

- `allow_sync`: Mark a sync route handler or dependency as sync on purpose.

## `domainkit.http.client`

Outgoing HTTP calls, bounded by the request's deadline (`domainkit[httpclient]`).

- `CallTimeout`: A call took longer than its `timeout`, retries included.
- `CircuitBreaker`: Circuits per host: open after failures in a row, then one trial at a time.
- `CircuitOpen`: Calls to a host fail at once while its circuit is open.
- `HttpClient`: Outgoing calls, pooled, bounded, retried, and broken off per host.
- `ResponseTooLarge`: An answer was longer than `MAX_RESPONSE_BYTES`, as sent or decompressed.
- `UnsafeURL`: A URL that may not be called; the message never quotes it.
- `client`: A client with `HTTP_CLIENT`'s options, for calls that need no others.
- `close_all`: Close every client's connections on the running event loop.
- `stub`: Send every call made in the block (and tasks it starts) to `handler`.

## `domainkit.http.deadlines`

Request deadlines over HTTP: `REQUEST_TIMEOUT`, and `RequestTimeout` per route.

- `DeadlineMiddleware`: Cancel HTTP requests that run past their deadline, and answer 503.
- `RequestTimeout`: A dependency that gives a route its own deadline (`Depends`).

## `domainkit.http.live`

Long-lived connections: WebSockets and event streams, bounded and re-checked.

- `Closed`: Raised by an event source to end its connection, saying why.
- `Closure`: Why the framework ended a connection: the close code and its reason.
- `LiveLimits`: A FastAPI dependency: a route's own limits for its connections.

## `domainkit.http.live_events`

Live committed events: domain events streamed to the clients that may see them.

- `live_events`: The process's live events, fed by the global `event_bus`.

## `domainkit.http.locale`

`LocaleMiddleware`: answer in the language the caller asks for.

- `LocaleMiddleware`: Make the language `Accept-Language` asks for the request's language.

## `domainkit.http.metrics`

Prometheus metrics (`domainkit[metrics]`).

- `MetricsMiddleware`: Count requests and time them, by method, route template, and status.

## `domainkit.http.middleware`

Pure-ASGI middleware enforcing the HTTP security baseline.

- `AllowedHostsMiddleware`: Reject requests whose Host header is not in `ALLOWED_HOSTS`.
- `CORSMiddleware`: Starlette's CORS middleware configured from `CORS_*` settings.
- `ProxyHeadersMiddleware`: Take the client address and scheme from trusted proxies' headers.
- `RequestBodyLimitMiddleware`: Refuse bodies over the limit, or too slow to arrive.
- `RequestContextMiddleware`: Bind request context, echo the request id, and write the access log.
- `SecurityHeadersMiddleware`: Add security headers unless the endpoint already set them.
- `request_path`: The request's path within the application: without its `root_path`.

## `domainkit.http.ratelimit`

Request rate limiting (`RATE_LIMIT = "300/minute"`).

- `Backend`: Where rate-limit state lives.
- `Decision`: Whether a request may proceed, and the numbers for the headers.
- `MemoryBackend`: GCRA state in this process, for at most `max_keys` callers.
- `RateLimitMiddleware`: Refuse callers over `RATE_LIMIT`; place it after RequestContextMiddleware.
- `RedisBackend`: GCRA in Redis or Valkey, shared by every replica (`RATE_LIMIT_CACHE`).
- `RefundingBackend`: A backend that can give a counted request back.
- `Throttle`: A FastAPI dependency: at most `rate` requests per caller to a route.

## `domainkit.http.responses`

JSON responses encoded in Rust, byte for byte as Starlette encodes them.

- `JSONResponse`: Starlette's `JSONResponse`, rendered by `encode_json`: the same bytes.
- `json_native`: Whether `to_json` writes `content` as `json.dumps` does (see above).

## `domainkit.http.routing`

Serve the routes of included routers from the top of the application.

- `flatten_routes`: Put the routes of each router included in `router` in its place.
- `serves_itself`: Whether `router` has its own version of a method that serves requests.

## `domainkit.http.shedding`

Load shedding: a cap on the HTTP requests an event loop works on at once.

- `ConcurrencyLimitMiddleware`: Refuse HTTP requests past `MAX_CONCURRENT_REQUESTS` with 503.

## `domainkit.http.sse`

Server-Sent Events: a response that streams events to a browser's `EventSource`.

- `Event`: What a stream yields: an event, or text sent as one event's data.
- `EventSourceResponse`: Stream `events` as Server-Sent Events (`text/event-stream`).
- `ServerSentEvent`: One event of a stream: its data, and optionally a type, id, and retry.
- `comment`: `text` as a comment, which `EventSource` ignores.
- `last_event_id`: The id of the last event a reconnecting client saw (`Last-Event-ID`).

## `domainkit.http.uploads`

Receiving uploaded files, and serving stored ones, safely.

- `GIF`: A type of file a route accepts, and how to recognize its first bytes.
- `IMAGES`: Images browsers show; SVG is not one of them, as it can carry scripts.
- `JPEG`: A type of file a route accepts, and how to recognize its first bytes.
- `PDF`: A type of file a route accepts, and how to recognize its first bytes.
- `PNG`: A type of file a route accepts, and how to recognize its first bytes.
- `TEXT`: UTF-8 text, CSV included; it cannot be told apart by its bytes.
- `WEBP`: A type of file a route accepts, and how to recognize its first bytes.
- `ZIP`: Also Office documents (`.docx`, `.xlsx`), which are zip files.
- `FileType`: A type of file a route accepts, and how to recognize its first bytes.
- `sanitize_filename`: A client's file name, safe to show and to put in `Content-Disposition`.
- `save_upload`: Store `upload` if it is one of `types` and at most `max_size` bytes.
- `serve`: Answer with the stored file `name`, streamed, as `content_type`.

## `domainkit.http.versioning`

API versions by URL prefix, and deprecation headers (RFC 9745, RFC 8594).

- `Deprecation`: When routes were deprecated, when they go, and where to read why.
- `versioned_router`: A router for one API version, served under `/<version>`.

## `domainkit.http.websockets`

`WebSocketLimitsMiddleware`: every WebSocket bounded, and authorized for life.

- `WebSocketLimitsMiddleware`: Cap, queue, rate-limit, time, and re-authorize every WebSocket.

## `domainkit.i18n`

Responses in the caller's language, and dates in the business's time zone.

- `LazyString`: A translation made each time the string is used, in the language active then.
- `date_format`: A date, or the local date of an aware datetime: `Oct 19, 2026`.
- `datetime_format`: An aware datetime in the current zone: `Oct 19, 2026, 3:04 PM`.
- `get_language`: The language of the request or block being served (`LANGUAGE_CODE` if none).
- `gettext`: `message` in the active language, or itself when no catalog has it.
- `gettext_lazy`: `gettext` when the string is used.
- `gettext_noop`: Mark `message` for translators without translating it now.
- `localdate`: The date `value` (default: now) falls on in `timezone`.
- `localtime`: `value` (default: now) in `timezone` (default: the current zone).
- `negotiate`: The language of `languages` an `Accept-Language` header prefers most.
- `ngettext`: The form of `singular`/`plural` for `n`, by the language's plural rule.
- `ngettext_lazy`: `ngettext` when the string is used.
- `npgettext`: `ngettext` within `context`.
- `number_format`: `value` with the language's digits and separators (`1,234.5` in English).
- `override`: Use `language` (`"ar"`) inside the block, as a request in it would.
- `pgettext`: `message` as translated in `context` (a `msgctxt`).
- `pgettext_lazy`: `pgettext` when the string is used.
- `time_format`: A time, or the local time of an aware datetime: `3:04 PM`.
- `timezone`: Local time for display (Django's `django.utils.timezone`).
- `validation_message`: The `msg` of one pydantic (or FastAPI) validation error, translated.

## `domainkit.i18n.formats`

Numbers and dates written as each language writes them.

- `ARABIC`: CLDR's `ar`: Arabic-Indic digits, Arabic month names, and RLM marks.
- `date_format`: A date, or the local date of an aware datetime: `Oct 19, 2026`.
- `datetime_format`: An aware datetime in the current zone: `Oct 19, 2026, 3:04 PM`.
- `number_format`: `value` with the language's digits and separators (`1,234.5` in English).
- `register_formats`: Write numbers and dates of `language` with `formats` from now on.
- `time_format`: A time, or the local time of an aware datetime: `3:04 PM`.

## `domainkit.i18n.timezone`

Local time for display (Django's `django.utils.timezone`).

- `localdate`: The date `value` (default: now) falls on in `timezone`.
- `localtime`: `value` (default: now) in `timezone` (default: the current zone).
- `now`: The current time, aware, in UTC: what to store and compare.
- `override`: Show times in `timezone` (a name or a `tzinfo`) inside the block.

## `domainkit.ledger`

Ledgers: write-once, read-many stores whose reads and writes are verified.

- `Ledger`: An append-only key-value store with verified reads and writes.
- `LedgerChanged`: `append(..., unchanged_since=...)` found a later revision of the key.
- `LedgerEntry`: One verified revision of a key.
- `LedgerError`: Base class of ledger errors.
- `LedgerHandler`: Create each configured ledger on first use and keep it for the process.
- `LedgerKeyExists`: `create` found the key already written.
- `LedgerKeyNotFound`: The key, or the requested revision of it, does not exist.
- `LedgerTampered`: A proof or a server signature did not verify: do not trust the ledger.
- `LedgerUnavailable`: The ledger could not be reached in time.
- `LedgerWrite`: Where a verified write landed.
- `MemoryLedger`: Revisions in a dictionary, numbered by a transaction counter.
- `ledgers`: The process-wide ledgers: `ledgers["evidence"]`.

## `domainkit.lifecycle`

Shutting a process down, in phases, within one budget (`SHUTDOWN_TIMEOUT`).

- `PHASE_ENDS`: The share of the budget, from the start of the shutdown, each phase ends at.
- `Budget`: A shutdown's time: when it began, on the loop's clock, and for how long.
- `Closer`: Closes one kind of thing for the running loop (`caches.close_all`).
- `Phase`: The steps of a shutdown, in order.
- `close_resources`: Finish the running loop's work and close what it opened, in budget.
- `closers`: The closers `phase` runs, as steps: each at once, the steps in turn.
- `register`: Call `close` for the running loop when it shuts down, in `phase`.
- `serving`: Start the installed domains; at the end, shut everything down in budget.
- `shutdown_timeout`: `SHUTDOWN_TIMEOUT`, or its default where no settings are loaded.
- `unregister`: Stop closing `name` at shutdown; an unknown name is ignored.

## `domainkit.log`

Structured logging: JSON for log pipelines, escaped text for terminals.

- `WARNING_RESERVE`: Part of a logging queue only records at `WARNING` and above may fill.
- `BackgroundHandler`: Put records on a bounded queue for `BackgroundListener` to write.
- `BackgroundListener`: The logging thread: takes records off the queue and writes them.
- `ContextFilter`: Attach request and trace correlation fields to every record.
- `JsonFormatter`: Render one JSON object per line; control characters are always escaped.
- `TextFormatter`: Human-readable lines; CR/LF in messages are escaped to stop log forging.
- `ThrottledLog`: Warnings written once per event and key each `interval`, with a count.
- `aflush_logs`: `flush_logs` on a worker thread, for the lifespan's shutdown.
- `aflush_throttled`: `flush_throttled`, for the shutdown's last phase, before the logs'.
- `build_logging_config`: Return a `LOGGING` mapping for `text` or `json` output on stdout.
- `configure_logging`: Apply a `LOGGING` mapping with `logging.config.dictConfig`.
- `flush_logs`: Wait until every background handler has written what it queued.
- `flush_throttled`: Write the counts every live `ThrottledLog` still holds back.
- `log_enabled`: `logger.log(level, msg, *args, extra=extra)`, once `isEnabledFor` said yes.

## `domainkit.management`

Run management commands: `python manage.py <command>`.

- `BaseCommand`: One `manage.py` command, in `<package>.management.commands.<name>`.
- `CommandError`: Stop the command and print the message; the process exits with `returncode`.
- `call_command`: Run command `name` from code; `options` override parsed defaults.
- `execute_from_command_line`: Dispatch `argv` (default `sys.argv`) to a command and exit on errors.
- `get_commands`: Map each command name to the package that provides it.

## `domainkit.management.base`

Base class for management commands (`django.core.management.base`).

- `BaseCommand`: One `manage.py` command, in `<package>.management.commands.<name>`.
- `CommandError`: Stop the command and print the message; the process exits with `returncode`.
- `run_async`: Run `awaitable` in a new event loop, then close what it connected.

## `domainkit.management.generators`

What the code generators share: names, field types, and careful edits.

- `FieldType`: How the generators write fields of one kind (`--field name:kind:options`).
- `register_field_type`: Let `--field x:<name>` generate fields of `kind`.

## `domainkit.reporting`

Send unexpected errors to error trackers (Sentry, Rollbar, ...).

- `Reporter`: An error reporter: `(exc, context) -> None` (`ERROR_REPORTERS`).
- `aflush_reports`: `flush_reports` on a worker thread, for the lifespan's shutdown.
- `flush_reports`: Wait until the reports made so far were sent; False after `timeout`.
- `report_error`: Hand `exc` to every reporter on the reporting thread; never waits.
- `reporters`: Return the configured reporters, failing fast on bad paths.

## `domainkit.resources`

REST resources over aggregates: routes, schemas, conditional writes, policies.

- `MERGE_PATCH`: RFC 7396's media type; `PATCH` routes take it as well as JSON.
- `UNSET`: A field left out of a partial change (`PatchModel`, `UpdateX` commands).
- `Actions`: Rules that depend on a route's action (`view`, `add`, ...).
- `BatchAnswer`: One result per item, in order, and how many failed.
- `BatchError`: Why one item failed, as a request of its own would have been told.
- `BatchResult`: What became of one item.
- `DomainSchema`: A read schema `schema_for` made; `from_domain` fills it from an object.
- `ExportTooLarge`: `422`: an export would hold more rows than the resource allows.
- `FieldNotEditable`: A partial change sets a field the caller may not change.
- `FieldPolicy`: Who sees and who changes each field of a resource (see the module).
- `HasModelPermission`: Users granted `<domain>.<action>_<model>`, the model resolved per request.
- `ModelActions`: Model permissions by each route's action, after any `rules` given.
- `NotAcceptable`: `406`: the request accepts none of the media types offered.
- `Operation`: One route of a resource, as its rules and a generic client see it.
- `PatchModel`: A partial change: each field defaults to `UNSET`; unknown ones are refused.
- `PreconditionRequired`: The resource changes only with `If-Match`, and the request sent none.
- `RecordChanged`: Fields of an aggregate were changed through a generic route.
- `RecordCreated`: An aggregate was created through a generic route.
- `RecordDeleted`: An aggregate was deleted through a generic route.
- `ResourceRouter`: The routes of one aggregate type (see the module).
- `Unset`: The type of `UNSET`: a field the request left out, as opposed to None.
- `entity_tag`: The weak tag of one aggregate at `version`, sent as `representation`.
- `expected_versions`: The versions an `If-Match` value accepts; None for `*` (any).
- `schema_for`: A pydantic schema of `model`'s fields (see the module).
- `to_domain`: A request's `value` as `field` holds it: value objects built, enums found.

## `domainkit.signing`

Signed values: proof that this service made a value, and when (Django's `signing`).

- `BadSignature`: The value was not signed with this key and salt, or it was changed.
- `Key`: A secret key: text (as settings hold it) or bytes.
- `SignatureExpired`: The signature holds, but it is older than `max_age`.
- `Signer`: Signs text, and checks the signature: `value:signature`.
- `TimestampSigner`: Signs text with the time it was signed: `value:timestamp:signature`.
- `constant_time_compare`: Whether two values are equal, in a time that does not tell how close.
- `derive_key`: A key for `purpose` alone, derived from `key` (default: `SECRET_KEY`).
- `derive_keys`: `derive_key` for `SECRET_KEY` and each fallback, in that order.
- `dumps`: `obj` as signed, timestamped, URL-safe JSON (`loads` reads it).
- `loads`: The object `dumps` signed, if signed at most `max_age` ago.

## `domainkit.storage`

Files: a storage port, on the file system or in memory.

- `FileMissing`: No file has the name (a malformed name names none): 404 over HTTP.
- `FileSystemStorage`: Files under `location`, an absolute directory created on first save.
- `FileTooLarge`: The content went over the size limit while it was saved; nothing was kept.
- `MemoryStorage`: Files in this process's memory; gone when it ends.
- `Storage`: Files under names the storage chooses: save, open, delete, check, size.
- `StorageError`: A storage refused an operation.
- `StorageHandler`: Make each configured storage on first use; one per alias and process.
- `StoredFile`: A file open for reading; close it, or use it in `async with`.
- `check_name`: Return `name` if a storage could hold a file under it.
- `new_name`: A new random name, in `directory`, ending in `extension` (`.png`).
- `storages`: The process-wide storages: `storages["default"]`.

## `domainkit.tasks`

Background tasks the framework starts: clean, kept, owned, and stopped.

- `Factory`: Makes one run of a supervised task (an `async def` called with no arguments).
- `adopt`: Own `task`, started some other way, so shutdown waits for it too.
- `clean_context`: Return a new, empty context, holding only the frozen clock if one is in use.
- `owned`: The running loop's `spawn`-ed tasks that have not finished.
- `spawn`: Run `coro` as a task of the running loop, in a clean context, owned.
- `supervise`: Keep `factory`'s work running: restart a run that crashes, until cancelled.

## `domainkit.testing`

pytest plugin for domainkit projects: test settings, databases, clients.

- `IN_MEMORY`: The URL of an in-memory SQLite database, for `migrated_database`.
- `AsyncClientFor`: The `async_client_for` fixture's type: awaited, it gives a client for a new user.
- `AsyncTestClient`: An `httpx2.AsyncClient` that serves an ASGI application in this task.
- `CapturedQueries`: The SQL statements run while it is entered; checked against a count.
- `ClientFor`: The `client_for` fixture's type: a client for a new user with permissions.
- `Factory`: Make `T` objects for tests: `build` keeps them in memory, `create` stores.
- `FreezeTime`: The `freeze_time` fixture's type: freezes the clock, returning it.
- `FrozenClock`: A clock that stands at a moment, or ticks on from it, until moved.
- `LazyAttribute`: `LazyAttribute(lambda o: o.name.lower())`: computed from other fields.
- `LazyFunction`: `LazyFunction(uuid4)`: the function's result, called for each object.
- `Sequence`: `Sequence(lambda n: f"user-{n}")`: the object's number, from 0.
- `SubFactory`: `SubFactory(MoneyFactory, currency="EUR")`: another factory's object.
- `TestClient`: Starlette's `TestClient`; the application sees the caller's context.
- `assert_max_queries`: Check that the `with` block runs at most `limit` SQL statements.
- `assert_num_queries`: Check that the `with` block runs exactly `expected` SQL statements.
- `freeze_time`: Return a clock frozen at `when` (default: now), for a `with` block.
- `migrated_database`: Make, migrate, and later drop the test databases; yield the default's URL.
- `user_token`: Store a new user granted `permissions`; return an access token for them.

## `domainkit.tracing`

OpenTelemetry spans and log correlation (`domainkit[tracing]`).

- `remote_parent`: Make the trace context in `headers` the parent of spans in the block.
- `span`: Run the block inside a span named `name` (a no-op without OpenTelemetry).
- `trace_headers`: The active span's W3C trace context, as headers; empty without one.

## `domainkit.utils.decimals`

One text form for a `Decimal`: fixed point, never an exponent.

- `decimal_text`: `value` in fixed point (`"1000"` for `Decimal("1E+3")`), at its places.

## `domainkit.utils.loops`

Event loops for the framework's own entry points: uvloop when it is there.

- `loop_factory`: The factory of `kind`'s loops; None stands for asyncio's own.
