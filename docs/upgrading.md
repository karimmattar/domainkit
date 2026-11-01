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

## 0.13 to 0.14

### Breaking changes

- **Accounts may not go negative unless opened so** (FIN-97).
  `may_go_negative` defaults to `False` (`Account.open`, `OpenAccount`,
  `POST /accounts`): an account opened without it refuses what would
  leave less than nothing available. **Fix:** open clearing, settlement,
  and external accounts with `may_go_negative=True`; those already stored
  keep the flag they were opened with.
- **A currency a threshold or a limit does not name is refused**
  (FIN-89). With `APPROVALS.ENTRY_THRESHOLDS` set, an entry in another
  currency needs approval; a subject bounded by limits in some currency
  is refused (`currency_not_limited`) a movement in another. **Fix:**
  name every currency the ledger or the project moves, or give an amount
  for all of them: `"*": "10000"` in `ENTRY_THRESHOLDS`, `"currency": "*"`
  in a default of `LIMITS.DEFAULTS`.
- **The books are closed up to the latest closed period's end** (FIN-87).
  No entry is dated on or before it, in a period or in a gap, whatever
  `REQUIRE_PERIOD` says; no period is opened there; and a period closes
  only once those before it are closed (`earlier_period_open`). **Fix:**
  close periods in date order; date corrections in an open period, as
  reversals.
- **Reversals over the thresholds need approval** (FIN-88). With
  `ENTRY_THRESHOLDS`, reversing an entry over them is refused
  (`approval_required`): submit `accounting.reverse_entry` with
  `{"entry_id": ..., "booked_on": ...}`; its makers need
  `accounting.reverse_entry`. An approved reversal is counted against
  limits and frees nothing. **Fix:** route large corrections through
  approvals.
- **Limits keep the movements of bounded subjects only** (PERF-26,
  revising SEC-15b-5). A
  subject no window bounds keeps no usage; a limit set, changed, or
  enabled mid-window counts the ledger's postings since its start, for
  accounts and holders. An actor's movements, and those a project counts
  with `consume` outside the ledger, count from when a window bounds
  them. **Fix:** set limits before their window starts where it matters
  for actors and a project's own kinds; old usage rows of unbounded
  subjects age out with `KEEP_USAGE`.
- **`EntryPosted` has `amounts` and `digest`** (FIN-96), both required.
  **Fix:** code that builds an `EntryPosted` itself passes them; the
  ledger does.
- **Erasure keeps the books and the approvals, pseudonymised** (FIN-94).
  `privacy erase` is held (`ErasureHeld`; the command names why) while
  the person holds an open account with money on it or held; done, their
  accounts name one `erased:` holder instead of none, and their approval
  requests keep their commands, sealed again for the pseudonym. **Fix:**
  close a customer's accounts before erasing them; register
  `on_erase_check` in a domain whose records a law asks to keep.
- **Every 429 answers `rate_limited`** (WEB-25). A route's `Throttle` and
  auth's code, link, and signup throttles answered `throttled`, and its
  login and password throttles `login_throttled`. **Fix:** a client that
  switched on either switches on `rate_limited`, or on the status, and
  waits as `Retry-After` says. Every code is listed in
  [reference/problems.md](reference/problems.md).
- **A live connection's lifetime is at most a day** (WEB-24):
  `WEBSOCKET_MAX_LIFETIME`, `SSE_MAX_LIFETIME`, and a route's
  `LiveLimits(max_lifetime=...)` above 86,400 seconds are refused at
  startup. **Fix:** lower them; clients reconnect. A route that turns
  re-authorization off (`LiveLimits(reauth_interval=None)`) is warned
  about where deployed (`live.W002`).
- **The `settings` fixture reads settings read-only** (TEST-19): a test
  that changed a mapping in place (`settings.CACHES["x"] = ...`) gets
  `TypeError`. **Fix:** assign the whole setting,
  `settings.CACHES = {**settings.CACHES, "x": {...}}`, which is validated
  and undone when the test ends.
- **SQLite checks foreign keys** (DATA-18). Every connection runs
  `PRAGMA foreign_keys = ON`: a row naming no parent is refused, as on
  PostgreSQL, and `ON DELETE CASCADE` deletes children. `migrate` checks
  the rows already stored and refuses, naming the tables, rows that name
  a missing parent. **Fix:** delete or repair those rows (`PRAGMA
  foreign_key_check` lists them), then migrate again; a test that stored
  a child without its parent stores the parent first.
- **Derived schemas leave `Encrypted` fields out** (DSGN-19). `schema_for`
  without `include`, `crud=True` with `writable=ALL_FIELDS`, and the admin
  no longer show or take a field stored encrypted. **Fix:** name it where
  it should be served: `schema_for(Card, include=["id", "number"])`,
  `writable=[..., "number"]`, or the admin's `schema=`; a create body that
  needs it names it in `writable`.
- **Revisions are rendered in batch mode** (DATA-13), on PostgreSQL too,
  so they apply on SQLite. Nothing to do; a revision written before is
  unchanged.
- **A missing money check is added** (DATA-20). `makemigrations` writes the
  both-or-neither check of every nullable money pair the database lacks,
  also of a pair made optional, or added before 0.14. **Fix:** run
  `makemigrations` and `migrate`; a row holding half a pair fails the
  check: repair it first.
- **A route that only reads `CurrentUser` declares no rule** (DSGN-17).
  Staging and production refuse to start with one (`security.E101`), and
  `check` lists it. **Fix:** add `dependencies=[Public]` to a route that
  anyone may call, or take `AuthenticatedUser` where a login is meant.
- **Making a personal token asks the user to authenticate again, and the
  token ends** (AUTH-16). `POST /auth/tokens` needs a step-up within 5
  minutes from users with a second factor, and the `password` in the body
  from the others; tokens work `AUTH.PERSONAL_TOKEN_MAX_AGE` (90 days) at
  most, and `expires_in_days: null` is refused; `login` is no longer a
  token's name. **Fix:** clients send the password, or step up first;
  `AUTH = {"PERSONAL_TOKEN_MAX_AGE": None}` keeps tokens that never end.
  Tokens already made keep their lifetime.
- **An account takes 100 wrong passwords a day at most** (AUTH-19), from
  `AUTH.ACCOUNT_LOGIN_RATE`, beside `LOGIN_RATE`. A user whose account a
  stranger floods logs in from the browsers they used before; a new one
  waits for the day, or a password reset. **Fix:** nothing, or set the
  rate (`None` restores 0.15's, and `check` warns of it where deployed,
  `auth.W006`).
- **`If-Match: *` no longer satisfies `require_if_match`** (WEB-22): it is
  `428 precondition_required`, as a write without `If-Match`. **Fix:**
  clients send the `ETag` of the version they change.
- **Turning a second factor on ends the user's other logins and tokens**
  (AUTH-20), personal tokens and registered certificates
  included. **Fix:** make personal tokens, and register certificates,
  after turning it on.

- **An open live connection no longer keeps its login alive**
  (SEC-15c-2). With `AUTH.IDLE_TIMEOUT`, the re-checks of an open
  WebSocket or event stream wrote the login's last use, so a page left
  open kept its login for good; they write none now, and the connection
  closes once its login idles out. Nothing changes without
  `IDLE_TIMEOUT`. **Fix:** a page that should stay open sends a request
  now and then, or the project raises `IDLE_TIMEOUT`.
- **A JSON Web Token issued longer ago than `AUTH.IDLE_TIMEOUT` is
  refused** (SEC-15c-1), whatever its `ACCESS_TOKEN_TTL`: a JSON Web Token
  login's use is its refreshes. Nothing changes without `IDLE_TIMEOUT`, or
  with an access token that lives no longer than it (the default, 5
  minutes, is shorter than the 5-minute floor plus its tenth). **Fix:**
  refresh before the timeout, or raise it.
- **Auth has three new tables** (FIN-29, FIN-30, FIN-42):
  `domainkit_step_ups`, `domainkit_sca_challenges`, and
  `domainkit_password_history`. No default changed, and only `StepUp`,
  the step-up and transaction routes, `PASSWORD_HISTORY`, and
  `PASSWORD_CHANGE_ON_FIRST_USE` read and write them; but deleting or
  erasing a user deletes the user's rows there, and the hourly purge of
  tokens and `cleartokens` clear old ones, so each fails until the
  database has them. **Fix:** run `makemigrations` and `migrate` in each
  project that installs auth, before the new version serves.
- **Messaging's outbox gained columns, a status, and a table** (FIN-19).
  The outbox has `ordering_key` and `sequence` columns, a `held` status,
  and an index, `ix_domainkit_messages_ordering`, and messaging has a
  table of ordering keys, `domainkit_ordering_keys`, in every database
  that holds an outbox. Workers claim through the new columns, so they
  fail until the database has them. **Fix:** run `makemigrations` and
  `migrate` in each project that installs messaging, before the new
  version's workers start. Stop the 0.13 workers before any 0.14 process
  (a web process too) writes a message with an ordering key: a 0.13
  worker finishes such a message without making the next of its key
  pending, and the key waits until a 0.14 worker's cleanup round, or
  `messages --release`, releases it (SEC-14c-3). `messages` prints a
  `held` line after `pending`: a script that reads its lines by position
  reads them by name instead. Nothing keeps the old schema.
- **A message for an unknown task or handler waits before it dies**
  (CONC-15). It is given back, its attempt not spent, until
  `MESSAGING.UNKNOWN_TARGET_GRACE` (an hour) has passed, then
  dead-lettered as before. A message whose target no deploy brings back
  is dead-lettered an hour later, and an ordered key waits behind it
  meanwhile. **Fix:** nothing, usually; `MESSAGING =
  {"UNKNOWN_TARGET_GRACE": 0}` keeps the old behaviour. Deploy workers
  before web processes when a release adds tasks or handlers, and name
  tasks (`@task(name=...)`): `check` warns about the others where
  deployed (`messaging.W001`).
- **Task names are at most 255 characters** (CONC-16): `@task` and
  `@periodic` refuse a longer `name=` (or module path) with
  `ImproperlyConfigured`. PostgreSQL refused its messages anyway.
  **Fix:** a shorter `name=`.
- **A handler's own `CancelledError` is a failure** (CONC-17). One raised
  while the worker is not stopping (a cancelled task or future awaited)
  is retried with backoff, counts as an attempt, and dead-letters the
  message after `max_attempts`, as `HandlerCancelled`; it used to be
  retried at once, uncounted, for ever. **Fix:** nothing; a handler that
  cancels on purpose raises another error.
- **An amount in SQL is multiplied or divided by a number, not by another
  amount** (TEST-11). `amount * other` and `amount / other`, where `other`
  is an amount too (an `ExactDecimal` column, money's amount column
  included, or a parameter or literal typed as one), raise `TypeError`
  when the expression is built. On SQLite they gave minor units times
  minor units (9.95 times a typed 2 was 1990.00); on PostgreSQL they gave
  the product. **Fix:** give the factor as a number: a plain `Decimal` or
  an untyped `bindparam("rate")`, or a column declared `Integer` or
  `Numeric` (a rate is not an amount); `+` and `-` between amounts are
  unchanged.
- **A factor of an amount in SQL has at most 9 places** (TEST-07).
  `amount * factor` and `amount / factor`, with `factor` a value or a
  bound parameter, refuse a factor of more places (`ValueError`), where
  they used to compute with it (and on SQLite counted a bound one as an
  amount). **Fix:** round the factor to 9 places with the rule the domain
  needs, or compute the product in Python with `Money.multiply`.
- **The `immudb` extra needs protobuf 6.33.5 or later** (INFRA-15). The
  floor was 5.27.0, which has two advisories. **Fix:** a project that pins
  protobuf below 6.33.5 lifts the pin; immudb's SDK works with 6.33.5 to
  7.x.

## 0.12 to 0.13

### Breaking changes

Upgrade from 0.12 with deprecations as errors first: the removals below
were each deprecated in 0.12, with a warning that named its replacement.
Each item says what fails and what to write instead, and, where there is
a way, how to keep the old behaviour for as long as the project needs it.

- **A domain that defines a removed hook method fails `setup()`**
  (DSGN-03). `startup`, `shutdown`, `check`, `seed`, `erase_subject`,
  `export_subject`, `check_application`, `deployment_warnings`, `stage`,
  `committed`, `stage_transaction`, `transaction_committed`, and
  `migrated` are no longer methods of `DomainConfig`, and nothing calls
  them. A configuration that still has one of these names, written in its
  class or a base, set on the class later, or on the instance, is refused
  with `apps.E002`, which names the domain, the class, and the point:
  `Domain 'search': SearchConfig.erase_subject is a hook method, which
  domainkit 0.13 removed; nothing would call it.` **Fix:** register the
  function in `ready()` under a name of its own, as 0.12's warning said:

  ```python
  class SearchConfig(DomainConfig):
      def ready(self) -> None:
          on_erase_subject(erase_documents)  # domainkit.apps.hooks
  ```

  The refusal stays until 1.0
  ([ADR 0111](adr/0111-refuse-a-removed-hook-method-until-1-0-wherever-the-domain-defines-it.md)).
- **`HookPoint` takes no `method=`** (DSGN-03). It named the
  `DomainConfig` method a point called too; `HookPoint("x", method="...")`
  raises `TypeError`. **Fix:** drop the argument:
  `HookPoint("x", arguments=1, awaited=True)`.
- **`expect_version` is gone** (DSGN-14). `from
  domainkit.ddd.preconditions import expect_version` raises `ImportError`,
  and no expectation is held for the whole context any more. **Fix:** hold
  it on the unit of work the service changes the aggregate through:
  `with uow.expect(Order, order_id, versions):` in place of `with
  expect_version(Order, order_id, versions):`. The expectation is still checked
  again in each transaction of the unit of work (SEC-12a-6).
- **A test that did not request the database is refused it** (TEST-08).
  The pytest plugin raises `domainkit.db.DatabaseAccessRefused` when a
  test reaches the test databases without requesting `db`, or a fixture
  that does (`client`, `async_client`, `live_server`, ...); the message
  names the fixture. 0.12 let such a test read whatever the test before
  it left, and, run first, reach the databases the test settings name.
  **Fix:** request `db` in the test, or in its function-scoped fixtures
  that reach the database; a module- or session-scoped fixture opens the
  databases with `with database_blocker.unblock():`. **To keep the old
  behaviour:** `@pytest.mark.allow_database` on a test, or `pytestmark =
  pytest.mark.allow_database` in a module; or, for the whole suite,
  `domainkit_allow_database = true` under `[tool.pytest.ini_options]`.

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
- **Dumps of personal data name their operator where deployed**
  (SEC-12a-7). In staging and production, `dumpdata` of a table whose
  model has fields marked `Personal` needs `--operator` or
  `DOMAINKIT_OPERATOR`, and is recorded. **Fix:** set `DOMAINKIT_OPERATOR`
  where `dumpdata` runs. There is no setting to keep the old behaviour.

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
