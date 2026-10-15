# Users, groups, and permissions

`domainkit.contrib.auth` and `domainkit.contrib.contenttypes` are Django's
`contrib.auth` and `contrib.contenttypes`, for REST APIs: users, groups,
permissions per content type, password hashing and validation, pluggable
backends, object permissions, a swappable user model, and the
`createsuperuser` and `changepassword` commands. The design is in
[ADR 0015](adr/0015-auth-and-content-types.md).

Authenticating requests (tokens, the middleware that sets the request's
actor, FastAPI dependencies such as `RequirePermissions`) and the admin API
come in the next releases; until then a project checks credentials with
`authenticate` and permissions with `has_perm` in its own dependencies.

## Install

```python
INSTALLED_DOMAINS = [
    "domainkit.contrib.contenttypes",
    "domainkit.contrib.auth",
    # your domains
]
```

```bash
python manage.py makemigrations -m "auth"
python manage.py migrate            # also creates content types and permissions
python manage.py createsuperuser
```

| App | Tables |
| --- | --- |
| `domainkit.contrib.contenttypes` | `domainkit_content_types` |
| `domainkit.contrib.auth` | `domainkit_users`, `domainkit_groups`, `domainkit_permissions`, `domainkit_group_permissions`, `domainkit_users_groups`, `domainkit_users_permissions` |

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

## Users and groups

`User` has the fields of Django's user, and is an aggregate like any other:
it records events, and repositories load it with its groups and
permissions.

| Field | Type | Notes |
| --- | --- | --- |
| `id` | UUID | |
| `username` | text, unique | 1 to 150 letters, digits, and `@ . + - _`; NFKC-normalized. |
| `password` | `PasswordHash` | Never a raw password; see below. |
| `email`, `first_name`, `last_name` | text | The email's domain is lower-cased. |
| `is_active` | bool | Inactive users cannot log in and have no permissions. |
| `is_staff` | bool | May use the admin API (with its permissions). |
| `is_superuser` | bool | Has every permission without grants. |
| `last_login`, `date_joined` | UTC timestamps | |
| `groups`, `user_permissions` | sets | Grants, through link tables. |

A `Group` has a unique name and a set of permissions; members have them
all. `AnonymousUser` stands for requests without credentials: no id, no
permissions.

```python
from domainkit.contrib.auth import AuthUnitOfWork, authenticate, has_perm

async with AuthUnitOfWork() as uow:
    alice = await uow.users.create_user("alice", password, email="a@example.com")
    editors = await uow.groups.create("editors")
    editors.grant(await uow.permissions.get("shop.publish_product"))
    alice.join(editors)
    await uow.commit()

user = await authenticate(username="alice", password=password)
if user is not None and await has_perm(user, "shop.publish_product"):
    ...
```

`user.has_perm(key)` answers from the loaded user, without a query;
`await has_perm(user, key, obj)` also asks every backend, which is how
object permissions work. Both say yes to an active superuser and no to an
inactive user. `has_perms`, `get_all_permissions`, and `user.has_domain_perms(label)`
(Django's `has_module_perms`) do what their names say.

`UserRepository.create_user` and `create_superuser` hash the password they
are given and refuse a taken username (`UsernameTaken`, a 409); they do not
validate it. Call `validate_password(password, user)` where people choose
passwords. `set_password(user, None)` makes a password unusable.

Changes are recorded as events, never with passwords or hashes in them:
`UserCreated`, `PasswordChanged`, `UserStatusChanged`, `UserJoinedGroup`,
`UserLeftGroup`, `UserPermissionGranted`, `UserPermissionRevoked`,
`GroupCreated`, `GroupRenamed`, `GroupPermissionGranted`, and
`GroupPermissionRevoked`. With `domainkit.contrib.audit` installed, each
becomes a row of the audit trail (`auth.user_permission_granted`).

## A custom user model

Swap the user for one of your own, as with `AUTH_USER_MODEL`: subclass
`AbstractUser` (or `AbstractBaseUser`, which has no username, names, or
staff flag), give it a table, and name both in the settings. Do it before
the first `migrate` that creates users.

```python
# accounts/domain/model.py
class Account(AbstractUser):
    phone: str = ""
    REQUIRED_FIELDS = ("email", "phone")  # what createsuperuser asks for


# accounts/infrastructure/orm.py
accounts = Table(
    "accounts_users",
    metadata,
    *abstract_user_columns(),
    Column("phone", String(32), nullable=False),
    info={"domain": "accounts"},
)
```

```python
AUTH = {
    "USER_MODEL": "accounts.domain.model.Account",
    "USER_TABLE": "accounts.infrastructure.orm.accounts",
}
```

The model's links to groups and permissions get tables named after its
table (`accounts_users_groups`, `accounts_users_permissions`), and the
default `domainkit_users` tables are then neither migrated nor dropped.
Startup refuses a table without a column for one of the model's fields, or
without `info={"domain": ...}` naming the installed domain that defines it:
migrations and `DATABASE_ROUTING` follow that domain.

Users who log in with their email subclass `AbstractBaseUser`:

```python
class Member(AbstractBaseUser):
    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    email: str


members = Table(
    "members",
    metadata,
    *abstract_base_user_columns(),
    Column("email", String(254), nullable=False, unique=True),
    info={"domain": "members"},
)

user = await authenticate(email="boss@example.org", password=password)
```

`get_user_model()` returns the class in use, and `get_user_table()` its
table.

## Backends

`AUTH.BACKENDS` lists backend classes, asked in order. `authenticate`
returns the first user a backend accepts; `has_perm` is true when any
backend grants the permission. A backend that raises `PermissionDenied`
ends either search with "no". `ModelBackend` checks passwords against the
user table, costs an unknown username as much time as a wrong password,
and grants what users and their groups were given.

A backend subclasses `BaseBackend` and overrides what it needs:
`authenticate(credentials)`, `get_user(user_id)`,
`get_all_permissions(user, obj)`, and `has_perm(user, perm, obj)`.
Object permissions:

```python
class OwnerBackend(BaseBackend):
    async def has_perm(self, user, perm, obj=None):
        return (
            perm == "billing.change_invoice"
            and getattr(obj, "owner_id", None) == user.id
        )


AUTH = {
    "BACKENDS": [
        "domainkit.contrib.auth.backends.ModelBackend",
        "billing.auth.OwnerBackend",
    ]
}
```

## Passwords

A user holds a `PasswordHash`, which only the hashers make, so a raw
password can never be stored by mistake, and hashes never appear in logs.
Hashing is slow on purpose; `authenticate` and the repositories run it in a
worker thread, so the event loop never waits.

`AUTH.PASSWORD_HASHERS` lists hasher classes. The first hashes new
passwords; every one listed checks passwords it once hashed. When a
password checks against an older or weaker hash, the login stores a new
hash made with the first hasher.

| Hasher | Notes |
| --- | --- |
| `PBKDF2PasswordHasher` | Default: PBKDF2-HMAC-SHA256, 1,500,000 iterations, from the standard library. |
| `PBKDF2SHA1PasswordHasher` | Checks hashes moved from older systems. |
| `Argon2PasswordHasher` | Argon2id; the `argon2` extra. Put it first to hash new passwords with it. |
| `ScryptPasswordHasher` | scrypt, from the standard library. |
| `FastPBKDF2PasswordHasher` | One iteration, for tests only; staging and production refuse it. |

The hashes have Django's format, so a project moving from Django keeps its
users' passwords: copy the hashes into `password`.

`AUTH.PASSWORD_VALIDATORS` lists the rules new passwords must follow, each
`{"NAME": "<dotted path>", "OPTIONS": {...}}`; `validate_password` runs them
all and raises `InvalidPassword` (a 422) with every rule broken, and
`password_help_texts()` describes them for clients.

| Validator | Options | Refuses |
| --- | --- | --- |
| `UserAttributeSimilarityValidator` | `user_attributes`, `max_similarity` (0.7) | Passwords like the username, names, or email, or parts of them. |
| `MinimumLengthValidator` | `min_length` (8) | Shorter passwords. |
| `CommonPasswordValidator` | `password_list_path` | Django's list of 20,000 common passwords, or your own (one per line, gzipped or not). |
| `NumericPasswordValidator` | | Passwords made only of digits. |

A validator of your own is a class with `validate(password, user)`, raising
`PasswordValidationError`, and `help_text()`.

In tests, hash with `FastPBKDF2PasswordHasher`:

```python
# config/settings/test.py
AUTH = {"PASSWORD_HASHERS": ["domainkit.contrib.auth.hashers.FastPBKDF2PasswordHasher"]}
```

## Settings

| `AUTH` key | Default | Meaning |
| --- | --- | --- |
| `USER_MODEL` | `"domainkit.contrib.auth.models.User"` | The user class. |
| `USER_TABLE` | `None` (the default user's table) | A custom user's `Table`; required with a custom `USER_MODEL`. |
| `PASSWORD_HASHERS` | PBKDF2, PBKDF2-SHA1, Argon2, scrypt | Hasher classes; the first hashes new passwords. |
| `PASSWORD_VALIDATORS` | the four above | Rules for new passwords; `[]` checks nothing. |
| `BACKENDS` | `["domainkit.contrib.auth.backends.ModelBackend"]` | Authentication and permission backends, in order. |

## Commands

| Command | Options | Purpose |
| --- | --- | --- |
| `createsuperuser` | `--<USERNAME_FIELD>`, `--<field>` per `REQUIRED_FIELDS`, `--noinput` | Create a superuser (and staff), asking for what the options leave out. With `--noinput`, values come from `<PREFIX>SUPERUSER_<FIELD>` (or a `_FILE`), and the password from `<PREFIX>SUPERUSER_PASSWORD`, which must pass the validators; without one the password is unusable. |
| `changepassword` | `[username]` | Ask twice for a new password that passes the validators; three tries. The default user is your login name. |
