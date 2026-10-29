# Public API: setup, settings, and the process

`domainkit` itself, its domains and their hooks, settings, checks, the
clock and deadlines, logs, reports, and traces, translations, signing,
commands, tasks, and the test tools. Part of the [public API](../api.md),
under its compatibility policy.

## `domainkit`

domainkit: a Django-style framework for domain-driven FastAPI services.

- `__version__`: The installed distribution's version, as `importlib.metadata` reports it.
- `setup`: Configure logging and load the installed domains (`django.setup()`).

## `domainkit.apps`

Installed domains (`django.apps`): configuration classes and the registry.

- `DomainConfig`: Describe one installed domain: its label, settings, and what it requires.
- `DomainRegistry`: Installed domains in `INSTALLED_DOMAINS` order.
- `Requirement`: A domain package another needs in `INSTALLED_DOMAINS`, and why.
- `registry`: The process-wide registry populated by `domainkit.setup()`.

## `domainkit.apps.hooks`

Typed hooks a domain registers for what it takes part in.

- `Hook`: One registered function, and the domain that registered it.
- `HookPoint`: Functions installed domains registered for one purpose, in domain order.
- `on_check`: Raise when the domain cannot serve traffic (`/ready`): `()`.
- `on_check_application`: Problems with the built application: `(app)`, best as `CheckMessage`.
- `on_deployment_warnings`: Weak but valid settings where deployed: `()`; none elsewhere.
- `on_erase_subject`: Erase the copies kept of a person's data: `(subject, reason)`.
- `on_export_subject`: What the domain keeps about a person outside its aggregates: `(subject)`.
- `on_seed`: Add the rows a fresh database needs (`manage.py seed`): `()`.
- `on_shutdown`: Release what the domain's startup hooks opened: `()`.
- `on_startup`: Open process resources, such as clients and pools: `()`.

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

## `domainkit.checks`

System checks: what `check` and startup find, each with an id and a level.

- `MAX_SILENCED_CHECKS`: The most ids `SILENCED_CHECKS` may list.
- `CheckFailed`: Checks found errors; the message lists every one, with its id.
- `CheckMessage`: One finding of a check: the message, with an `id` and a `hint`.
- `Level`: How serious a check's finding is, read from its id's letter.
- `describe`: One finding as `check` prints it: the id, the message, the hint.
- `silence`: `messages` without those whose id `SILENCED_CHECKS` lists.
- `validate_silenced`: Check `SILENCED_CHECKS`: ids of warnings, each once, and not too many.

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

## `domainkit.exceptions`

Errors raised by the framework itself (`django.core.exceptions`).

- `DomainkitError`: The root of every exception domainkit raises.
- `ImproperlyConfigured`: Settings, the environment, or the installed domains are invalid.
- `ServiceBusy`: Something the operation needs is busy now; trying again later may work.

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

- `AggregateGenerator`: A generator that adds to an aggregate of a domain `startdomain` made.
- `FieldType`: How the generators write fields of one kind (`--field name:kind:options`).
- `Generator`: A scaffolding command: plan every change, then write them all.
- `register_field_type`: Let `--field x:<name>` generate fields of `kind`.

## `domainkit.reporting`

Send unexpected errors to error trackers (Sentry, Rollbar, ...).

- `Reporter`: An error reporter: `(exc, context) -> None` (`ERROR_REPORTERS`).
- `aflush_reports`: `flush_reports` on a worker thread, for the lifespan's shutdown.
- `flush_reports`: Wait until the reports made so far were sent; False after `timeout`.
- `report_error`: Hand `exc` to every reporter on the reporting thread; never waits.
- `reporters`: Return the configured reporters, failing fast on bad paths.

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
- `SettingsOverrides`: The `settings` fixture: settings changed until the test ends.
- `SubFactory`: `SubFactory(MoneyFactory, currency="EUR")`: another factory's object.
- `TestClient`: Starlette's `TestClient`; the application sees the caller's context.
- `assert_max_queries`: Check that the `with` block runs at most `limit` SQL statements.
- `assert_num_queries`: Check that the `with` block runs exactly `expected` SQL statements.
- `freeze_time`: Return a clock frozen at `when` (default: now), for a `with` block.
- `migrated_database`: Make, migrate, and later drop the test databases; yield the default's URL.
- `token_for`: Return an access token for `user`, a user stored already.
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

- `PerLoop`: One value per event loop, made on the loop's first use.
- `loop_factory`: The factory of `kind`'s loops; None stands for asyncio's own.
