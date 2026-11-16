# Multi-tenancy

Multi-tenancy lets one deployment serve many customers, and each
customer sees only its own data. Each customer is a
[tenant](learning.md#words): a company, a school, a shop.
`domainkit.tenancy` gives each request its tenant. It then scopes to that
tenant what the framework keeps: rows, databases, caches, rate limits,
logs, and background tasks.

On this page you learn how to:

- name the tenant of each request, from a subdomain, a header, or a
  function of your own;
- take the tenant from a route's path, such as `/orgs/{org_id}/...`;
- check that the caller belongs to the tenant, and give members
  permissions in it;
- keep each tenant's rows apart with `TenantOwned`;
- give a tenant its own database, or its own PostgreSQL schema;
- serve a tenant outside a request, and read every tenant's rows on
  purpose.

| What | How it is scoped |
| --- | --- |
| Rows | Repositories of a `TenantOwned` aggregate read and change only the tenant's rows, set the tenant on what they add, and refuse another tenant's. |
| A database per tenant | A shard group picks the tenant's database. `PINNED` gives a tenant its own. |
| A schema per tenant | On `SCHEMA_DATABASES` (PostgreSQL), every transaction works in the tenant's schema. |
| Caches | Every key is the tenant's own. |
| Rate limits | Every count is the tenant's own. |
| Logs | Every record names the tenant (`tenant`). |
| Background tasks | A task runs as the tenant whose request queued it. |

## A first example

An invoice belongs to one tenant. The repository sets the tenant on a new
invoice, and hides it from every other tenant:

```python
import asyncio
from abc import ABC
from typing import Annotated, NewType
from uuid import UUID, uuid4

from domainkit.ddd import AggregateRoot, MaxLength, Repository, UnitOfWork, in_memory
from domainkit.tenancy import TenantOwned, tenant

InvoiceId = NewType("InvoiceId", UUID)


class Invoice(TenantOwned, AggregateRoot[InvoiceId]):
    number: Annotated[str, MaxLength(20)]


class BillingUnitOfWork(UnitOfWork, ABC):
    invoices: Repository[Invoice, InvoiceId]


async def main() -> None:
    uow = in_memory(BillingUnitOfWork)
    with tenant("acme"):
        async with uow:
            invoice = Invoice(id=InvoiceId(uuid4()), number="A-1")
            await uow.invoices.add(invoice)
            await uow.commit()
        print(invoice.tenant_id)
    with tenant("globex"):
        async with uow:
            print(await uow.invoices.count(), await uow.invoices.find(invoice.id))


asyncio.run(main())
```

```text
acme
0 None
```

In a service, `TenantMiddleware` names the tenant of each request, so
your use cases do not call `tenant()` themselves.

## Turning it on

Set `TENANCY`, and add `TenantMiddleware` after the authentication
middleware:

```python
TENANCY = {
    "SUBDOMAIN_OF": "example.com",  # acme.example.com is "acme"
    "MEMBERSHIP": "accounts.tenancy.is_member",  # may this user act there?
}
MIDDLEWARE = [
    ...,
    "domainkit.contrib.auth.middleware.AuthenticationMiddleware",
    "domainkit.tenancy.TenantMiddleware",  # after authentication
]
```

`MEMBERSHIP` names a function of the user and a tenant id. It may be
async or not:

```python
# accounts/tenancy.py
from domainkit.ddd import F


async def is_member(user, tenant_id: str) -> bool:
    async with AccountsUnitOfWork() as uow:
        return await uow.memberships.exists(
            (F("user_id") == user.id) & (F("tenant") == tenant_id)
        )
```

`TenantMiddleware` works out the tenant of each request, HTTP and
WebSocket. It reads, in this order:

1. `RESOLVER`, if you set it: a function of the request that returns the
   tenant, or None;
2. else the subdomain of `SUBDOMAIN_OF`: `acme.example.com` is `acme`;
3. else the header `HEADER` names.

It then serves the request as that tenant. It refuses:

| Request | Answer |
| --- | --- |
| Names no tenant, while `REQUIRED`, and its path does not start with one of `EXEMPT_PATHS` | `400 tenant_not_named` |
| Names an id that is not 1 to 63 letters, digits, `_`, and `-`, starting with a letter or a digit | `400 invalid_tenant` |
| Comes from a caller that `MEMBERSHIP` says does not belong | `403 not_a_member` |

A WebSocket is closed with code 1008 instead.

!!! warning
    The caller chooses the value of a header. Pair `HEADER` with
    `MEMBERSHIP`, or anyone can read any tenant's data.

`TenantMiddleware` refuses to start without `TENANCY`
(`ImproperlyConfigured`). Put `IdempotencyMiddleware` after it, so an
`Idempotency-Key` is the tenant's own.

## A tenant from the path

A route can take its tenant from its path (since 2.4), once it is routed:
`/orgs/acme/invoices` is served as `acme`. Put `PathTenant` on a router,
as the first of its dependencies:

```python
from fastapi import APIRouter, Depends

from domainkit.http.access import HasPermissions, Require
from domainkit.tenancy import PathTenant

router = APIRouter(
    prefix="/orgs/{org_id}",
    dependencies=[Depends(PathTenant("org_id"))],
)


@router.get(
    "/invoices",
    dependencies=[Depends(Require(HasPermissions("billing.view_invoice")))],
)
async def invoices() -> list[InvoiceOut]: ...
```

```python
TENANCY = {"MEMBERSHIP": "accounts.tenancy.is_member"}
```

Every route of the router, and of the routers it includes, is then served
as the path's tenant, as `TenantMiddleware` serves one: rows, databases,
caches, rate limits, logs, and background tasks are the tenant's.
OpenAPI documents the path parameter. The dependency returns the tenant
id: name the `PathTenant` (`by_org = PathTenant("org_id")`), and a route
takes the id as `org: Annotated[str, Depends(by_org)]`, which FastAPI
does not run twice.

`PathTenant` needs `MEMBERSHIP` or `MEMBER_PERMISSIONS`: a caller can type
any id in a path. Without either, it raises `ImproperlyConfigured`. It
answers:

| Request | Answer |
| --- | --- |
| From a member of the tenant | Served as the tenant. |
| From a caller who proved who they are, and does not belong | `404 not_found` |
| Naming a tenant that does not exist, or an id that is not a tenant id | `404 not_found` |
| Naming a tenant other than the one `TenantMiddleware` already serves | `404 not_found` |
| From an anonymous caller who does not belong | `401 not_authenticated` |

The `404` is the answer a route that does not exist gives, word for word.
So a caller who does not belong cannot tell a tenant that exists from one
that does not. [RFC 9110](https://www.rfc-editor.org/rfc/rfc9110#section-15.5.4)
lets a server answer `404` to hide that a resource it refuses exists, as
GitHub does for private repositories. An anonymous caller gets `401`, as
`Require` answers it, whichever tenant the path names: logging in may
help, and the answer tells no tenant apart. A WebSocket handshake gets
the same answers. While a WebSocket or an event stream
stays open, membership is checked again with the route's rules
(`LIVE_REAUTH_INTERVAL`), and a member removed meanwhile is disconnected.

Put `PathTenant` first. A rule that runs before it sees no tenant: it
would answer a member's request `403`, and a stranger's `403` rather than
`404`. `check`, and the application as it starts, refuse a route whose
own dependencies put a rule before `PathTenant` (`tenancy.E002`), and one
whose path lacks the parameter `PathTenant` names (`tenancy.E001`), which
would refuse every request with `422`.

**With `TenantMiddleware` too.** A project may name some tenants before
routing and others by path. `TenantMiddleware` then refuses a request
that names no tenant before routing, unless its path is exempt. List the
prefix in `EXEMPT_PATHS`:

```python
TENANCY = {
    "SUBDOMAIN_OF": "example.com",
    "MEMBERSHIP": "accounts.tenancy.is_member",
    "EXEMPT_PATHS": ["/health", "/orgs/"],
}
```

**A resource whose items are tenants.** A companies resource lists at
`/companies`, whose path names no company, and reads one at
`/companies/{company_id}`. `PathTenant` on its router would refuse the
list (`tenancy.E001`). Give it to the resource instead (since 2.5):
`ResourceRouter("/companies", ..., tenant=PathTenant("company_id"))`
serves each route whose path holds `{company_id}` as that company, and
the list as no tenant
([resources](resources.md#a-resource-whose-items-are-tenants)).

**Served as no tenant.** `serve_no_tenant` is `PathTenant`'s
counterpart: a dependency that serves the route as no tenant, whatever
the middleware chose, and `no_tenant()` (`domainkit.ddd.tenancy`) does
the same for a block of code. A resource whose items are tenants puts it
on its collection's routes (since 2.5).

**The id's type.** `PathTenant("company_id", id_type=UUID)` (or `int`)
reads the parameter as that type (since 2.5). The tenant id is then the
value's text, a UUID in lowercase, so one tenant has one id however the
path spells it, and a value that is not one is `422`, as the route
answers it. By default the parameter is text, taken as it comes. A
resource's tenant reads its id's type. A route that reads the parameter
itself gets it as it declares it: declared as text, a UUID in capitals
stays in capitals there, while the tenant id is lowercase. Declare it
with the tenant's type, or take the tenant id the `PathTenant` returns.

## Members' permissions

A member's role in a tenant often decides what they may do there: an
owner changes invoices, a viewer reads them. `MEMBER_PERMISSIONS` (since
2.4) names a function of the user and a tenant id (async or not). It returns
the permission keys the user holds as a member of that tenant, or `None`
for one who is not a member. It replaces `MEMBERSHIP`; set one of them.
Your project keeps the memberships, in its own table:

```python
# accounts/tenancy.py
from domainkit.ddd import F

ROLES = {
    "owner": {"billing.view_invoice", "billing.change_invoice"},
    "viewer": {"billing.view_invoice"},
}


async def member_permissions(user, tenant_id: str) -> set[str] | None:
    if not user.is_authenticated:
        return None
    async with AccountsUnitOfWork() as uow:
        membership = await uow.memberships.first(
            (F("user_id") == user.id) & (F("tenant") == tenant_id)
        )
    return None if membership is None else ROLES[membership.role]
```

```python
TENANCY = {
    "SUBDOMAIN_OF": "example.com",
    "MEMBER_PERMISSIONS": "accounts.tenancy.member_permissions",
}
AUTH = {
    "BACKENDS": [
        "domainkit.contrib.auth.backends.ModelBackend",
        "domainkit.contrib.auth.backends.MembershipBackend",
    ]
}
```

`MembershipBackend` grants a user the keys they hold in the tenant being
served. So `HasPermissions("billing.change_invoice")`, `ModelPermissions`,
and `has_perm` count a member's role, beside what `ModelBackend` grants
everywhere. With no tenant current it grants nothing. It grants on no
object: object permissions stay with other backends. Listed without
`MEMBER_PERMISSIONS`, it raises `ImproperlyConfigured`.

The function is asked once per request: `TenantMiddleware` and
`PathTenant` ask it when they admit the caller, and every permission
check of the request reuses the answer. `member_permissions(user)` in
`domainkit.tenancy` returns it. In code outside a request, inside
`tenant(...)`, it asks the function each time. On a WebSocket or an event
stream, each re-check asks afresh, as the principal is loaded again. A
project that authenticates its own way calls `member_permissions` from
its principal's `ahas_perms`.

**A role never reaches beyond its tenant.** Keys a member holds through
`MEMBER_PERMISSIONS` answer the checks of requests served as that tenant,
and count toward no ceiling. `get_all_permissions`, which every ceiling
reads, leaves out the backends whose grants hold in one tenant only
(`tenant_scoped`, as `MembershipBackend` is). So, while a tenant is served:

- staff grant through the admin API, directly or through a group, only
  what they hold everywhere, and change or touch only users and groups
  within that (`You can only grant permissions you have`);
- a personal token, and a sign-in approval's access token, carry only
  scopes the user holds everywhere (`403 scope_not_held`). A scope only
  bounds a token anyway: each request still needs the permission itself.

`/auth/me` lists a member's keys in the tenant with the rest, as the
checks count them.

**Rules that count a member's keys alone.** `HasPermissions` asks every
backend: in a tenant, a user holds their role's keys and what
`ModelBackend` grants them everywhere. A back-office officer who is a
company's member would then act on the company's routes with their staff
permissions. Where only the role should count, use the member rules
(since 2.5):

```python
from domainkit.http.access import IsAuthenticated, Require
from domainkit.resources import MemberActions, ResourceRouter
from domainkit.tenancy import HasMemberPermissions, PathTenant


@router.post(
    "/invoices/{invoice_id}/approve",
    dependencies=[Depends(Require(HasMemberPermissions("billing.change_invoice")))],
)
async def approve(invoice_id: UUID) -> None: ...


companies = ResourceRouter(
    "/companies",
    ...,
    tenant=PathTenant("company_id"),
    rule=MemberActions(Company, outside=IsAuthenticated()),
    scope=companies_of_caller,  # The companies the caller belongs to.
)
```

- `HasMemberPermissions(*perms)` allows an active caller who holds every
  key as a member of the tenant being served (`member_permissions`),
  within their credentials' scopes. What they hold everywhere does not
  count, nor does a superuser's status: a superuser who is a member holds
  there what the membership gives. With no tenant served, it refuses.
  Without `MEMBER_PERMISSIONS`, it raises `ImproperlyConfigured`.
- `MemberActions(model)` asks for the key of each route's action, as
  `ModelActions` does, with `HasMemberPermissions`. A resource's routes
  that serve no tenant are refused, unless `outside=` rules them. A rule
  given as `outside` decides only the `view` and `add` routes there (the
  list, queries on the collection, creation); `Actions`, such as
  `ModelActions`, name each action's rules. A batch of commands on single
  companies is refused when declared: its items name companies it is not
  served as. Each command on the collection names its action
  (`action="add"` to create one), or is refused when declared.
- The resource serves its collection's routes as no tenant
  (`serve_no_tenant`), whatever `TenantMiddleware` named: the tenant a
  header or a subdomain names decides nothing there, through any rule.
  With `outside`, a member's keys also count only where the path named
  the tenant (`path_tenant()`).
- The list serves no tenant, so no member rule narrows it: give the
  resource a `scope` that keeps each caller to the companies they belong
  to, from your `MEMBER_TENANTS` function
  ([resources](resources.md#a-resource-whose-items-are-tenants)). A
  resource without one, under a rule as `outside`, is refused when
  declared.
- `InTenant()` allows a request served as a tenant, and its objects;
  `~InTenant()`, one served as none.

**Staff change no one who holds more than they do in a tenant.** Setting
a user's password, logging them out, or acting as them hands over their
memberships too. So, as Microsoft Entra keeps a helpdesk administrator
from resetting the password of an administrator, the admin API lets
staff change a user only in tenants the staff member belongs to, holding
there, or everywhere, every key the user holds there. To know a user's
tenants, name a function of the user (async or not) that returns their
ids in `MEMBER_TENANTS`:

```python
async def member_tenants(user) -> list[str]:
    async with AccountsUnitOfWork() as uow:
        rows = await uow.memberships.values(F("user_id") == user.id, "tenant")
    return [row["tenant"] for row in rows]


TENANCY = {
    "MEMBER_PERMISSIONS": "accounts.tenancy.member_permissions",
    "MEMBER_TENANTS": "accounts.tenancy.member_tenants",
}
```

Without it, nothing rules a user out as a member, so only superusers
change users through the admin API while `MEMBER_PERMISSIONS` is set, and
`check` warns (`auth.W010`). A refusal says in how many of the user's
tenants staff lack what the user holds, not which. Impersonation tokens
are checked the same way on every request. With `MEMBERSHIP` alone, staff
are held to the user's tenants once `MEMBER_TENANTS` is set; without it,
as before 2.4, the admin API does not count memberships.

## Outside a request

A command, a script, or a test names the tenant with `tenant()`:

```python
from domainkit.tenancy import all_tenants, tenant

with tenant("acme"):
    await send_invoices()

with all_tenants():  # every tenant's rows, on purpose
    await report_usage()
```

`tenant(tenant_id)` checks the id and raises `ValueError` for one that
is not a tenant id. `all_tenants()` is for maintenance and reports across
tenants. Nothing reads every tenant's rows unless it says so.

A background task that a request queued runs as the request's tenant.

## Rows: `TenantOwned`

`TenantOwned` is a mixin for aggregates. Put it before the aggregate
base:

```python
from domainkit.ddd import AggregateRoot
from domainkit.ddd.tenancy import TenantOwned


class Invoice(TenantOwned, AggregateRoot[InvoiceId]):
    number: str
```

`TenantOwned` lives in `domainkit.ddd.tenancy`, and `domainkit.tenancy`
exports it too. So the domain layer may use it. It adds one field,
`tenant_id: str | None = None`.

**The table.** The table needs a `tenant_id` column: `String(63)`, not
null, and indexed. Put it first in the unique constraints that a
tenant's rows share: `UniqueConstraint("tenant_id", "number")`.

**What the repository does.** While a tenant is current:

- Every read adds `tenant_id = <the tenant>`: `filter`, `first`, `count`,
  `exists`, `values`, `aggregate`, `paginate`, `stream`, and the bulk
  `update_rows` and `delete_rows`.
- `get` and `find` treat another tenant's aggregate as missing. Over
  HTTP that is 404, not 403, so its existence is not told.
- `add` and `get_or_create` set the tenant on a new aggregate. They
  refuse one that names another tenant: `403 tenant_mismatch`.
- A write schema made by `schema_for` leaves `tenant_id` out, so no
  caller moves a row to another tenant.

**With no tenant current,** every one of these refuses with
`403 tenant_required`, rather than read every tenant's rows. Inside
`all_tenants()` they read and change every tenant's rows. An aggregate
you add there must name its tenant.

## A database per tenant

Route the tenant's domains to a shard group. A unit of work given no
`shard_key` takes the current tenant as its key. Each tenant's rows then
land in one database. `PINNED` gives a large tenant a database of its
own:

```python
SHARDS = {
    "tenants": {
        "DATABASES": ["tenants_1", "tenants_2", "bigco"],
        "PINNED": {"bigco": "bigco"},
    },
}
DATABASE_ROUTING = {"billing": "tenants"}
```

[Sharding](databases.md#sharding) says how keys map to databases, and how
to move tenants between them.

## A schema per tenant (PostgreSQL)

List in `SCHEMA_DATABASES` the database aliases whose every table lives
in a schema per tenant. Each transaction of a unit of work there begins
with:

```sql
SET LOCAL search_path TO "tenant_acme"
```

The setting ends with the transaction, so a pooled connection never keeps
another tenant's schema. With no tenant current, the unit of work is
refused, and inside `all_tenants()` too: a schema holds one tenant. A
read-only unit of work that reads `Reads.STATEMENTS` reads in a
transaction there too, as `Reads.TRANSACTION` does: a statement on its
own would not see the setting.

`SCHEMA` names the schema. `{tenant}` is the id in lowercase, with `-`
as `_`. `tenant_schema(tenant_id)` gives the name:

```python
from domainkit.tenancy import tenant_schema

tenant_schema("Big-Co")  # 'tenant_big_co'
```

Keep shared tables (users, the outbox, the audit records made outside a
tenant) on another alias: the database's own schema holds no tables.
Every setting that names an app's home database must name that alias,
or the application refuses to start:

```python
DATABASES = {
    "default": {"URL": "postgresql+asyncpg://app@db/app"},
    "shared": {"URL": "postgresql+asyncpg://app@db/shared"},
}
TENANCY = {"SCHEMA_DATABASES": ["default"]}
DATABASE_ROUTING = {"auth": "shared", "contenttypes": "shared"}
AUDIT = {"DATABASE": "shared"}
MESSAGING = {"DATABASE": "shared"}
IDEMPOTENCY = {"DATABASE": "shared"}
```

Plain `migrate` migrates `shared`. Migrate each tenant's schema. `migrate` makes the schema if it is missing, and keeps
its own revision history:

```bash
python manage.py migrate --tenant acme --tenant globex
```

Plain `migrate` leaves these databases out. Run it for a new tenant
before its first request.

**The audit trail.** With `domainkit.contrib.audit` installed, each
tenant's schema holds an audit trail of its own: a unit of work run as
the tenant writes its rows there.

- `migrate --tenant` protects it with the same triggers as the database's
  own trail.
- `audit seal` and `audit verify` cover each tenant's trail, under its
  own ledger key (`<alias>/<schema>`).
- `audit forget` and `privacy erase` delete a person's pseudonym keys in
  each tenant's schema.
- `audit prune`, `audit verify --export`, and the startup check of the
  triggers cover each tenant's trail too.

## Caches, limits, and logs

While a tenant is served, every cache key and every rate-limit count is
the tenant's own. So one tenant never reads another's cached answers, or
uses up its quota. A cache's `make_key` puts the tenant in every key
(`t:acme:...`). `scoped_key(key)` makes a key the current tenant's;
caches and rate limits call it for you:

```python
from domainkit.ddd.tenancy import shared_keys
from domainkit.tenancy import scoped_key, tenant

with tenant("acme"):
    scoped_key("rates")  # 't:acme:rates'
    with shared_keys():
        scoped_key("rates")  # 'rates'
```

**A limit across tenants.** A route's `Throttle` counts, like every key,
per tenant: a member of ten companies may send ten times a day's
invitations. `Throttle(..., per_tenant=False)` counts each caller once,
whichever tenant the route serves (since 2.5):

```python
invitations = Throttle("50/day", scope="invitations", per_tenant=False)
```

A limit holds no data, so one count for every tenant tells nothing of
any. A cache entry does: computed while a tenant is served, it holds that
tenant's rows. So `@cached` has no such option; share an entry on purpose
with `shared_keys()`, where you know it belongs to no tenant.

`shared_keys()` keeps keys once, for every tenant, while rows stay
scoped. domainkit uses it for what belongs to no tenant: the login
attempts of an account and of an address, what principals rest on, and
the feature flags. So naming another tenant never starts a login count
again, and a change made while one tenant is served reaches every other.
Inside `all_tenants()`, keys are shared too.

An `Idempotency-Key` is the tenant's own: the same key sent to two
tenants is two requests.

Log records get a `tenant` field, `-` outside a tenant. JSON logs have
it, and so do formats that name it (`%(tenant)s`).

## Reading the tenant in code

| Function | Answers |
| --- | --- |
| `current_tenant()` | The tenant being served, or None. |
| `member_permissions(user)` | The keys `user` holds as a member of the current tenant (`MEMBER_PERMISSIONS`); async. |
| `path_tenant()` | The tenant a route's path named (`PathTenant`), if it is the one served; None where only `TenantMiddleware` or `tenant()` chose it. |
| `tenants_beyond(actor, user, held)` | The tenants where `user` holds what `actor` does not (`MEMBER_TENANTS`), or None when no one can be ruled out; async. The admin API's `may_change` asks it. |
| `row_tenant()` | The tenant `TenantOwned` rows are scoped to; None inside `all_tenants()`. With no tenant current, it raises `TenantRequired`. |
| `tenant_schema(tenant_id)` | The PostgreSQL schema of a tenant. |
| `scoped_key(key)` | The key, made the current tenant's own. |
| `check_tenant_id(value)` (`domainkit.ddd.tenancy`) | The value as a tenant id, or `ValueError`. |

```python
from domainkit.tenancy import current_tenant, row_tenant, tenant

with tenant("acme"):
    print(current_tenant(), row_tenant())
print(current_tenant())
```

```text
acme acme
None
```

## Options

| `TENANCY` key | Default | What it does |
| --- | --- | --- |
| `SUBDOMAIN_OF` | `None` | `example.com`: `acme.example.com` is the tenant `acme`. |
| `HEADER` | `None` | A header that names the tenant (`X-Tenant`). Not with `SUBDOMAIN_OF`. |
| `RESOLVER` | `None` | Dotted path to a function of the request (async or not) that returns its tenant, or None. It replaces the two above. |
| `MEMBERSHIP` | `None` | Dotted path to a function of the user and a tenant id (async or not): whether the user may act in it. |
| `MEMBER_PERMISSIONS` | `None` | Dotted path to a function of the user and a tenant id (async or not): the keys the user holds as a member, or None for one who is not. Not with `MEMBERSHIP`. |
| `MEMBER_TENANTS` | `None` | Dotted path to a function of the user (async or not): the ids of the tenants the user belongs to. With `MEMBERSHIP` or `MEMBER_PERMISSIONS`. |
| `REQUIRED` | `True` | Refuse a request that names no tenant. |
| `EXEMPT_PATHS` | `("/health",)` | Paths that may name no tenant. |
| `SCHEMA` | `"tenant_{tenant}"` | A tenant's schema on `SCHEMA_DATABASES`: lowercase letters and `_` around `{tenant}`. |
| `SCHEMA_DATABASES` | `()` | Database aliases (PostgreSQL) whose every table lives in a schema per tenant. |

`TENANCY = None`, the default, means one tenant, and nothing is scoped.
[The settings reference](reference/backends.md) has the full rows.

## Errors and refusals

| Error | When | Over HTTP |
| --- | --- | --- |
| `TenantRequired` | Tenant data is read or written with no tenant current. | `403 tenant_required` |
| `TenantMismatch` | An aggregate of another tenant is added to this tenant's repository. | `403 tenant_mismatch` |
| (middleware) | No tenant named, where one is required. | `400 tenant_not_named` |
| (middleware) | A malformed tenant id. | `400 invalid_tenant` |
| (middleware) | The caller does not belong to the tenant. | `403 not_a_member` |
| (`PathTenant`) | The caller does not belong to the path's tenant, or it is not one. | `404 not_found` |
| (`PathTenant`) | An anonymous caller does not belong to the path's tenant. | `401 not_authenticated` |
| `ValueError` | `tenant()` or `check_tenant_id()` with a malformed id. | |
| `ImproperlyConfigured` | `TenantMiddleware` without `TENANCY`; both `SUBDOMAIN_OF` and `HEADER`, or both `MEMBERSHIP` and `MEMBER_PERMISSIONS`, or `MEMBER_TENANTS` without either; `PathTenant` without either, or with an `id_type` other than `str`, `int`, or `UUID`; `MembershipBackend` or `HasMemberPermissions` without `MEMBER_PERMISSIONS`; a hook that answers one string or a bool. | |

## Common mistakes

- **`TenantMiddleware` before authentication.** `MEMBERSHIP` then sees no
  user. Put it after `AuthenticationMiddleware`.
- **A header without `MEMBERSHIP`.** Anyone can send any tenant's name.
  Always pair `HEADER` with `MEMBERSHIP`.
- **A rule before `PathTenant`.** It runs with no tenant: members get
  `403`, and so do strangers, where they should get `404`. Put
  `PathTenant` first in the router's dependencies (`tenancy.E002`).
- **`HasPermissions` where only the role should count.** It counts what
  a member holds everywhere too: staff who are members act as staff. Use
  `HasMemberPermissions` or `MemberActions`.
- **`PathTenant` on a resource's router.** Its list has no tenant in its
  path (`tenancy.E001`). Pass `tenant=` to the `ResourceRouter`.
- **A path tenant's prefix that is not exempt.** With `TenantMiddleware`
  and `REQUIRED`, a request that names no tenant before routing gets
  `400 tenant_not_named`. Add the prefix to `EXEMPT_PATHS`.
- **A unique constraint without `tenant_id`.** Two tenants then cannot
  use the same invoice number. Put `tenant_id` first in it.
- **Plain `migrate` for a schema per tenant.** It leaves those databases
  out. Run `migrate --tenant <id>` for each tenant.
- **A home database that keeps a schema per tenant.** `AUDIT.DATABASE`,
  `MESSAGING.DATABASE`, `IDEMPOTENCY.DATABASE`, and the routing of shared
  apps such as `auth` cannot name one of `SCHEMA_DATABASES`: its own schema
  holds no tables. Name another alias.
- **Using `all_tenants()` to read shared keys.** It also opens every
  tenant's rows. Use `shared_keys()` when only the keys are shared.

## See also

- [Databases: sharding](databases.md#sharding): shard groups, buckets,
  and `PINNED`.
- [Caches](cache.md): cache keys and versions.
- [Idempotency](contrib/idempotency.md): retried requests per tenant.
- [Audit](contrib/audit.md): the audit trail and its seals.
- [Problems](reference/problems.md): every error code.
