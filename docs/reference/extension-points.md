# Extension points

| To add | Implement | Register with |
| --- | --- | --- |
| A bounded context | a package with `apps.py` (`DomainConfig`) | `INSTALLED_DOMAINS` |
| Domain settings | a Pydantic model | `DomainConfig.settings_class` |
| Domains a domain cannot work without | `DomainConfig.requires`: package names, or `apps.Requirement(name, reason)` | (checked by `setup()` before any `ready()`; a missing one is refused by name) |
| Startup wiring, and the hooks a domain has | `DomainConfig.ready()` | (called by `setup()`; a hook is registered there, `on_<method>(function)`, and checked against the point: its arguments, and whether it is awaited; a mistake fails `setup()`, naming the domain) |
| Process resources (clients, pools) | `async def hook()` | `domainkit.apps.hooks.on_startup(hook)` / `on_shutdown(hook)` in `ready()` (called by the app lifespan, a domain's shutdown hooks once all its startup hooks ran; after the domains stop, the framework closes its caches, databases, ledgers, and brokers, in that order, each within 5 seconds, and a failure is logged without keeping the rest open; management commands close them too) |
| A readiness check | `async def hook()`, raising when the domain cannot serve | `on_check(hook)` in `ready()` (called by `/ready`; a domain's checks in turn, within `READINESS_TIMEOUT` together) |
| A check of the built application (routes, middleware) | `def hook(app)`, returning problems, best as `domainkit.checks.CheckMessage("<label>.E001", ...)` | `on_check_application(hook)` in `ready()` (called when the app is built and by `check`; staging and production refuse to start with any) |
| Warnings about a domain's weak settings where deployed | `def hook()`, returning warnings (none outside staging and production), best as `CheckMessage("<label>.W001", ...)`, which `SILENCED_CHECKS` can name | `on_deployment_warnings(hook)` in `ready()` (listed by `check` after the framework's own) |
| Work staged with each commit | `async def hook(uow, events, tasks)`, typed on `SqlAlchemyUnitOfWork` | `domainkit.db.hooks.on_stage(hook)` in `ready()` (called by the unit of work; only domains that registered are called) |
| Work after each commit | `async def hook(uow)` | `on_committed(hook)` in `ready()` (called by the unit of work; failures are reported, never raised) |
| The same, for a commit outside a unit of work | `async def hook(connection, database)` / `async def hook(database)` | `on_stage_transaction(hook)` / `on_transaction_committed(hook)` in `ready()` (called by `messaging.enqueue` around its own transaction; idempotency uses them to hold the key) |
| Rows after each `migrate` (`post_migrate`) | `async def hook(connection, database)` | `on_migrated(hook)` in `ready()` (called by `migrate` for each database it upgraded, in one transaction as the schema owner, and by `flush`) |
| The same hooks, as methods (removed in 0.13) | `DomainConfig.startup`, `shutdown`, `check`, `seed`, `erase_subject`, `export_subject`, `check_application`, `deployment_warnings`, `stage`, `committed`, `stage_transaction`, `transaction_committed`, `migrated` | (none: `setup()` refuses a domain that still defines one, `apps.E002`, naming the point to register on instead; [ADR 0111](../adr/0111-refuse-a-removed-hook-method-until-1-0-wherever-the-domain-defines-it.md)) |
| Another hook point an app offers others | `domainkit.apps.hooks.HookPoint(name, arguments=..., awaited=...)` | (domains call it with a function in `ready()`, checked against `arguments` and `awaited` when given; the app iterates it) |
| A scaffolding command | `management.generators.Generator` (`plan`, then `apply`), or `AggregateGenerator` for one that adds to an aggregate | a domain's `management/commands/` |
| Sample or default data | `async def hook()` | `on_seed(hook)` in `ready()` (called by `seed`, in `INSTALLED_DOMAINS` order) |
| How long a domain keeps rows | `DomainConfig.RETENTION`, a sequence of `privacy.Retention` | (checked when the app is built; applied by `privacy purge`; see [privacy.md](../privacy.md)) |
| Erasing a personal field no default fits | `erase_personal_data(fields)` on the aggregate | (called by `privacy erase` and retention erasure) |
| Erasing, and exporting, a person's data a domain keeps outside its aggregates (copies in deliveries, kept responses, queues) | `async def erase(subject, reason)`, returning counts by kind; `async def export(subject)`, returning JSON-ready data | `on_erase_subject(erase)` / `on_export_subject(export)` in `ready()` (called by `privacy erase` and `privacy export`; the counts are audited; see [privacy.md](../privacy.md#what-erasure-reaches-and-when)) |
| How errors name a column | `info={"field": "<API name>"}` on the `Column` | (read by unique, foreign key, NOT NULL, and CHECK errors) |
| A table fixtures or `flush` leave alone | `info={"fixtures": False}`, `{"flush": False}`, or `{"append_only": True}` on the `Table`; `{"guarded": True}` for one only `--include-guarded` reaches | (read by `dumpdata`, `loaddata`, and `flush`) |
| A deprecated route or version | `Depends(Deprecation(...))`, `versioned_router(...)` | `domainkit.http.versioning` |
| Packages a domain layer may import, layers of the project's own | the `ARCHITECTURE` keys | `ARCHITECTURE` ([Layer rules](settings.md#layer-rules)) |
| Tables a domain stops using | `DomainConfig.holds_table(table)` | (asked by migrations and content types; a swapped user table answers no) |
| A user model | a subclass of `AbstractUser` or `AbstractBaseUser`, and its table | `AUTH.USER_MODEL`, `AUTH.USER_TABLE` |
| An authentication or permission backend | `auth.backends.BaseBackend` | `AUTH.BACKENDS` |
| A way requests prove who sent them | `auth.authentication.BaseAuthentication` | `AUTH.AUTHENTICATION_CLASSES` |
| A rule for who may call a route | `domainkit.http.access.BasePermission` | `Depends(Require(rule))` on the route, or `Public` for anyone |
| Another way to authenticate requests (OpenID Connect, a gateway's header) | an ASGI middleware that puts a `domainkit.http.access.Principal` in `scope["user"]` | `MIDDLEWARE` ([Another authentication](../auth/routes.md#another-authentication)) |
| A password hasher | `auth.hashers.PasswordHasher` | `AUTH.PASSWORD_HASHERS` |
| A password rule | a class with `validate(password, user)` and `help_text()` | `AUTH.PASSWORD_VALIDATORS` |
| An email backend (a provider's API) | `mail.BaseEmailBackend` | `MAIL.BACKEND` |
| A text-message or push provider | `notifications.SmsBackend`, `notifications.PushBackend` | `NOTIFICATIONS.SMS_BACKEND`, `NOTIFICATIONS.PUSH_BACKEND` |
| Middleware | an ASGI class taking `app` | `MIDDLEWARE` |
| An error tracker | `(exc, context) -> None` | `ERROR_REPORTERS` |
| A rate-limit store other than memory or Redis | `ratelimit.Backend` | `RATE_LIMIT_BACKEND` |
| A way to match statement lines to postings | `accounting.reconciliation.MatchingRule`: a `name` and `match(lines, postings, window)` | `ACCOUNTING.MATCHING_RULES` ([accounting.md](../contrib/accounting.md#reconciliation)) |
| Statements from a bank's file format or API | `accounting.reconciliation.StatementSource`: a `name` and `lines()` | `import_statement(account_id, source)` |
| A command | `BaseCommand` | `management/commands/<name>.py` |
