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
