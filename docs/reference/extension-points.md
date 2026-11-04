# Extension points

An extension point is a place where your code plugs into domainkit: a
class to subclass, a hook to register, or a setting that names your
class. This page lists every extension point, what you implement, and
where you register it. The guides and the recipes linked from each row
show it at work.

On this page you find:

- domains, their settings, and their hooks: startup, readiness, checks,
  commits, migrations, seeds, and privacy;
- tables, routes, and layers;
- users, logins, rules, and the backends of mail, caches, brokers, and
  rate limits;
- the ledger's hooks, and commands;
- how an installed package offers a backend by name.

## A first example

A [domain](../learning.md#words) plugs in through its `DomainConfig`, in
its `apps.py`. `ready()` runs once the settings load; it registers the
hooks the domain has:

```python
from domainkit.apps import DomainConfig, Requirement
from domainkit.apps.hooks import on_ready, on_shutdown, on_startup


async def open_client() -> None:
    """Open the search client once per process."""


async def close_client() -> None:
    """Close it when the application stops."""


async def search_answers() -> None:
    """Raise when the search engine cannot serve: /ready answers 503."""


class CatalogConfig(DomainConfig):
    requires = (Requirement("domainkit.contrib.messaging", "it indexes in tasks"),)

    def ready(self) -> None:
        on_startup(open_client)
        on_shutdown(close_client)
        on_ready(search_answers)
```

`setup()` checks each hook against its point when you register it: the
number of its arguments, and whether it is awaited. A mistake fails
`setup()`, and names the domain.

## Domains and settings

| To add | Implement | Register with |
| --- | --- | --- |
| A bounded context | a package with `apps.py` (`DomainConfig`) | `INSTALLED_DOMAINS` |
| Domain settings | a Pydantic model | `DomainConfig.settings_class` (read from the setting named by `settings_name`: the label in upper case, as `BILLING`) |
| Project settings | a `domainkit.conf.Settings` subclass ([recipe: API keys](../recipes/api-keys.md)) | `SETTINGS_CLASS` in the settings module |
| Domains a domain cannot work without | `DomainConfig.requires`: package names, or `apps.Requirement(name, reason)` | (checked by `setup()` before any `ready()`; a missing one is refused by name, `apps.E001`) |
| Startup wiring, and the hooks a domain has | `DomainConfig.ready()` | (called by `setup()`; a hook is registered there, `on_<method>(function)`, and checked against the point: its arguments, and whether it is awaited; a mistake fails `setup()`, naming the domain) |
| Tables a domain stops using | `DomainConfig.holds_table(table)` | (asked by migrations and content types; a swapped user table answers no) |
| Where a domain's tables are | `DomainConfig.databases(router)` | (asked by the database router; the default is the domain's target in `DATABASE_ROUTING`. Apps whose rows commit with every domain's data, as the outbox and the audit trail, return every target that hosts domains) |
| Packages a domain layer may import, layers of the project's own | the `ARCHITECTURE` keys | `ARCHITECTURE` ([Layer rules](settings.md#layer-rules)) |

## Lifecycle, readiness, and checks

These hooks come from `domainkit.apps.hooks`, and are registered in
`ready()`.

| To add | Implement | Register with |
| --- | --- | --- |
| Process resources (clients, pools) | `async def hook()` | `on_startup(hook)` / `on_shutdown(hook)` (called by the application's lifespan; a domain's shutdown hooks run once all its startup hooks ran. After the domains stop, the framework closes its caches, databases, ledgers, and brokers, in that order, each within 5 seconds; a failure is logged and does not keep the rest open. Management commands close them too.) |
| A readiness check | `async def hook()`, raising when the domain cannot serve | `on_ready(hook)` (called by `/ready`; a domain's checks in turn, within `READINESS_TIMEOUT` together) |
| A check of the built application (routes, middleware) | `def hook(app)`, returning problems, best as `domainkit.checks.CheckMessage("<label>.E001", ...)` | `on_check_application(hook)` (called when the application is built and by `check`; staging and production refuse to start with any) |
| Warnings about a domain's weak settings where deployed, or its code | `def hook()`, returning warnings (about settings: none outside staging and production; about code the project must change, such as messaging's `def` tasks: anywhere), best as `CheckMessage("<label>.W001", ...)`, which `SILENCED_CHECKS` can name | `on_deployment_warnings(hook)` (listed by `check` after the framework's own) |
| Sample or default data | `async def hook()` | `on_seed(hook)` (called by `seed`, in `INSTALLED_DOMAINS` order) |
| Another hook point that an app offers others | `domainkit.apps.hooks.HookPoint(name, arguments=..., awaited=...)` | (domains call it with a function in `ready()`, checked against `arguments` and `awaited` when given; the app iterates it) |
| The same hooks, as methods (removed in 1.0) | `DomainConfig.startup`, `shutdown`, `check`, `seed`, `erase_subject`, `export_subject`, `check_application`, `deployment_warnings`, `stage`, `committed`, `stage_transaction`, `transaction_committed`, `migrated` | (none: `setup()` refuses a domain that still defines one, `apps.E002` until 2.0, and names the point to register on instead; [ADR 0111](../adr/0111-refuse-a-removed-hook-method-until-1-0-wherever-the-domain-defines-it.md)) |

A hook point of your own lets other domains plug into your app:

```python
from collections.abc import Awaitable, Callable

from domainkit.apps.hooks import HookPoint

on_price_changed: HookPoint[Callable[[str], Awaitable[None]]] = HookPoint(
    "price_changed", arguments=1, awaited=True
)
```

## Commits and migrations

These hooks come from `domainkit.db.hooks`, and are registered in
`ready()`.

| To add | Implement | Register with |
| --- | --- | --- |
| Work staged with each commit | `async def hook(uow, events, tasks)`, typed on `SqlAlchemyUnitOfWork` | `on_stage(hook)` (called by the [unit of work](../learning.md#words); only domains that registered are called) |
| Work after each commit | `async def hook(uow)` | `on_committed(hook)` (called by the unit of work; failures are reported, never raised) |
| The same, for a commit outside a unit of work | `async def hook(connection, database)` / `async def hook(database)` | `on_stage_transaction(hook)` / `on_transaction_committed(hook)` (called by `messaging.enqueue` around its own transaction; idempotency uses them to hold the key) |
| Rows after each `migrate` (Django's `post_migrate`) | `async def hook(connection, database)` | `on_migrated(hook)` (called by `migrate` for each database it upgraded, in one transaction as the schema owner, and by `flush`) |

```python
from sqlalchemy.ext.asyncio import AsyncConnection

from domainkit.db.hooks import on_migrated


async def default_categories(connection: AsyncConnection, database: str) -> None:
    """Add the categories every catalog starts with, once."""
```

Register it with `on_migrated(default_categories)` in `ready()`.

## Privacy

| To add | Implement | Register with |
| --- | --- | --- |
| How long a domain keeps rows | `DomainConfig.retention`, a sequence of `privacy.Retention` | (checked when the application is built; applied by `privacy purge`; see [privacy.md](../privacy.md)) |
| Erasing a personal field that no default fits | `erase_personal_data(fields)` on the aggregate | (called by `privacy erase` and by retention erasure) |
| Erasing, and exporting, a person's data that a domain keeps outside its aggregates (copies in deliveries, kept responses, queues) | `async def erase(subject, reason)`, returning counts by kind; `async def export(subject)`, returning JSON-ready data | `on_erase_subject(erase)` / `on_export_subject(export)` in `ready()` (called by `privacy erase` and `privacy export`; the counts are audited; see [privacy.md](../privacy.md#what-erasure-reaches-and-when)) |
| Holding an erasure while a law asks to keep a person's data (an open account, a running contract) | `async def check(subject, reason)`, returning a sentence for the operator, or None | `on_retain_subject(check)` in `ready()` (asked by `privacy erase` before anything is erased; any sentence holds it: `ErasureHeld`; see [privacy.md](../privacy.md#what-the-law-keeps-holding-an-erasure)) |

## Tables and routes

| To add | Implement | Register with |
| --- | --- | --- |
| How errors name a column | `info={"field": "<API name>"}` on the `Column` | (read by unique, foreign key, NOT NULL, and CHECK errors) |
| A table that fixtures or `flush` leave alone | `info={"fixtures": False}`, `{"flush": False}`, or `{"append_only": True}` on the `Table`; `{"guarded": True}` for one that only `--include-guarded` reaches | (read by `dumpdata`, `loaddata`, and `flush`; see [Data: fixtures, flush, and seeds](commands.md#data-fixtures-flush-and-seeds)) |
| A deprecated route or version | `Depends(Deprecation(...))`, `versioned_router(...)` | `domainkit.http.versioning` ([Versions and deprecations](responses.md#versions-and-deprecations)) |
| Middleware | an ASGI class taking `app` | `MIDDLEWARE` ([Middleware](http.md#middleware)) |
| An error tracker | `(exc, context) -> None` | `ERROR_REPORTERS` |

```python
from typing import Any


def report_to_tracker(exc: BaseException, context: dict[str, Any]) -> None:
    """Send the error, with the request id the context holds, to a tracker."""


ERROR_REPORTERS = ["config.errors.report_to_tracker"]
```

## Users, logins, and rules

| To add | Implement | Register with |
| --- | --- | --- |
| A user model | a subclass of `AbstractUser` or `AbstractBaseUser`, and its table | `AUTH.USER_MODEL`, `AUTH.USER_TABLE` (`startdomain --user` writes both) |
| An authentication or permission backend | `auth.backends.BaseBackend` | `AUTH.BACKENDS` |
| A way requests prove who sent them | `auth.authentication.BaseAuthentication` ([recipe: API keys](../recipes/api-keys.md)) | `AUTH.AUTHENTICATION_CLASSES` |
| A rule for who may call a route | `domainkit.http.access.BasePermission` ([recipe](../recipes/permission-rules.md)) | `Depends(Require(rule))` on the route, or `Public` for anyone |
| Another way to authenticate requests (OpenID Connect, a gateway's header) | an ASGI middleware that puts a `domainkit.http.access.Principal` in `scope["user"]` | `MIDDLEWARE` ([Another authentication](../auth/routes.md#another-authentication)) |
| A password hasher | `auth.hashers.PasswordHasher` | `AUTH.PASSWORD_HASHERS` |
| A password rule | a class with `validate(password, user)` and `help_text()` | `AUTH.PASSWORD_VALIDATORS` |

## Backends

| To add | Implement | Register with |
| --- | --- | --- |
| An email backend (a provider's API) | `mail.BaseEmailBackend` | `MAIL.BACKEND` |
| A text-message or push provider | `notifications.SmsBackend`, `notifications.PushBackend` ([recipe](../recipes/sms-backend.md)) | `NOTIFICATIONS.SMS_BACKEND`, `NOTIFICATIONS.PUSH_BACKEND` |
| A cache store other than memory or Redis | `domainkit.cache.Store`, made as `Store(config)` ([recipe](../recipes/cache-store.md)) | `CACHES[alias].BACKEND`; its `URL`, of a scheme of its own, is the store's to read |
| A broker other than the ones domainkit ships | `domainkit.broker.Broker`, or `broker.pull.PullBroker` for one that is asked for messages; made as `Broker(config)` ([recipe](../recipes/broker.md)) | `BROKERS[alias].BACKEND`; its `URL` is the broker's to read |
| A file storage | `domainkit.storage.Storage` | `STORAGES[alias].BACKEND` ([backends.md](backends.md#storages)) |
| A virus scanner, or a search engine | `domainkit.storage.scanning.Scanner`; `domainkit.search.SearchBackend` | `UPLOAD_SCANNER.BACKEND`; `SEARCH.BACKEND` |
| A rate-limit store other than memory or Redis | `ratelimit.Backend` | `RATE_LIMIT_BACKEND` |

## The ledger, approvals, and limits

| To add | Implement | Register with |
| --- | --- | --- |
| A way to match statement lines to postings | `accounting.reconciliation.MatchingRule`: a `name` and `match(lines, postings, window)` | `ACCOUNTING.MATCHING_RULES` ([accounting.md](../contrib/accounting.md#reconciliation)) |
| Statements from a bank's file format or API | `accounting.reconciliation.StatementSource`: a `name` and `lines()` | `import_statement(account_id, source)` |
| A use case that runs only once others approve it (maker-checker) | `@approvals.requires_approval(action)` on its service, and `approvals.Policy` | `approvals.guard(action, service, ...)` in `ready()` ([approvals.md](../contrib/approvals.md#guarding-a-use-case)) |
| A check of every entry before the ledger posts it | `async def hook(command)`, raising to refuse it | `accounting.hooks.on_post_entry(hook)` in `ready()` (approvals refuses large entries there) |
| Something to check or count on every entry as the ledger books it (limits do) | `async def hook(uow, entry, accounts)`, raising a `DomainError` to refuse the entry | `contrib.accounting.hooks.on_stage_entry(hook)` in `ready()` (called in the posting's transaction, after its accounts are locked and their balances moved; [limits.md](../contrib/limits.md#on-the-ledger)) |
| Subjects of the project's own that limits bound (a merchant, a card) | a kind of subject: `Movement(amount, {"merchant": merchant_id})` | `contrib.limits.consume(uow, ...)` in the use case ([limits.md](../contrib/limits.md#counting-movements)) |

## Commands and generators

| To add | Implement | Register with |
| --- | --- | --- |
| A command | `BaseCommand`, or `ActionCommand` for one of several actions ([Writing a command of your own](commands.md#writing-a-command-of-your-own)) | `management/commands/<name>.py` |
| A scaffolding command | `management.generators.Generator` (`plan`, then `apply`), or `AggregateGenerator` for one that adds to an aggregate | a domain's `management/commands/` |
| A field kind for `--field` | `management.generators.FieldType` | `register_field_type(name, kind)` ([resources.md](../resources.md#generators)) |

```python
from domainkit.management.generators import FieldType, register_field_type

register_field_type(
    "email",
    FieldType(
        annotation="Email",
        column="String(254)",
        imports=(("domainkit.ddd", "Email"),),
        example='"ann@example.com"',
        request='"ann@example.com"',
    ),
)
```

A kind lives in the process that registers it. So register it in a
command of your own, which then runs the generator with
`call_command("startaggregate", "inventory", "Customer", "--field", "contact:email")`.

## Backends from an installed package

A package can offer the backends it brings by name. A project then
installs the package and names the backend without its module path. The
package declares its backends in the `domainkit.backends` entry points of
its `pyproject.toml`:

```toml
[project.entry-points."domainkit.backends"]
sqlite-queue = "sqlitequeue.broker:SqliteBroker"
```

A project names the backend where it would name the class:

```python
BROKERS = {
    "default": {"URL": "sqlitequeue:///queue.sqlite3", "BACKEND": "sqlite-queue"}
}
```

This works for:

- the `BACKEND` of `CACHES` and `BROKERS` entries;
- the `BACKEND` of `STORAGES`, `SEARCH`, and `UPLOAD_SCANNER`;
- `MAIL.BACKEND`;
- `NOTIFICATIONS`' `SMS_BACKEND` and `PUSH_BACKEND`.

A name has no dot; a dotted path is imported as before. Installing a
package loads nothing by itself: only the backend that a setting names is
loaded. A name that no installed package offers is refused, with the
nearest name offered. A name that two packages offer differently is
refused: name that backend by its dotted path.

## See also

- [Settings](settings.md): `SETTINGS_CLASS`, `INSTALLED_DOMAINS`, and
  `ARCHITECTURE`.
- [Checks](checks.md): the checks of your own that hooks report.
- [Recipes](../recipes.md): a cache store, a broker, an SMS provider, a
  permission rule, and API keys, each from start to finish.
