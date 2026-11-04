# Multi-tenancy

One deployment serving many tenants (companies, schools, shops), each
seeing only its own data. `domainkit.tenancy` gives each request its
tenant, and scopes to it what the framework keeps:

| What | How |
| --- | --- |
| Rows | Repositories of a `TenantOwned` aggregate read and change only the tenant's rows, set it on what they add, and refuse another's. |
| A database per tenant | A shard group picks the tenant's database; `PINNED` gives a tenant its own. |
| A schema per tenant | On `SCHEMA_DATABASES` (PostgreSQL), every transaction works in the tenant's schema. |
| Caches | Every key is the tenant's own. |
| Rate limits | Every count is the tenant's own. |
| Logs | Every record names the tenant (`tenant`). |
| Background tasks | A task runs as the tenant whose request queued it. |

## Turning it on

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

```python
# accounts/tenancy.py
async def is_member(user, tenant_id: str) -> bool:
    async with AccountsUnitOfWork() as uow:
        return await uow.memberships.exists(
            (F("user_id") == user.id) & (F("tenant") == tenant_id)
        )
```

`TenantMiddleware` works out the tenant of each request (HTTP and
WebSocket), from `RESOLVER` if set, else the subdomain of `SUBDOMAIN_OF`,
else the `HEADER`, and serves the request as that tenant. It refuses:

- a request that names none, while `REQUIRED` and its path does not start
  with one of `EXEMPT_PATHS`: `400 tenant_not_named`;
- an id that is not 1 to 63 letters, digits, `_`, `-`: `400 invalid_tenant`;
- a caller `MEMBERSHIP` says does not belong: `403 not_a_member`.

A WebSocket is closed with 1008 instead. A header is chosen by the
caller: pair `HEADER` with `MEMBERSHIP`, or anyone reads any tenant.

| `TENANCY` key | Default | Meaning |
| --- | --- | --- |
| `SUBDOMAIN_OF` | `None` | `example.com`: `acme.example.com` is the tenant `acme`. |
| `HEADER` | `None` | A header that names the tenant (`X-Tenant`); not with `SUBDOMAIN_OF`. |
| `RESOLVER` | `None` | Dotted path to a function of the request (async or not) that returns its tenant, or `None`; replaces the two above. |
| `MEMBERSHIP` | `None` | Dotted path to a function of the user and a tenant id (async or not): whether the user may act in it. |
| `REQUIRED` | `True` | Refuse a request that names no tenant. |
| `EXEMPT_PATHS` | `("/health",)` | Paths that may name none. |
| `SCHEMA` | `"tenant_{tenant}"` | A tenant's schema on `SCHEMA_DATABASES`: lowercase letters and `_` around `{tenant}` (the id lowercased, `-` as `_`). |
| `SCHEMA_DATABASES` | `()` | Database aliases (PostgreSQL) whose every table lives in a schema per tenant. |

Outside a request, name the tenant yourself:

```python
from domainkit.tenancy import tenant, all_tenants

with tenant("acme"):
    await send_invoices()

with all_tenants():  # every tenant's rows, on purpose
    await report_usage()
```

## Rows: `TenantOwned`

```python
from domainkit.ddd import AggregateRoot
from domainkit.ddd.tenancy import TenantOwned


class Invoice(TenantOwned, AggregateRoot[InvoiceId]):
    number: str
```

The table needs a `tenant_id` column: `String(63)`, not null, indexed,
and first in the unique constraints a tenant's rows share
(`UniqueConstraint("tenant_id", "number")`). Then every read of the
repository (`filter`, `first`, `count`, `exists`, `values`, `aggregate`,
`paginate`, `stream`, the bulk `update` and `delete`) adds
`tenant_id = <the tenant>`; `get` and `find` treat another tenant's
aggregate as missing (404, not 403: its existence is not told); `add`
and `get_or_create` set the tenant on a new aggregate, and refuse one
that names another (`403 tenant_mismatch`). With no tenant current, all
of them refuse (`403 tenant_required`) rather than read every tenant's
rows; `all_tenants()` reads them on purpose, and an aggregate added
there must name its tenant.

`TenantOwned` lives in `domainkit.ddd`, so the domain layer may use it.

## A database per tenant

Route the tenant's domains to a shard group: a unit of work given no
`shard_key` takes the current tenant's, so each tenant's rows land in
one database, and `PINNED` gives a large tenant a database of its own:

```python
SHARDS = {
    "tenants": {
        "DATABASES": ["tenants_1", "tenants_2", "bigco"],
        "PINNED": {"bigco": "bigco"},
    },
}
DATABASE_ROUTING = {"billing": "tenants"}
```

Moving tenants between databases is sharding's
([databases.md](databases.md)).

## A schema per tenant (PostgreSQL)

List the database aliases whose every table lives in a schema per
tenant in `SCHEMA_DATABASES`. Each transaction of a unit of work there
begins with `SET LOCAL search_path TO "tenant_acme"`: it ends with the
transaction, so a pooled connection never keeps another tenant's
schema. With no tenant current, the unit of work is refused
(`all_tenants()` too: a schema holds one tenant).

Keep shared tables (users, the outbox) on another alias. Migrate each
tenant's schema, which is made if missing and keeps its own revision:

```text
python manage.py migrate --tenant acme --tenant globex
```

Plain `migrate` leaves these databases out. Run it for a new tenant
before its first request.

## Caches, limits, and logs

While a tenant is served, `make_key` puts it in every cache key
(`t:acme:...`), and rate limits count each tenant apart, so one tenant
never reads another's cached answers or uses up its quota. What belongs
to no tenant is kept once, inside `shared_keys()`: an account's and an
address's login attempts, what principals rest on, and the flags; so
naming another tenant never starts a login count afresh, and a change
made while one is served reaches every other. An `Idempotency-Key` is the
tenant's own: the same key sent to two tenants is two requests
(`IdempotencyMiddleware` comes after `TenantMiddleware`). Log records
get a `tenant` field (`-` outside one), in JSON logs and for formats
that name it (`%(tenant)s`).
