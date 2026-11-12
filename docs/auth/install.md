# Installing auth: content types and permissions

`domainkit.contrib.auth` gives your service users, groups, permissions,
logins, and access rules. It needs `domainkit.contrib.contenttypes`, which
keeps one row for each kind of stored object. This page shows how to
install both, what tables they add, and how each of your classes gets
its permissions.

On this page you learn how to:

- install the two domains, the middleware, and the login routes;
- create the tables, the permissions, and a first superuser;
- name the permissions of a class with its `Meta`;
- keep users and permissions in one database.

## A first example

A new project from `domainkit init` has auth installed already:
look for `domainkit.contrib.auth` in `config/settings/base.py`. To add it
to an older project, list both domains before your own
[domains](../learning.md#words):

```python
# config/settings/base.py
INSTALLED_DOMAINS = [
    "domainkit.contrib.messaging",
    "domainkit.contrib.contenttypes",
    "domainkit.contrib.auth",
    "shop",
]
AUTH: dict[str, object] = {}  # Every key is optional.
```

`domainkit.contrib.messaging` is optional. With it, the worker deletes
expired tokens every hour; without it, run `cleartokens` yourself.

Add `AuthenticationMiddleware` to `MIDDLEWARE`. It must come after
`RequestContextMiddleware`, and before `RateLimitMiddleware`,
`IdempotencyMiddleware`, and `WebSocketLimitsMiddleware`. Startup refuses
another order, because those middleware count users, and only
`AuthenticationMiddleware` knows the user:

```python
MIDDLEWARE = [
    "domainkit.http.middleware.ProxyHeadersMiddleware",
    "domainkit.http.middleware.SecurityHeadersMiddleware",
    "domainkit.http.middleware.RequestContextMiddleware",
    "domainkit.http.middleware.AllowedHostsMiddleware",
    "domainkit.contrib.auth.middleware.AuthenticationMiddleware",
    "domainkit.http.ratelimit.RateLimitMiddleware",
    "domainkit.http.websockets.WebSocketLimitsMiddleware",
    "domainkit.http.middleware.RequestBodyLimitMiddleware",
]
```

Mount the routes that log in and out, and the admin API for staff:

```python
# config/urls.py
from fastapi import APIRouter

from domainkit.contrib.auth.admin import router as admin_router
from domainkit.contrib.auth.api import router as auth_router
from domainkit.http.health import router as health_router

router = APIRouter()
router.include_router(health_router)
router.include_router(auth_router, prefix="/auth")
router.include_router(admin_router, prefix="/admin/auth")
```

`auth_router` logs users in and out. Each other part of auth, and each
app built on it, has a router of its own: sessions, the second factor,
tokens, recovery, devices. `account_router()` mounts those of every
installed app at once (since 2.4), in place of `auth_router`:

```python
from domainkit.contrib.auth.routing import account_router

router.include_router(account_router(), prefix="/auth")
```

Public signup is off unless you include it
(`account_router(include=["signup"])`), and `exclude=[...]` leaves routes
out. [`domainkit.contrib.auth.routing`](../api/auth.md) lists the names.

Then make the tables, and a user who may do everything:

```bash
python manage.py makemigrations -m "auth"
python manage.py migrate            # also creates content types and permissions
python manage.py createsuperuser
```

After `migrate`, each class of the `shop` domain has its permissions.
For a `Product` with one extra permission, and a `StockLevel` that
declares two actions only (see [the `Meta` options](#content-types-and-permissions)),
`uow.permissions.all()` lists:

```text
shop.add_product         Can add product
shop.change_product      Can change product
shop.delete_product      Can delete product
shop.publish_product     Can publish product
shop.view_product        Can view product
shop.change_stocklevel   Can change stock
shop.view_stocklevel     Can view stock
```

[Users, groups, backends, and passwords](users.md) shows how to give
these permissions to users, and [Protecting routes](routes.md) how a
route asks for them.

## The tables

`migrate` creates these tables. Run `makemigrations` and `migrate` again
after you install another auth domain, such as
[identity](identity.md), [devices](devices.md), or [phones](phones.md).

| Domain | Tables |
| --- | --- |
| `domainkit.contrib.contenttypes` | `domainkit_content_types` |
| `domainkit.contrib.auth` | `domainkit_users`, `domainkit_groups`, `domainkit_permissions`, `domainkit_group_permissions`, `domainkit_users_groups`, `domainkit_users_permissions`, `domainkit_tokens`, `domainkit_mfa_devices`, `domainkit_recovery_codes`, `domainkit_pending_signups`, `domainkit_step_ups`, `domainkit_sca_challenges`, `domainkit_password_history` |

The tokens table holds every credential: login tokens, browser sessions,
refresh tokens, personal tokens, client certificates, and device proofs.
It stores only a digest of each, never the token itself.

A [custom user model](users.md#a-custom-user-model) brings its own user
table. Its link tables are then named after it, and the three
`domainkit_users` tables are neither migrated nor dropped.

## One database for users and permissions

Users, groups, permissions, and content types are joined by foreign keys,
so they must live in one database. If you use `DATABASE_ROUTING`, route
`auth`, `contenttypes`, and the domain of a custom user model to the same
SQL alias, not to a shard group. Otherwise startup stops with a message
such as:

```text
Users, groups, permissions, and content types need one database (foreign keys join them): auth in 'default', contenttypes in 'default', accounts_users in 'customers'.
```

The tables of `domainkit.contrib.identity`, `domainkit.contrib.devices`,
and `domainkit.contrib.phones` must be on that database too.

## Content types and permissions

A content type is one kind of stored object. Every class mapped onto a
table of an installed domain is one. Its name is
`<domain label>.<model>`, where the model is the class name in lower
case: `shop.product`.

After each `migrate`, every content type gets one permission for each of
its default actions: `add_<model>`, `change_<model>`, `delete_<model>`,
and `view_<model>`. Their names read "Can add product", and so on. You
name a permission by its key, `<domain>.<codename>`: `shop.add_product`.

A class changes the defaults, and adds permissions of its own, in a
nested `Meta`:

```python
from typing import NewType
from uuid import UUID

from domainkit.ddd import AggregateRoot

ProductId = NewType("ProductId", UUID)


class Product(AggregateRoot[ProductId]):
    name: str

    class Meta:
        verbose_name = "product"
        default_permissions = ("add", "change", "delete", "view")
        permissions = (("publish_product", "Can publish product"),)


class StockLevel(AggregateRoot[ProductId]):
    count: int = 0

    class Meta:
        verbose_name = "stock"
        default_permissions = ("change", "view")
```

| `Meta` option | Default | Meaning |
| --- | --- | --- |
| `verbose_name` | the class name in words (`stock level`) | The name used in permission names ("Can view stock"). |
| `default_permissions` | `("add", "change", "delete", "view")` | The actions that get an `<action>_<model>` permission. |
| `permissions` | `()` | Extra `(codename, name)` pairs. |

Every option is optional, and so is `Meta` itself.

- **Codenames** use lower-case letters, digits, and underscores.
- **Two models of one domain may not declare the same codename**: their
  keys would be the same.
- **`migrate` only adds.** It creates the missing rows, and never changes
  or removes a row that exists, because grants point to it. To rename a
  permission, write a data migration.
- **The rules do not need this domain.** Which classes are models, their
  keys, and their `Meta` are the core's (`domainkit.db.models`). Access
  rules such as `ModelPermissions(Product)` work without
  `domainkit.contrib.contenttypes`.

`domainkit.contrib.contenttypes` exports two names:

| Name | What it is |
| --- | --- |
| `ContentType` | The entity of one row: `domain` and `model`. `str()` gives `shop.product`. |
| `sync_content_types(connection)` | Inserts a row for every installed model that has none, and returns the ids by `(domain, model)`. `migrate` calls it for you. |

Both domains fill their rows from an `on_migrated` hook
(`domainkit.db.hooks`), which runs after each `migrate`. A domain of
yours can register its own hook the same way.

## Errors and refusals

| What you see | Why | Fix |
| --- | --- | --- |
| `MIDDLEWARE: list domainkit.http.middleware.RequestContextMiddleware before domainkit.contrib.auth.middleware.AuthenticationMiddleware.` | The actor is bound to the request context, which must exist first. | Move `RequestContextMiddleware` up. |
| `MIDDLEWARE: list domainkit.contrib.auth.middleware.AuthenticationMiddleware before domainkit.http.ratelimit.RateLimitMiddleware, so it counts users rather than client addresses.` | A middleware that counts users runs before the user is known. | Move `AuthenticationMiddleware` up. |
| `auth.E101`: "Routes check users, but MIDDLEWARE lacks ..." | A route reads the user, and nothing finds it. | Add `AuthenticationMiddleware`. |
| `Users, groups, permissions, and content types need one database ...` | `DATABASE_ROUTING` splits them. | Route them to one SQL alias. |

## Common mistakes

- **Swapping the user model after the first `migrate`.** The first
  migration creates `domainkit_users`. Choose a
  [custom user model](users.md#a-custom-user-model) before it.
- **Forgetting `makemigrations` after installing an auth domain.** Each
  of `identity`, `devices`, and `phones` adds tables.
- **Expecting `migrate` to rename a permission.** It never changes a row
  that exists. Change it in a data migration.

## See also

- [Users, groups, backends, and passwords](users.md): make users and
  give them permissions.
- [Authenticating requests](authentication.md): tokens, cookies, and
  certificates.
- [Protecting routes](routes.md): rules that read the permissions.
- [Commands](../reference/commands.md): `migrate`, `createsuperuser`, and
  the other auth commands.
