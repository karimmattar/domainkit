# Settings

Settings are the values that configure your service: its databases, its
limits, its security headers, and every optional app. You write them in
a settings module, a Python module of UPPERCASE names. domainkit
validates every name and every value when the service starts, so a typo
or a bad value fails at once, not later in production. This page lists
the framework's own settings; the pages linked below list the rest.

On this page you find:

- how a settings module is chosen and loaded;
- how to read environment variables, and secrets mounted as files;
- how to read settings in code, and settings of your own;
- the core settings, the layer rules, languages, and the debug toolbar.

## Where each setting is listed

| Settings | Page |
| --- | --- |
| Core settings, layer rules, languages and time zones, the debug toolbar | this page |
| Databases, ledgers, shards, caches, storages, search, tenancy, brokers | [backends.md](backends.md) |
| HTTP, security headers, CORS, outgoing calls, live connections, middleware | [http.md](http.md) |
| Each optional app's settings (`AUTH`, `MESSAGING`, `AUDIT`, ...) | the app's own page ([contrib.md](../contrib.md)) |

Each table has a Default column. A default in backticks is the value
itself. A row also states its bounds, such as "at most 3600".

## A settings module

A settings module is a Python module of UPPERCASE names. `init` writes
four of them: `base`, `local`, `test`, and `production`. Each one starts
from `base` and changes what differs:

```python
"""Settings for production (selected by SHOP_API_SETTINGS_MODULE)."""

from domainkit.conf import Env

env = Env()

ENVIRONMENT = "production"
SECRET_KEY = env.str("SECRET_KEY")
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS")
INSTALLED_DOMAINS = ["domains.catalog"]
ROOT_ROUTER = "config.urls.router"
DATABASES = {"default": {"URL": env.str("DATABASE_URL"), "SSLMODE": "verify-full"}}
```

### Which module loads

`<PREFIX>SETTINGS_MODULE` names the module, as
`SHOP_API_SETTINGS_MODULE=config.settings.production`. The prefix comes
from `[project].name` in `pyproject.toml` (`shopAPI` gives `SHOP_API_`),
or from `[tool.domainkit] env-prefix`. The local `.env` file that `init`
writes selects the local settings.

### How the settings are validated

`domainkit.conf.Settings` validates every name when the settings load.
Every number must be finite: infinity and NaN are refused, here and in
every setting. A name that the framework does not declare must belong to
an installed domain's `settings_class`, or to a project schema that
`SETTINGS_CLASS` names. Otherwise startup fails, and suggests the
nearest name:

```python
MAX_REQEST_BODY_SIZE = 2_000_000
```

```text
ImproperlyConfigured: Unknown settings in 'mysettings': MAX_REQEST_BODY_SIZE (did you mean MAX_REQUEST_BODY_SIZE?). Declare them in a domain's settings_class or SETTINGS_CLASS.
```

A project schema is a subclass of `Settings` that adds names of the
project's own ([recipe: API keys](../recipes/api-keys.md)):

```python
from domainkit.conf import Settings


class ProjectSettings(Settings):
    API_KEYS: dict[str, str] = {}
```

```python
SETTINGS_CLASS = "config.schema.ProjectSettings"
```

## Environment variables

Read environment variables in a settings module with `domainkit.conf.Env`.
It adds the prefix, reads `.env` (or the file `<PREFIX>ENV_FILE` names;
an empty value skips the file), and converts the value:

```python
import os

from domainkit.conf import Env

os.environ["SHOP_API_WORKERS"] = "4"
os.environ["SHOP_API_DEBUG"] = "true"
os.environ["SHOP_API_ALLOWED_HOSTS"] = "api.example.com,www.example.com"
env = Env(prefix="SHOP_API_")
print(env.int("WORKERS"))
print(env.bool("DEBUG", False))
print(env.list("ALLOWED_HOSTS", []))
print(env.str("REDIS_URL", "redis://localhost:6379/0"))
```

```text
4
True
['api.example.com', 'www.example.com']
redis://localhost:6379/0
```

| Method | What it reads |
| --- | --- |
| `str(name, default)` | Text. |
| `int(name, default)`, `float(name, default)` | A number. A float must be finite. |
| `bool(name, default)` | `1` or `0`, `true` or `false`, `yes` or `no`, `on` or `off`, in any case. |
| `list(name, default)` | Comma-separated text. |
| `path(name, default)` | A `Path`. |

Without a default, a missing variable is an error. `Env()` with no
`prefix` uses the project's prefix; `Env(prefix="")` reads names as they
are.

**Secrets in files.** Any variable can instead be given as
`<NAME>_FILE`, the path of a file that holds the value, as Docker and
Kubernetes mount secrets. Setting both `<NAME>` and `<NAME>_FILE` is an
error.

**Errors never quote values.** An error names the variable and what it
must be, never its value, which may be a secret:

```text
ImproperlyConfigured: SHOP_API_TIMEOUT must be a finite number.
```

**URLs.** Percent-encode `/ ? # @ :` in the user names and passwords of
URLs. A port that cannot be read is refused without quoting the URL.

## Reading settings in code

Read a setting with `domainkit.conf.settings`:

```python
from domainkit.conf import settings

if settings.DEBUG:
    print(settings.ALLOWED_HOSTS)
```

A domain's settings are read through its model:
`registry.settings(SettingsClass)` returns the validated settings of the
domain that declares `SettingsClass`. It validates them once per
settings object, and again after `override_settings`, `use_settings`,
`configure`, or `reset_settings` replaces the object, or `registry.populate`
or `clear` changes the domains.

```python
from domainkit.apps import registry
from domainkit.contrib.auth import AuthSettings

print(registry.settings(AuthSettings).LOGIN_RATE)
```

`repr(settings)` and its dumps show domain settings (`AUTH`,
`EVIDENCE`, ...) as `**********`, because they may hold keys. The
domains' own models keep those keys as secrets.

| Name | What it does |
| --- | --- |
| `settings` | The settings of the process, loaded on first use. |
| `override_settings(**values)` | Changes some settings inside a `with` block, and validates them again. A value of `UNSET` removes a setting, so its default applies. |
| `use_settings(module)` | Loads another settings module inside a `with` block, then puts the current one back. |
| `configure(module)` | Loads a module now, instead of `<PREFIX>SETTINGS_MODULE`. |
| `reset_settings()` | Forgets the loaded settings; the next read loads them again. |
| `load_settings(module)` | Loads and validates a module, and returns its `Settings`. |

## Core

| Setting | Default | Meaning |
| --- | --- | --- |
| `SETTINGS_MODULE` | set by the loader | The module these settings came from. |
| `BASE_DIR` | the project root | The directory that holds `pyproject.toml`. |
| `ENVIRONMENT` | `"local"` | `local`, `test`, `staging`, or `production`. Staging and production get the [deployment checks](checks.md). |
| `SILENCED_CHECKS` | `()` | Ids of warnings that `check` stops reporting, as `security.W001` ([deployment checks](checks.md)). At most 64, each once. Errors cannot be silenced. |
| `DEBUG` | `False` | Development conveniences. Refused in staging and production. |
| `DEBUG_TOOLBAR` | see [Debug toolbar](#debug-toolbar) | The options of `DebugToolbarMiddleware`, which the development settings list in `MIDDLEWARE`. |
| `SLOW_CALLBACK_DURATION` | `0.1` | With `DEBUG`, the application's loop runs in asyncio's debug mode, and logs each step that holds it this many seconds or more ([runtime.md](../runtime.md#finding-blocking-code)). `None` turns it off. |
| `PROJECT_NAME` | `"API"` | The OpenAPI title. |
| `VERSION` | `"0.1.0"` | The OpenAPI version. |
| `SECRET_KEY` | `None` | Signs what the service hands out: reset links, cookies, MFA challenges ([Signing](signing.md)). Required in staging and production, with 50 random characters or more. Never empty: leave it `None` instead. Shown as `**********`. |
| `SECRET_KEY_FALLBACKS` | `()` | Earlier keys. What they signed still verifies while `SECRET_KEY` signs, so you can replace a key without logging anyone out. Held to the same rules. |
| `INSTALLED_DOMAINS` | `()` | Domain packages (Django's `INSTALLED_APPS`), started in order and stopped in reverse order. |
| `ARCHITECTURE` | none added | What the project adds to the layer rules that `check` enforces: packages its domain layers may import, libraries and layers a layer must not use, and layers of its own ([Layer rules](#layer-rules)). |
| `MIDDLEWARE` | see [Middleware](http.md#middleware) | ASGI middleware by dotted path, outermost first. |
| `ROOT_ROUTER` | `None` | The dotted path of the `APIRouter` that mounts every route (Django's `ROOT_URLCONF`). |
| `FLATTEN_ROUTERS` | `True` | Serves the routes of the routers that `ROOT_ROUTER` includes from the top of the application, each matched once ([Included routers](responses.md#included-routers)). `False` keeps FastAPI's nesting, which matches a request again at each level. |
| `REQUIRE_ROUTE_RULES` | `True` | Every route must say who may call it (`Require`, a dependency that reads the principal, or `Public`), whatever authenticates requests. `check` lists the routes that do not, and staging and production refuse to start ([Deny by default](../auth/routes.md#deny-by-default)). `AUTH.REQUIRE_ROUTE_RULES`, its name until 0.10, is refused since 0.11. |
| `SCOPED_CREDENTIALS` | `"refuse"` | What happens to credentials limited to scopes, on a route that declares no rule that checks scopes (`HasScope`, a permission rule, `IsUnscoped`, or `AllowScoped`). `"refuse"` answers 403. `"warn"` lets them through, as 0.10 did by default, with a security log line once per route for a while ([Scopes on every route](../auth/authentication.md#scopes-on-every-route)). |
| `ASGI_APPLICATION` | `None` | The `module:attribute` that `runserver` serves. |
| `EVENT_LOOP` | `"auto"` | The event loop of `runserver` and of commands with an `async` handle (`runworker` among them). `auto`: uvloop when the `uvloop` extra is installed. `asyncio`: always asyncio's own. `uvloop`: uvloop, or a configuration error. |
| `LOGGING` | minimal | A `logging.config.dictConfig` mapping, applied by `setup()`. `domainkit.log.build_logging_config(log_format="json", level="INFO")` makes one that writes from a thread, through a bounded queue ([runtime.md](../runtime.md#logging-and-error-reports)). The settings `init` writes use it. |
| `ERROR_REPORTERS` | `()` | Callables `(exc, context) -> None` by dotted path, called for every unexpected error (500s, dead-lettered messages), on a reporting thread and in the caller's context. 100 reports may wait; more are dropped and counted. A reporter that raises is logged and skipped. |
| `EVENT_HANDLER_TIMEOUT` | `10.0` | Seconds an in-process event handler may run after a commit, up to 3600. A handler still running then is cancelled, logged, and reported (`HandlerTimedOut`), and the request goes on. `event_bus.subscribe(..., timeout=30)` sets one handler's limit. `durable=True` runs a handler from the outbox instead ([runtime.md](../runtime.md#event-handlers)). |
| `SHUTDOWN_TIMEOUT` | `15.0` | Seconds a shutdown may take in all, from 0 to 300: domain hooks, work in flight, background tasks, then connections, each phase within its share ([runtime.md](../runtime.md#shutting-down-within-a-budget)). Add the server's graceful shutdown (`uvicorn --timeout-graceful-shutdown 10`), and keep the sum under the orchestrator's grace period: 30 seconds in Kubernetes. |

## Layer rules

`check` holds every installed domain to the rules of
`domainkit.architecture` ([ADR 0003](../adr/0003-enforced-layer-rules.md)):

- the layers depend inwards: `api → infrastructure → application →
  domain`;
- the domain layer imports only the standard library, `domainkit.ddd`,
  `domainkit.clock`, `domainkit.money`, and itself;
- the application layer uses no web or database library;
- another domain is reached only through its `application` or
  `domain.events`.

`ARCHITECTURE` adds to these rules, and only adds: a rule the framework
states always holds. A broken rule is `architecture.E001`.

| `ARCHITECTURE` key | Default | Meaning |
| --- | --- | --- |
| `DOMAIN_ALLOWED` | `()` | More packages a domain layer may import, such as a shared kernel (`"myproject.kernel"`). Up to 64 dotted names. |
| `FORBIDDEN_LIBRARIES` | `{}` | More libraries a layer must not import, by layer: `{"application": ("httpx",)}`. Not the domain layer, which is an allowlist: use `DOMAIN_ALLOWED`. Up to 12 layers, each with up to 64 names. |
| `EXTRA_LAYERS` | `()` | Layer packages of the project's own, such as `ports`, so the two tables here can name them. Up to 8, none a built-in layer. |
| `FORBIDDEN_LAYERS` | `{}` | More layers of the same domain a layer must not import: `{"ports": ("infrastructure", "api")}`. Every layer named must be built in or in `EXTRA_LAYERS`, and none may forbid itself. Up to 12 layers, each forbidding up to 12. |
| `PUBLIC_SURFACE` | `()` | More parts of a domain that other domains may import, each starting with a layer: `("domain.values",)`. |

```python
ARCHITECTURE = {
    "DOMAIN_ALLOWED": ("myproject.kernel",),
    "FORBIDDEN_LIBRARIES": {"application": ("httpx",)},
}
```

Tests check a domain the same way. `check_domain("billing")` applies the
settings' rules, and `check_domain(..., rules=LayerRules().extend(...))`
applies others.

## Languages and time zones

See [i18n.md](../i18n.md) for translations.

| Setting | Default | Meaning |
| --- | --- | --- |
| `LANGUAGE_CODE` | `"en"` | The language of responses when `Accept-Language` asks for none of `LANGUAGES`, and outside requests. It must be one of them. |
| `LANGUAGES` | `(("en", "English"), ("ar", "Arabic"))` | `(code, name)` of each language responses may be in; `LocaleMiddleware` picks one per request. Codes are lower case (`ar`, `pt-br`). |
| `LOCALE_PATHS` | `()` | Directories of `<locale>/LC_MESSAGES/messages.mo` catalogs, read before each installed domain's `locale` directory, then domainkit's own. |
| `TIME_ZONE` | `"UTC"` | The zone dates are shown in (`localtime`, `localdate`, and the date formats). Dates are stored and compared in UTC, whatever it says. |

```python
LANGUAGE_CODE = "en"
LANGUAGES = [("en", "English"), ("ar", "Arabic"), ("fr", "French")]
TIME_ZONE = "Africa/Cairo"
```

## Debug toolbar

`DebugToolbarMiddleware` (`domainkit.debug`) shows what each request cost
the database, while `DEBUG` is on. The local settings that `init` creates
list it last in `MIDDLEWARE`. Listed right after
`RequestContextMiddleware`, it also counts the queries that authenticate
the request. Staging and production refuse to start with it listed
(`security.E002`).

Every response gets a `Server-Timing` header, which browsers show in
their network panel:

```text
Server-Timing: db;dur=12.3;desc="7 queries", app;dur=45.6
```

A statement run `REPEATED_QUERIES` times or more in one request is logged
as a likely N+1, by its shape: the SQL with every value and placeholder
replaced by `?`. Bound values are never read.

The toolbar counts statements as `assert_num_queries` does
([testing.md](../testing.md#counting-queries)), but only the request's
own, so concurrent requests do not mix. Find a request's queries with the
toolbar, then keep them in check with `assert_max_queries` in a test.

| `DEBUG_TOOLBAR` key | Default | Meaning |
| --- | --- | --- |
| `REPEATED_QUERIES` | `5` | Runs of one statement shape in one request that log a likely N+1 (2 or more). |
| `HEADERS` | `False` | Also sends `X-Debug-Queries`, `X-Debug-Query-Time` (milliseconds), and `X-Debug-Repeated-Queries`. |
| `PROFILE_DIR` | `None` | A directory. A request sent with `X-Debug-Profile: 1` leaves a cProfile file there, named in the response's `X-Debug-Profile` (one request at a time; read it with `python -m pstats`). |

```python
DEBUG_TOOLBAR = {
    "REPEATED_QUERIES": 3,
    "HEADERS": True,
    "PROFILE_DIR": BASE_DIR / "profiles",
}
```

## Common mistakes

- **A misspelled name.** domainkit refuses names it does not know, and
  suggests the nearest one. Fix the spelling; do not silence it.
- **A domain's setting that `setup()` does not find.** A domain's
  settings are validated only when the domain is in `INSTALLED_DOMAINS`.
  Install the domain, or remove its settings.
- **`SECRET_KEY = ""`.** An empty key is refused. Leave it `None` in
  development, and set a random key of 50 characters or more where you
  deploy.
- **A password with `@` or `/` in a URL.** Percent-encode it: `p@ss`
  becomes `p%40ss`.

## See also

- [backends.md](backends.md) and [http.md](http.md): the other settings
  tables.
- [Checks](checks.md): what staging and production refuse.
- [Commands](commands.md#diffsettings-show-the-settings): `diffsettings`.
- [Deploying](../deploying.md): settings for production.
