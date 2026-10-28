# Upgrading

How to move a project to a new minor version, and what each one asks of
it. A minor version may change a default or break a behaviour, each listed
here with the setting that keeps the old one, and it removes only names
the minor before deprecated ([versioning.md](versioning.md)). Patch
releases change nothing a project relies on: take them as they come.

## How to upgrade

Upgrade one minor at a time, from its newest patch release (0.9.2, then
0.10.x):

1. Read the section below for the version you move to.
2. Raise the version in the project's `pyproject.toml`, and install.
3. Run the tests with deprecations as errors,
   `pytest -W error::DeprecationWarning`, so each deprecated name the
   project still uses fails, naming its replacement. Replace them all:
   they are removed in the next minor.
4. `python manage.py makemigrations`: a contrib app's tables may have
   changed. Review the revision, then `migrate` it in a test database.
5. `python manage.py check` with the production settings and variables,
   for the deployment checks this version adds.
6. Deploy as usual: [operations.md](operations.md#migrations-in-a-rollout).

## 0.11 to 0.12

### Breaking changes

Each changes what a project gets without changing its code. Each says how
to keep the old behaviour, for as long as the project needs it.

- **A hook registered with the wrong arguments fails `setup()`**
  (DSGN-03). The points of `domainkit.db.hooks` (`on_stage`, ...) check a
  function when it registers: it must take the arguments the point calls
  it with, and be an `async` function. 0.11 accepted it and failed the
  first commit or `migrate` that called it. **Fix:** give the hook the
  point's arguments (`async def hook(uow, events, tasks)` for `on_stage`).
  A `HookPoint` of a project's own checks nothing unless it is made with
  `arguments=` or `awaited=`.
- **`dumpdata` leaves guarded tables out** (DATA-12). A dump of every
  table leaves out users and their password hashes, groups, permissions,
  content types, object grants, and dynamic settings, and says so on
  standard error; naming one, or a domain that holds one, is refused.
  **Fix:** dump them on purpose: `dumpdata --include-guarded`, which is
  recorded in the audit trail and, in staging and production, needs
  `--operator` or `DOMAINKIT_OPERATOR`. **To keep the old behaviour:**
  `--include-guarded` on every dump that should hold them.
- **Test tokens need the test settings** (SEC-12a-2). `user_token`,
  `token_for`, and the clients' `authenticated` and `authenticated_as`
  (`client_for`, `admin_client`) raise `ImproperlyConfigured` unless
  `ENVIRONMENT = "test"`: they sign users in without credentials.
  **Fix:** run the code that uses them with the test settings, as the
  database fixtures already require. There is no setting to keep the old
  behaviour.
- **Fixtures load only password hashes the configured hashers would
  make** (SEC-12a-3). A user's hash that no hasher of
  `AUTH.PASSWORD_HASHERS` identifies, or that it would make again more
  strongly (fewer iterations, a shorter salt), refuses the fixture.
  **Fix:** dump and load users between environments with the same
  hashers, or write unusable passwords (`!...`) in the fixture and reset
  them after loading. There is no setting to keep the old behaviour.

### Deprecated, removed in 0.13

Each still works in 0.12, with a `DomainkitDeprecationWarning` that names
its replacement.

| Deprecated | Use instead | Audit id |
| --- | --- | --- |
| Overriding a hook method of `DomainConfig`: `startup`, `shutdown`, `check`, `seed`, `erase_subject`, `export_subject`, `check_application`, `deployment_warnings` | A function registered in `ready()` on the point of the same name: `domainkit.apps.hooks.on_startup(...)`, `on_shutdown`, `on_check`, `on_seed`, `on_erase_subject`, `on_export_subject`, `on_check_application`, `on_deployment_warnings`. The warning names the domain and the point, at the method's line | DSGN-03 |
| Overriding `stage`, `committed`, `stage_transaction`, `transaction_committed`, or `migrated` (0.10 added the points, without a warning) | `domainkit.db.hooks.on_stage(...)`, `on_committed`, `on_stage_transaction`, `on_transaction_committed`, `on_migrated` in `ready()` | DSGN-03 |
| `with expect_version(Order, order_id, versions):` (`domainkit.ddd.preconditions`), which every unit of work of the context honoured | `with uow.expect(Order, order_id, versions):` on the unit of work the service changes the aggregate through | DSGN-14 |

`DomainConfig.databases` and `holds_table` stay methods: they answer where
a domain's tables are ([ADR 0101](adr/0101-register-every-domain-hook-and-check-it-when-it-registers.md)).
A method moves like this:

```python
# 0.11
class SearchConfig(DomainConfig):
    async def startup(self) -> None:
        await index.connect()


# 0.12
from domainkit.apps.hooks import on_startup


class SearchConfig(DomainConfig):
    def ready(self) -> None:
        on_startup(index.connect)
```

## 0.10 to 0.11

### Breaking changes

Each changes what a project gets without changing its code. Each says how
to keep the old behaviour, for as long as the project needs it.

- **Scoped credentials need a rule that checks scopes** (AUTH-01).
  `SCOPED_CREDENTIALS` defaults to `"refuse"`: credentials limited to
  scopes, on a route that declares no rule that checks scopes, get `403
  permission_denied`, where 0.10 served them with a warning naming the
  route. **Fix:** add `HasScope(...)`, a permission rule, or
  `AllowScoped()` to the route, or to a router it is included through.
  **To keep the old behaviour:** `SCOPED_CREDENTIALS = "warn"`, which
  serves them with the security log line, without the deprecation
  warning.
- **Anonymous idempotency keys are refused** (DATA-09).
  `IDEMPOTENCY.ANONYMOUS` defaults to `"refuse"`: an `Idempotency-Key`
  sent with no actor and none of `CREDENTIAL_HEADERS` gets 400
  `idempotency_key_needs_caller`, and the request does not run. A
  credential header in which authentication found no one
  (`Authorization: Bearer null`, a gateway's fixed `X-API-Key`) names no
  caller either (SEC-11a-1). **Fix:**
  the client authenticates, or sends the request without the key (a
  login or a sign-up, which has no caller yet, sends none).
  **To keep the old behaviour:** `IDEMPOTENCY = {"ANONYMOUS":
  "per-address"}`, which scopes such keys to the caller's address, shared
  by every caller behind one NAT or proxy. `check` no longer warns
  `idempotency.W002`: take it out of `SILENCED_CHECKS` if it is there.
- **`crud=True` needs the fields it writes** (WEB-09).
  `ResourceRouter(crud=True)` without `writable=`, or without both
  `create=` and `update=`, raises `TypeError` when the router is built,
  where 0.10 warned. **Fix:** `writable=[...]` naming the fields the
  writes take, or both schemas. **To keep the old behaviour:**
  `writable=ALL_FIELDS`, which takes every plain field.
- **`loaddata` only adds rows** (DATA-12). A fixture with a row whose
  primary key is stored is refused, and nothing is loaded. **To keep the
  old behaviour:** `loaddata --replace`.
- **Guarded tables** (DATA-12). `loaddata` refuses a fixture that writes
  users, groups, permissions or their links, content types, object
  grants, or dynamic settings, and `flush` keeps those tables, with
  tokens, TOTP authenticators, and recovery codes. **To keep the old
  behaviour:** `--include-guarded` on either command.
- **A named operator for `loaddata` and `flush` where deployed**
  (DATA-12). In staging and production the login name no longer names the
  operator of these two commands. **Fix:** `--operator NAME`, or
  `DOMAINKIT_OPERATOR` set where they run.
- **Request ids from trusted proxies only** (WEB-13). A caller's own
  `X-Request-ID` is dropped, and its request gets a new id. **Fix:** have
  the proxy listed in `TRUSTED_PROXIES` set the header, overwriting a
  client's (nginx: `proxy_set_header X-Request-ID $request_id;`), or set
  `ACCEPT_REQUEST_ID = "never"` if it passes headers through as they
  came. **To keep the old behaviour:** `ACCEPT_REQUEST_ID = "anyone"`.
- **Attempts counted in each process refuse to start** (AUTH-09). With
  `domainkit.contrib.auth` installed, staging and production refuse to
  start, and `check` fails (`auth.E105`, which `SILENCED_CHECKS` cannot
  name), while `RATE_LIMIT_BACKEND` is a `MemoryBackend`, the default:
  each process would allow `LOGIN_RATE` on its own. **Fix:**
  `RATE_LIMIT_BACKEND = "domainkit.http.ratelimit.RedisBackend"`, with
  `RATE_LIMIT_CACHE` naming a Redis cache every process shares. **To keep
  the old behaviour** (one process): `AUTH = {"REQUIRE_SHARED_THROTTLES":
  False}`, which warns (`auth.W004`).
- **Attempts that cannot be counted are refused** (AUTH-09). In staging
  and production, while Redis is down, logins, second-factor codes,
  password changes, and requests that send mail answer `503
  throttle_unavailable` ([runbook.md](runbook.md#logins-are-refused-while-redis-is-down)).
  **To keep the old behaviour:** `AUTH = {"THROTTLE_FAIL_CLOSED": False}`.
- **Committed idempotency keys outlive their TTL** (FIN-84). With
  `domainkit.contrib.idempotency` installed, a key whose request committed
  (or pinned it for a commit on another database) is kept as a tombstone
  for `IDEMPOTENCY.TOMBSTONE_SECONDS` (7 days) once `TTL_SECONDS` passes,
  without its response: a retry with the same key and request gets 409
  `idempotency_response_unavailable` instead of running again, and another
  request with the key gets 422 `idempotency_key_reused`. **Fix:** run
  `makemigrations` and `migrate` for the keys table's new `tombstone`
  column before the new version serves requests; clients that reuse a key
  on purpose send a new one. **To keep the old behaviour:**
  `IDEMPOTENCY = {"TOMBSTONE_SECONDS": 0}`.
- **Messaging's new table, and stale commits refused** (FIN-80).
  Messaging has an inbox table, `domainkit_inbox`, in every database that
  holds an outbox: projects that install messaging run `makemigrations`
  and `migrate` before deploying, or each worker's cleanup logs a failure
  every round. A unit of work that a durable handler or task commits on
  its outbox's database, after another worker took its message over (the
  run outlived its lease), now raises `LeaseLost` and rolls back; the
  worker holding the message runs it. Before, both runs committed. **Fix:**
  let `LeaseLost` propagate from handlers that catch errors around their
  commit. Nothing keeps the old behaviour: it committed the work twice.

### Removed in 0.11

Each was deprecated in 0.10, with a warning that named its replacement.

| Removed | Use instead | Audit id |
| --- | --- | --- |
| `from domainkit.contrib.auth import IsAuthenticated` (and `AllowAny`, `BasePermission`, `HasObjectPermissions`, `HasPermissions`, `HasScope`, `IsAuthenticatedOrReadOnly`, `IsUnscoped`, `ModelPermissions`, `NotAuthenticated`, `Public`, `Require`, `check_object_permissions`, `mark_public`, `open_routes`) raises `ImportError` | `from domainkit.http.access import ...`, the same objects | DSGN-02 |
| The same names from `domainkit.contrib.auth.access`, and `AllOf`, `AnyOf`, `Not`, `Rule`, `SAFE_METHODS`, `MODEL_ACTIONS`, `bearer`, `public`, `scopes_of`, `routes_need_users`, `unguarded_routes` | `domainkit.http.access`; `IsAdminUser`, `IsSuperuser`, `AuthenticatedUser`, `CurrentUser`, and `current_user` stay in `domainkit.contrib.auth` | DSGN-02 |
| `AUTH.REQUIRE_ROUTE_RULES`: settings that set it are refused when they load | `REQUIRE_ROUTE_RULES` | DSGN-02 |
| `Query` from `domainkit.ddd.query` | `domainkit.ddd.Selection` | DSGN-09 |
| `domainkit.contrib.evidence.model` | `domainkit.contrib.evidence` (or its `models`) | DSGN-09 |

## 0.9 to 0.10

### Breaking changes

Each changes what a project gets without changing its code. Each says how
to keep the old behaviour, for as long as the project needs it.

- **Deny by default without `contrib.auth`** (DSGN-02). A project that
  authenticates its own way, without `domainkit.contrib.auth`, now has deny
  by default too: a route that declares no rule (`Require`, `CurrentUser`,
  a `reads_principal` dependency, or `Public`) is listed by `check`, and
  staging and production refuse to start. **Fix:** give each route its
  rule, or `dependencies=[Public]` for a route anyone may call. **To keep
  the old behaviour:** `REQUIRE_ROUTE_RULES = False`.

- **API docs off in deployments** (WEB-08). `API_DOCS` defaults to
  `None`, which follows `DEBUG`: with `DEBUG = False`, `/docs`, `/redoc`,
  and `/openapi.json` are not served. **To keep them:** `API_DOCS = True`.
- **Security headers on every response** (WEB-08). A response that sets
  none gets `Content-Security-Policy: default-src 'none'; frame-ancestors
  'none'`, a `Permissions-Policy` that turns powerful browser features off,
  and `Cross-Origin-Opener-Policy: same-origin`. **Fix:** a route that
  answers HTML for a browser sets its own policy. **To keep the old
  headers:** `SECURE_CONTENT_SECURITY_POLICY = None` (and `None` for
  `SECURE_PERMISSIONS_POLICY` and `SECURE_CROSS_ORIGIN_OPENER_POLICY`).
- **Route templates in logs** (WEB-10). The access log, error lines,
  error reports, and the request context show the matched route's template
  (`/accounts/{account_id}`) or `unmatched`, not the path as sent. **To
  keep the old lines:** `ACCESS_LOG_PATH = "raw"`.
- **Queued log records carry no exception object** (CONC-04). A record
  behind `BackgroundHandler` has its traceback formatted into `exc_text`
  and no `exc_info`; every formatter writes the same text. **Fix:** a
  handler that needs the exception object goes on the logger itself, not
  behind the queue.
- **A browser login ends the session its cookie held** (AUTH-05). **To
  keep the old session working:** `AUTH.COOKIE.END_PREVIOUS_SESSION =
  False`.
- **At most 100 logins a user** (AUTH-08). A login past
  `AUTH.MAX_LOGINS` ends the oldest; personal tokens neither count nor
  end. **To keep the old behaviour:** `AUTH.MAX_LOGINS = 10000`.
- **Device proofs** (SEC-r1-2). `/login` and `/session` set the
  `__Host-dk_device` cookie, and a client that sends it is counted apart
  from its address. **To keep the old behaviour:** `AUTH.DEVICE_TTL =
  None`.
- **A rolled-back or forked ledger is refused** (DATA-05). With
  `AUDIT.LEDGER` set, an immudb ledger behind the state a client last
  verified raises `LedgerRolledBack`, and a forked one `LedgerTampered`, on
  every call and on `/ready`, after restarts too. **Fix:** after restoring
  a ledger from a backup on purpose, delete its row from
  `domainkit_ledger_states` once the restore is checked. Projects with the
  audit app run `makemigrations` and `migrate` for the new tables.

- **The audit trail's baseline refuses by default** (DATA-04,
  SEC-10b-8). With `domainkit.contrib.audit` installed, staging and
  production refuse to start without `AUDIT.LEDGER`, `AUDIT.NAMESPACE`, a
  sealing schedule (messaging's worker, or `AUDIT.SEALED_EXTERNALLY`), and
  the `protect()` triggers; `check` fails with `audit.E001` to `audit.E003`,
  and `SILENCED_CHECKS` cannot name them. **Fix:** set the four
  ([contrib/audit.md](contrib/audit.md#in-staging-and-production)). **To
  keep the old behaviour:** `AUDIT = {"REQUIRE_BASELINE": False}`, which
  warns as 0.9 did.
- **Keyed requests need a deadline** (SEC-r2-4, SEC-10b-8). With
  `domainkit.contrib.idempotency` installed, staging and production refuse
  to start, and `check` fails (`idempotency.E002`), when `REQUEST_TIMEOUT`
  is `None` or `DeadlineMiddleware` is not in `MIDDLEWARE`. **Fix:** set
  `REQUEST_TIMEOUT` below `IDEMPOTENCY.LEASE_SECONDS` and list the
  middleware, as `init`'s settings do. **To keep the old behaviour:**
  `IDEMPOTENCY = {"REQUIRE_DEADLINE": False}`, which warns as 0.9 did.

### Deprecated, removed in 0.11

Each still works in 0.10, with a `DomainkitDeprecationWarning` that names
its replacement.

| Deprecated | Use instead | Audit id |
| --- | --- | --- |
| `from domainkit.contrib.auth import IsAuthenticated` (and `AllowAny`, `BasePermission`, `HasObjectPermissions`, `HasPermissions`, `HasScope`, `IsAuthenticatedOrReadOnly`, `IsUnscoped`, `ModelPermissions`, `NotAuthenticated`, `Public`, `Require`, `check_object_permissions`, `mark_public`, `open_routes`) | `from domainkit.http.access import ...`, the same objects | DSGN-02 |
| `domainkit.contrib.auth.access` (all of it: the names above, and `AllOf`, `AnyOf`, `Not`, `Rule`, `SAFE_METHODS`, `MODEL_ACTIONS`, `scopes_of`) | `domainkit.http.access`; `IsAdminUser`, `IsSuperuser`, `AuthenticatedUser`, `CurrentUser`, and `current_user` stay in `domainkit.contrib.auth` | DSGN-02 |
| `AUTH.REQUIRE_ROUTE_RULES` | `REQUIRE_ROUTE_RULES` | DSGN-02 |
| `ResourceRouter(crud=True)` without `writable=`, or without both `create=` and `update=` | `writable=[...]` naming the fields, or `writable=ALL_FIELDS` | WEB-09 |
| `Query` from `domainkit.ddd.query` | `domainkit.ddd.Selection` | DSGN-09 |
| `domainkit.contrib.evidence.model` | `domainkit.contrib.evidence` (or its `models`) | DSGN-09 |
| Scoped credentials on a route that declares no rule that checks scopes (`SCOPED_CREDENTIALS = "warn"`) | `HasScope(...)`, a permission rule, or `AllowScoped()` on the route; `SCOPED_CREDENTIALS = "refuse"` refuses them now | AUTH-01 |
| `IDEMPOTENCY.ANONYMOUS` left unset (`"per-address"` by default; `check` warns `idempotency.W002` in staging and production) | `IDEMPOTENCY = {"ANONYMOUS": "per-address"}` to keep it, or `"refuse"`, 0.11's default, now | DATA-09 |

The whole list, with the dates, is in
[versioning.md](versioning.md#deprecations-planned-for-011), and every
change in the [CHANGELOG](../CHANGELOG.md).
