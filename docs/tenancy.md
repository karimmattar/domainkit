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
- check that the caller belongs to the tenant;
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

Keep shared tables (users, the outbox) on another alias. Migrate each
tenant's schema. `migrate` makes the schema if it is missing, and keeps
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
| `ValueError` | `tenant()` or `check_tenant_id()` with a malformed id. | |
| `ImproperlyConfigured` | `TenantMiddleware` without `TENANCY`, or both `SUBDOMAIN_OF` and `HEADER`. | |

## Common mistakes

- **`TenantMiddleware` before authentication.** `MEMBERSHIP` then sees no
  user. Put it after `AuthenticationMiddleware`.
- **A header without `MEMBERSHIP`.** Anyone can send any tenant's name.
  Always pair `HEADER` with `MEMBERSHIP`.
- **A unique constraint without `tenant_id`.** Two tenants then cannot
  use the same invoice number. Put `tenant_id` first in it.
- **Plain `migrate` for a schema per tenant.** It leaves those databases
  out. Run `migrate --tenant <id>` for each tenant.
- **Using `all_tenants()` to read shared keys.** It also opens every
  tenant's rows. Use `shared_keys()` when only the keys are shared.

## See also

- [Databases: sharding](databases.md#sharding): shard groups, buckets,
  and `PINNED`.
- [Caches](cache.md): cache keys and versions.
- [Idempotency](contrib/idempotency.md): retried requests per tenant.
- [Audit](contrib/audit.md): the audit trail and its seals.
- [Problems](reference/problems.md): every error code.
