# Installing auth: content types and permissions

## Install

```python
INSTALLED_DOMAINS = [
    "domainkit.contrib.contenttypes",
    "domainkit.contrib.auth",
    # your domains
]
```

To authenticate requests, add the middleware after
`RequestContextMiddleware` and before `RateLimitMiddleware` and
`IdempotencyMiddleware` (startup refuses another order), and mount the
endpoints that log in and out:

```python
MIDDLEWARE = [
    "domainkit.http.middleware.ProxyHeadersMiddleware",
    "domainkit.http.middleware.SecurityHeadersMiddleware",
    "domainkit.http.middleware.RequestContextMiddleware",
    "domainkit.http.middleware.AllowedHostsMiddleware",
    "domainkit.contrib.auth.middleware.AuthenticationMiddleware",
    "domainkit.http.ratelimit.RateLimitMiddleware",
    "domainkit.http.middleware.RequestBodyLimitMiddleware",
]
```

```python
# config/urls.py
from domainkit.contrib.auth.api import router as auth_router

router.include_router(auth_router, prefix="/auth")
```

```bash
python manage.py makemigrations -m "auth"
python manage.py migrate            # also creates content types and permissions
python manage.py createsuperuser
```

| App | Tables |
| --- | --- |
| `domainkit.contrib.contenttypes` | `domainkit_content_types` |
| `domainkit.contrib.auth` | `domainkit_users`, `domainkit_groups`, `domainkit_permissions`, `domainkit_group_permissions`, `domainkit_users_groups`, `domainkit_users_permissions`, `domainkit_tokens`, `domainkit_mfa_devices`, `domainkit_recovery_codes` |

Users, groups, permissions, and content types are joined by foreign keys,
so they must live in one database: route `auth`, `contenttypes`, and a
custom user's domain to the same SQL alias (not a shard group).

## Content types and permissions

Every class mapped onto a table of an installed domain is a content type,
named `<domain label>.<model>`, where the model is the class name in lower
case (`shop.product`). After each `migrate`, every content type gets a
permission for each of its default actions, `add_<model>`, `change_<model>`,
`delete_<model>`, and `view_<model>`, named "Can add product" and so on.
Permissions are named by key, `<domain>.<codename>`: `shop.add_product`.

A class changes the defaults, and declares its own permissions, in a nested
`Meta`, as in Django:

```python
class Product(AggregateRoot[ProductId]):
    name: str

    class Meta:
        verbose_name = "product"
        default_permissions = ("add", "change", "delete", "view")
        permissions = (("publish_product", "Can publish product"),)
```

| `Meta` option | Default | Meaning |
| --- | --- | --- |
| `verbose_name` | the class name in words (`stock level`) | Used in permission names. |
| `default_permissions` | `("add", "change", "delete", "view")` | Actions that get a `<action>_<model>` permission. |
| `permissions` | `()` | Extra `(codename, name)` pairs. |

Codenames are lower-case letters, digits, and underscores. Two models of
one domain may not declare the same codename: their keys would be the
same. `migrate` creates missing rows and never changes or removes existing
ones, since grants hang off them. A domain's `DomainConfig.migrated` hook
runs after each `migrate`; these two apps use it.

