# Settings

A settings module is a Python module of UPPERCASE names, selected with
`<PREFIX>SETTINGS_MODULE`. The prefix comes from `[project].name` in
`pyproject.toml` (`billingAPI` gives `BILLING_API_`), or from
`[tool.domainkit] env-prefix`. `domainkit.conf.Settings` validates every name
at startup. A name the framework does not declare must belong to an installed
domain's `settings_class`, or to a project schema named by `SETTINGS_CLASS`.
`registry.settings(SettingsClass)` returns a domain's validated settings;
it validates them once per settings object, and again after
`override_settings`, `use_settings`, `configure` or `reset_settings`
replaces it, or `registry.populate` or `clear` changes the domains.

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

This page has the settings of the framework itself. Those of databases,
caches, storages, and brokers are on [backends.md](backends.md), those of
HTTP, outgoing calls, and live connections on [http.md](http.md); each
optional app's are on its own page ([contrib.md](../contrib.md)).

## Core

| Setting | Default | Meaning |
| --- | --- | --- |
| `SETTINGS_MODULE` | set by the loader | The module these settings came from. |
| `BASE_DIR` | the project root | The directory holding `pyproject.toml`. |
| `ENVIRONMENT` | `"local"` | `local`, `test`, `staging`, or `production`. Staging and production get the [deployment checks](checks.md). |
| `SILENCED_CHECKS` | `()` | Ids of warnings `check` stops reporting, as `security.W001` ([deployment checks](checks.md)); at most 64, each once. Errors cannot be silenced. |
| `DEBUG` | `False` | Development conveniences; refused in staging and production. |
| `DEBUG_TOOLBAR` | see [Debug toolbar](#debug-toolbar) | The options of `DebugToolbarMiddleware`, which development settings list in `MIDDLEWARE`. |
| `SLOW_CALLBACK_DURATION` | `0.1` | With `DEBUG`, the application's loop runs in asyncio's debug mode and logs each step that holds it this many seconds or more ([runtime.md](../runtime.md#finding-blocking-code)); `None` turns it off. |
| `PROJECT_NAME` | `"API"` | The OpenAPI title. |
| `VERSION` | `"0.1.0"` | The OpenAPI version. |
| `SECRET_KEY` | `None` | Signs what the service hands out: reset links, cookies, MFA challenges ([Signing](signing.md)). Required in staging and production, with 50 random characters or more; never empty (leave it `None` instead); shown as `**********`. |
| `SECRET_KEY_FALLBACKS` | `()` | Earlier keys: what they signed still verifies while `SECRET_KEY` signs, so a key can be replaced without logging anyone out. Held to the same rules. |
| `INSTALLED_DOMAINS` | `()` | Domain packages (Django's `INSTALLED_APPS`), started in order and stopped in reverse. |
| `ARCHITECTURE` | none added | What the project adds to the layer rules `check` enforces: packages its domain layers may import, libraries and layers a layer must not use, layers of its own ([Layer rules](#layer-rules)). |
| `MIDDLEWARE` | see [Middleware](http.md#middleware) | ASGI middleware by dotted path, outermost first. |
| `ROOT_ROUTER` | `None` | Dotted path to the `APIRouter` that mounts every route (Django's `ROOT_URLCONF`). |
| `FLATTEN_ROUTERS` | `True` | Serve the routes of the routers `ROOT_ROUTER` includes from the top of the application, each matched once ([Included routers](responses.md#included-routers)); `False` keeps FastAPI's nesting, which matches a request again at each level. |
| `REQUIRE_ROUTE_RULES` | `True` | Every route must say who may call it (`Require`, a dependency that reads the principal, or `Public`), whatever authenticates requests; `check` lists those that do not, and staging and production refuse to start ([Deny by default](../auth/routes.md#deny-by-default)). `AUTH.REQUIRE_ROUTE_RULES`, its name until 0.10, is refused since 0.11. |
| `SCOPED_CREDENTIALS` | `"refuse"` | Credentials limited to scopes on a route that declares no rule that checks scopes (`HasScope`, a permission rule, `IsUnscoped`, or `AllowScoped`): `"refuse"` answers 403; `"warn"` lets them through, as 0.10 did by default, with a security log line once per route a while ([Scopes on every route](../auth/authentication.md#scopes-on-every-route)). |
| `ASGI_APPLICATION` | `None` | `module:attribute` that `runserver` serves. |
| `EVENT_LOOP` | `"auto"` | The event loop of `runserver` and of commands with an `async` handle (`runworker` among them): uvloop when the `uvloop` extra is installed (`auto`), always asyncio's own (`asyncio`), or uvloop or a configuration error (`uvloop`). |
| `LOGGING` | minimal | A `logging.config.dictConfig` mapping, applied by `setup()`. `domainkit.log.build_logging_config(log_format="json", level="INFO")` makes one that writes from a thread, through a bounded queue ([runtime.md](../runtime.md#logging-and-error-reports)); the settings `init` writes use it. |
| `ERROR_REPORTERS` | `()` | Callables `(exc, context) -> None` by dotted path, called for every unexpected error (500s, dead-lettered messages), on a reporting thread and in the caller's context; 100 reports may wait, and more are dropped and counted. A reporter that raises is logged and skipped. |
| `EVENT_HANDLER_TIMEOUT` | `10.0` | Seconds an in-process event handler may run after a commit, up to 3600; one still running then is cancelled, logged, and reported (`HandlerTimedOut`), and the request goes on. `event_bus.subscribe(..., timeout=30)` sets one handler's limit; `durable=True` runs a handler from the outbox instead ([runtime.md](../runtime.md#event-handlers)). |
| `SHUTDOWN_TIMEOUT` | `15.0` | Seconds a shutdown may take in all, from 0 to 300: domain hooks, work in flight, background tasks, then connections, each phase within its share ([runtime.md](../runtime.md#shutting-down-within-a-budget)). Add the server's graceful shutdown (`uvicorn --timeout-graceful-shutdown 10`) and keep the sum under the orchestrator's grace period: 30 s in Kubernetes. |

## Layer rules

`check` holds every installed domain to `domainkit.architecture`'s rules
([ADR 0003](../adr/0003-enforced-layer-rules.md)): `api → infrastructure →
application → domain`; the domain layer imports only the standard library,
`domainkit.ddd`, `domainkit.clock`, `domainkit.money`, and itself; the application layer uses
no web or database library; another domain is reached only through its
`application` or `domain.events`. `ARCHITECTURE` adds to these rules, and
only adds: a rule the framework states always holds.

| `ARCHITECTURE` key | Default | Meaning |
| --- | --- | --- |
| `DOMAIN_ALLOWED` | `()` | More packages a domain layer may import, such as a shared kernel (`"myproject.kernel"`). Up to 64 dotted names. |
| `FORBIDDEN_LIBRARIES` | `{}` | More libraries a layer must not import, by layer: `{"application": ("httpx",)}`. Not the domain layer, which is an allowlist: use `DOMAIN_ALLOWED`. Up to 12 layers, each with up to 64 names. |
| `EXTRA_LAYERS` | `()` | Layer packages of the project's own, such as `ports`, so the two tables here can name them. Up to 8, none a built-in layer. |
| `FORBIDDEN_LAYERS` | `{}` | More layers of the same domain a layer must not import: `{"ports": ("infrastructure", "api")}`. Every layer named must be built in or in `EXTRA_LAYERS`, and none may forbid itself. Up to 12 layers, each forbidding up to 12. |
| `PUBLIC_SURFACE` | `()` | More parts of a domain other domains may import, each starting with a layer: `("domain.values",)`. |

```python
ARCHITECTURE = {
    "DOMAIN_ALLOWED": ("myproject.kernel",),
    "FORBIDDEN_LIBRARIES": {"application": ("httpx",)},
}
```

Tests check a domain the same way: `check_domain("billing")` applies the
settings' rules, and `check_domain(..., rules=LayerRules().extend(...))`
others.

## Languages and time zones

See [i18n.md](../i18n.md).

| Setting | Default | Meaning |
| --- | --- | --- |
| `LANGUAGE_CODE` | `"en"` | The language of responses when `Accept-Language` asks for none of `LANGUAGES`, and outside requests. It must be one of them. |
| `LANGUAGES` | `(("en", "English"), ("ar", "Arabic"))` | `(code, name)` of each language responses may be in; `LocaleMiddleware` picks one per request. Codes are lower case (`ar`, `pt-br`). |
| `LOCALE_PATHS` | `()` | Directories of `<locale>/LC_MESSAGES/messages.mo` catalogs, looked up before each installed domain's `locale` directory, then domainkit's own. |
| `TIME_ZONE` | `"UTC"` | The zone dates are shown in (`localtime`, `localdate`, and the date formats). Dates are stored and compared in UTC whatever it says. |

## Debug toolbar

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
([testing.md](../testing.md#counting-queries)), but only the request's own,
so concurrent requests do not mix. Find a request's queries with the
toolbar, then keep them in check with `assert_max_queries` in a test.

| `DEBUG_TOOLBAR` key | Default | Meaning |
| --- | --- | --- |
| `REPEATED_QUERIES` | `5` | Runs of one statement shape in one request that log a likely N+1 (2 or more). |
| `HEADERS` | `False` | Also send `X-Debug-Queries`, `X-Debug-Query-Time` (milliseconds), and `X-Debug-Repeated-Queries`. |
| `PROFILE_DIR` | `None` | A directory: a request sent with `X-Debug-Profile: 1` leaves a cProfile file there, named in the response's `X-Debug-Profile` (one request at a time; read it with `python -m pstats`). |
