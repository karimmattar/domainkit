# Users, groups, and permissions

`domainkit.contrib.auth` and `domainkit.contrib.contenttypes` are Django's
`contrib.auth` and `contrib.contenttypes`, for REST APIs: users, groups,
permissions per content type, password hashing and validation, pluggable
backends, object permissions, and a swappable user model. Requests prove
who sent them with tokens, opaque by default or JSON Web Tokens, and
routes say who may call them with rules modelled on Django REST
framework's permission classes. The design is in
[ADR 0015](adr/0015-auth-and-content-types.md),
[ADR 0016](adr/0016-authenticating-requests.md), and
[ADR 0017](adr/0017-deny-by-default-and-admin-api.md).

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
| `domainkit.contrib.auth` | `domainkit_users`, `domainkit_groups`, `domainkit_permissions`, `domainkit_group_permissions`, `domainkit_users_groups`, `domainkit_users_permissions`, `domainkit_tokens` |

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
are given and refuse a taken username (`UsernameTaken`, a 409), also when
two requests race for it and the unique constraint refuses the second; they
do not validate it. Call `validate_password(password, user)` where people choose
passwords. `set_password(user, None)` makes a password unusable.

Changes are recorded as events, never with passwords or hashes in them:
`UserCreated`, `PasswordChanged`, `UserStatusChanged`, `UserJoinedGroup`,
`UserLeftGroup`, `UserPermissionGranted`, `UserPermissionRevoked`,
`GroupCreated`, `GroupRenamed`, `GroupPermissionGranted`,
`GroupPermissionRevoked`, and `RefreshTokenReused` (a used JWT refresh
token came back, so its login was ended; it is also logged as a security
warning). With `domainkit.contrib.audit` installed, each
becomes a row of the audit trail (`auth.user_permission_granted`). The
username in `UserCreated` and `UserDeleted` is personal data: the trail keeps
it as a pseudonym of the user's id, which `auditforget <user id>` makes
unlinkable (see [contrib.md](contrib.md)).

## A custom user model

Swap the user for one of your own, as with `AUTH_USER_MODEL`: subclass
`AbstractUser` (or `AbstractBaseUser`, which has no username, names, or
staff flag), give it a table, and name both in the settings. Do it before
the first `migrate` that creates users.

```python
# accounts/domain/model.py
class Account(AbstractUser):
    phone: str = ""
    risk_flag: bool = False
    REQUIRED_FIELDS = ("email", "phone")  # what createsuperuser asks for
    EDITABLE_FIELDS = AbstractUser.EDITABLE_FIELDS | {"phone"}  # staff may change


# accounts/infrastructure/orm.py
accounts = Table(
    "accounts_users",
    metadata,
    *abstract_user_columns(),
    Column("phone", String(32), nullable=False),
    Column("risk_flag", Boolean, nullable=False),
    info={"domain": "accounts"},
)
```

Staff see and change only the fields a model names. `EDITABLE_FIELDS`
lists what the admin API may change (`email`, `first_name`, and
`last_name` on `AbstractUser`, nothing on `AbstractBaseUser`), and
`READ_ONLY_FIELDS` what it only shows (`date_joined`). A new field, such
as `risk_flag` above, stays out of the admin API until you list it.
Startup refuses names the model lacks, and protected fields (credentials,
status flags, grants, the username) in `EDITABLE_FIELDS`.

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
Hashing is slow on purpose; `authenticate` and the repositories run it on
the process's hashing threads (`HASHING_THREADS`), so the event loop never
waits. At most `HASHING_QUEUE` calls wait for a thread. When the queue is
full, a login, password change, or user creation answers
`503 password_checks_busy` with `Retry-After: 1` at once, rather than
queueing work the process cannot finish. Unknown usernames are hashed too,
so response times do not reveal which exist. Raise the limits on processes
with cores to spare; `LOGIN_RATE` limits each client and username, these
limit the process. They keep the rest of the API responsive during a login
flood, but logins themselves may then be refused: stopping a flood from
many addresses takes measures before hashing, such as locking an account
after repeated failures.

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

## Authenticating requests

`AuthenticationMiddleware` finds who sent each request, before anything
counts or records it. It tries `AUTH.AUTHENTICATION_CLASSES` in order:

- the user becomes `request.user` (`AnonymousUser` without credentials),
  and the `Credentials` used become `request.auth`;
- the user's id becomes the request's actor, so rate limits, idempotency
  keys, logs, and the audit trail are per user, not per address;
- credentials a class recognizes but that do not hold (an unknown,
  expired, or revoked token; an inactive user) get a 401 with
  `WWW-Authenticate: Bearer realm="api", error="invalid_token"`, even on
  public routes, so a client never mistakes a bad token for anonymity.
  Credentials no class recognizes are left to the route (`/metrics` has its
  own token). `AUTHENTICATION_EXEMPT_PATHS` (the probes and `/metrics`) are
  served without looking at credentials at all.

Clients send `Authorization: Bearer <token>`. WebSocket handshakes are
authenticated the same way, and refused with close code 1008. The user is
the connection's actor, so the audit trail records what it does under
their id. `Require(...)` and `AuthenticatedUser` guard WebSocket routes as
they do others: a refused handshake gets the same 401 or 403 response,
before the socket opens.

### Opaque tokens

`TokenAuthentication`, the default, accepts tokens such as
`dkt_6R2v...`: `AUTH.TOKEN_PREFIX` and 256 random bits. Only a token's
SHA-256 is stored, so the table never holds a working token, and every
request looks its token up: logging out, a password change, or
`cleartokens --user` ends it at once. A login's token works for
`AUTH.TOKEN_TTL` (14 days); `createtoken` makes longer-lived ones for
scripts and other services. The prefix lets secret scanners and people
recognize a leaked token.

### JSON Web Tokens

`JWTAuthentication` (the `jwt` extra: `pip install "domainkit[jwt]"`)
issues short-lived signed access tokens and opaque refresh tokens:

```python
AUTH = {
    "AUTHENTICATION_CLASSES": [
        "domainkit.contrib.auth.authentication.JWTAuthentication",
        "domainkit.contrib.auth.authentication.TokenAuthentication",  # createtoken
    ],
    "JWT": {"SIGNING_KEY": env.str("JWT_SIGNING_KEY")},
}
```

- An access token carries `sub` (the user id), `sid` (the login), `iat`,
  `exp`, `jti`, and `iss` and `aud` when set. It is checked with
  `ALGORITHM` only, whatever its own header says, and works for
  `ACCESS_TOKEN_TTL` (5 minutes), while its login lasts: logging out ends
  it at once.
- A refresh token works once, at `/auth/refresh`, for a new pair; the new
  refresh token expires with the login (`REFRESH_TOKEN_TTL`, 14 days), so a
  user logs in again at least that often. A used refresh token that comes
  back means someone else may hold its family: the whole login ends.
- HMAC keys (`HS256`, `HS384`, `HS512`) must be at least as long as the
  hash (32, 48, 64 bytes). For `RS*`, `PS*`, `ES*`, and `EdDSA`, set
  `SIGNING_KEY` to the private key and `VERIFYING_KEY` to the public key,
  in PEM; other services can then check access tokens with the public key
  alone. Startup signs and verifies a token, so wrong keys fail early.

### Other credentials

A class of your own subclasses `BaseAuthentication`, returns `Credentials`
for its own credentials, None for others, and raises
`AuthenticationFailed` when its own do not hold:

```python
from domainkit.contrib.auth import AuthUnitOfWork, BaseAuthentication, Credentials
from domainkit.contrib.auth.authentication import active_user


class GatewayAuthentication(BaseAuthentication):
    """Users named by the gateway in front of the API (trust it alone)."""

    async def authenticate(self, connection):
        user_id = connection.headers.get("x-gateway-user")
        if user_id is None:
            return None
        async with AuthUnitOfWork() as uow:
            return Credentials(await active_user(uow, UUID(user_id)), scheme="gateway")
```

`active_user` raises `AuthenticationFailed` for unknown and inactive
users. The first class in the list also issues the tokens logins return
(`issue`) and refreshes them (`refresh`).

## Logging in and out

`domainkit.contrib.auth.api.router` has the endpoints; errors use the usual
error body.

| Endpoint | Body | Answer |
| --- | --- | --- |
| `POST /login` | `{"username", "password"}` | `200` with tokens; `401 invalid_credentials`; `429 login_throttled`. |
| `POST /refresh` | `{"refresh_token"}` | `200` with new tokens; `401` for a used, expired, or unknown refresh token; `400 refresh_not_supported` with opaque tokens. |
| `POST /logout` | optional `{"everywhere": true}` | `204`: this session's tokens end, or every token of the user. |
| `GET /me` | | The user: `id`, `username`, `email`, `is_staff`, `is_superuser`, `last_login`, `groups`, and `permissions`. |
| `POST /password` | `{"old_password", "new_password"}` | `204`: every other session of the user ends; `400 wrong_password`; `422` if a validator refuses the new password. |

`username` is the user model's `USERNAME_FIELD` (the email, for `Member`).
Tokens come as OAuth 2's token response:

```json
{"access_token": "dkt_6R2v...", "token_type": "Bearer", "expires_in": 1209600}
{"access_token": "eyJhbGciOi...", "token_type": "Bearer", "expires_in": 300, "refresh_token": "dkt_q9Xc..."}
```

Logins are limited to `AUTH.LOGIN_RATE` (10 a minute) per client address
(IPv6 per network, `RATE_LIMIT_IPV6_PREFIX`) and per username, with the `RATE_LIMIT_BACKEND` (use `RedisBackend` behind
several replicas), and so are password changes. Unknown users, wrong
passwords, and inactive users get the same answer in the same time. With
`domainkit.contrib.audit` installed, refused and throttled logins are
audited as `auth.login` (outcome `denied`, without the username, which may
be a password typed in the wrong field), and logins and logouts as
`auth.user_logged_in` and `auth.user_logged_out`, attributed to the user.

## Protecting routes

Rules decide who may call a route. `Require(*rules)` makes them a FastAPI
dependency that returns the user, and documents the bearer token in
OpenAPI; without rules it requires a user. A refused anonymous caller gets
`401 not_authenticated`, so it knows to log in; a refused user gets `403`.

```python
from domainkit.contrib.auth import (
    AuthenticatedUser, CurrentUser, HasPermissions, ModelPermissions, Require,
)

@router.post("/invoices", dependencies=[Depends(Require(HasPermissions("billing.add_invoice")))])
async def create_invoice(...): ...

@router.api_route("/products", methods=["GET", "POST"])
async def products(user: Annotated[AnyUser, Depends(Require(ModelPermissions(Product)))]): ...

@router.get("/me/invoices")
async def my_invoices(user: AuthenticatedUser): ...      # 401 for anonymous callers

@router.get("/greeting")
async def greeting(user: CurrentUser): ...               # anyone; AnonymousUser if so
```

| Rule | Allows |
| --- | --- |
| `AllowAny` | Anyone. |
| `IsAuthenticated` | Users who logged in. |
| `IsAuthenticatedOrReadOnly` | Anyone for `GET`, `HEAD`, `OPTIONS`; users otherwise. |
| `IsAdminUser` | Staff users (`is_staff`). |
| `IsSuperuser` | Active superusers. |
| `HasPermissions(*keys)` | Users granted every permission (by any backend). |
| `ModelPermissions(model)` | Django's model permissions by method: `GET`/`HEAD` need `view`, `POST` `add`, `PUT`/`PATCH` `change`, `DELETE` `delete`; `OPTIONS` needs a user. `model` is a mapped class or `"<domain>.<model>"`. |
| `HasObjectPermissions(*keys)` | Users granted every permission on the object, by a backend with object permissions. The request itself needs an active user who logged in; check each object with `check_object_permissions`. |

Combine rule instances with `&`, `|`, and `~`:
`Require(IsAdminUser() | HasPermissions("billing.view_invoice"))`. A rule of
your own subclasses `BasePermission`, overrides `has_permission(request,
user)` or `has_object_permission(request, user, obj)` (both async), and may
set `message`. Where a route loads one object, check it:

```python
class IsOwner(BasePermission):
    message = "Only the owner may change an invoice."

    async def has_object_permission(self, request, user, obj):
        return obj.owner_id == user.id


invoice = await uow.invoices.get(invoice_id)
await check_object_permissions(request, invoice, IsOwner())
```

### Deny by default

Every route must say who may call it: a `Require(...)` dependency, a
`CurrentUser` or `AuthenticatedUser` parameter, or `Public` for a route
anyone may call. Each can sit on the route, on a dependency it uses, or on
a router it is included through. Routes that say nothing are listed by
`manage.py check`, logged when the application starts in development and
tests, and refused in staging and production, where the application does
not start:

```text
The application has problems:
  GET /reports declares no access rule: add Depends(Require(...)), or dependencies=[Public] if anyone may call it.
```

```python
from domainkit.contrib.auth import Public


@router.get("/catalog", dependencies=[Public])  # anyone, on purpose
async def catalog(): ...


public_router = APIRouter(dependencies=[Public])  # every route in it
```

The framework's own routes (`/health`, `/ready`, `/metrics`) and the login
and refresh endpoints are covered already. `AUTH.REQUIRE_ROUTE_RULES =
False` turns the rule off; routes that read the user still need
`AuthenticationMiddleware`, and startup reports them while it is missing.
`open_routes(app)` returns the list, for a test of your own.

## The admin API

`domainkit.contrib.auth.admin.router` is Django's admin for users, groups,
and permissions, over REST. Mount it where staff tools live:

```python
from domainkit.contrib.auth.admin import router as admin_router

router.include_router(admin_router, prefix="/admin/auth")
```

Every endpoint needs a staff user (`is_staff`) or a superuser, and the
model permission of its action: `view`, `add`, `change`, or `delete` on
the user model (`auth.change_user`, or `accounts.change_account` for a
custom user), `auth.*_group`, or `auth.view_permission`.

| Endpoint | Body | Answer |
| --- | --- | --- |
| `GET /users` | `?limit=50&cursor=&search=` | A page of users by username; `search` matches part of it, in any case. |
| `POST /users` | `{"username", "password", "is_active", "is_staff", "is_superuser", "fields"}` | `201` and the user. The password is validated; without one, it is unusable. |
| `GET /users/{id}` | | The user. |
| `PATCH /users/{id}` | `{"is_active", "is_staff", "is_superuser", "fields"}` | The user; what is left out stays. |
| `DELETE /users/{id}` | | `204`; the user's tokens go too. |
| `POST /users/{id}/password` | `{"password"}` | `204`; `null` makes it unusable. Every session of the user ends. |
| `POST /users/{id}/logout` | | `204`: every session of the user ends. |
| `PUT /users/{id}/groups` | `{"groups": [names]}` | The user, in exactly those groups. |
| `PUT /users/{id}/permissions` | `{"permissions": [keys]}` | The user, granted exactly those permissions directly. |
| `GET /groups` | `?limit=50&cursor=` | A page of groups by name. |
| `POST /groups` | `{"name", "permissions"}` | `201` and the group. |
| `GET /groups/{id}` | | The group. |
| `PATCH /groups/{id}` | `{"name"}` | The renamed group. |
| `PUT /groups/{id}/permissions` | `{"permissions": [keys]}` | The group, granting exactly those permissions. |
| `DELETE /groups/{id}` | | `204`; its members lose its permissions. |
| `GET /permissions` | | Every permission: `key`, `name`, and `content_type`. |

A user is shown with `id`, `username`, `is_active`, `is_staff`,
`is_superuser`, `last_login`, `groups`, the `permissions` granted directly,
and `fields`: the ones the model lists in `EDITABLE_FIELDS` and
`READ_ONLY_FIELDS` (`email`, `first_name`, `last_name`, and `date_joined`
for `AbstractUser`). `fields` in a request changes only those in
`EDITABLE_FIELDS` (`editable_fields()`); naming any other field answers
`422 unknown_user_field`. Values are checked against the
field's type annotation (`422 invalid_user_field`), and `clean_field(name,
value)`, which a custom user may override, tidies what is stored.

Staff hand out only what they have:

- Only superusers make, unmake, or change superusers.
- Other staff cannot grant a permission they lack, directly or through a
  group (`403`).
- Nor can they change a user or a group that holds such a permission:
  setting a user's password is as good as becoming them. What every backend
  in `AUTH.BACKENDS` grants counts, and so does what a change would grant
  (a field a backend reads, a user made active again). Inactive users count
  with every grant they keep; while other backends are configured, only
  superusers change inactive users, since those backends grant them nothing
  until they are active.
- Deactivating a user ends their sessions: their tokens are deleted, and do
  not work again if they are reactivated.
- The last active superuser cannot be demoted, deactivated, or deleted
  (`409 last_superuser`): make another one first.

Changes are recorded as events (`UserUpdated`, `UserDeleted`,
`UserStatusChanged`, `UserJoinedGroup`, `GroupDeleted`, and the others),
so with `domainkit.contrib.audit` installed the audit trail names who
changed what.

## Settings

| `AUTH` key | Default | Meaning |
| --- | --- | --- |
| `USER_MODEL` | `"domainkit.contrib.auth.models.User"` | The user class. |
| `USER_TABLE` | `None` (the default user's table) | A custom user's `Table`; required with a custom `USER_MODEL`. |
| `PASSWORD_HASHERS` | PBKDF2, PBKDF2-SHA1, Argon2, scrypt | Hasher classes; the first hashes new passwords. |
| `HASHING_THREADS` | `1` | Threads per process that hash passwords; each keeps a core busy. One, like a Django worker, so with a process per core hashing never takes the event loops' cores. |
| `HASHING_QUEUE` | `8` | Hashing calls that may wait for a thread (about 0.4 s each); more answer `503 password_checks_busy`. |
| `PASSWORD_VALIDATORS` | the four above | Rules for new passwords; `[]` checks nothing. |
| `BACKENDS` | `["domainkit.contrib.auth.backends.ModelBackend"]` | Authentication and permission backends, in order. |
| `AUTHENTICATION_CLASSES` | `["domainkit.contrib.auth.authentication.TokenAuthentication"]` | How requests prove who sent them, in order; the first issues login tokens. |
| `AUTHENTICATION_EXEMPT_PATHS` | `["/health", "/ready", "/metrics"]` | Paths served without looking at credentials. |
| `TOKEN_TTL` | 14 days | How long a login's opaque token works; `None` until revoked. |
| `TOKEN_PREFIX` | `"dkt_"` | Starts every opaque token; changing it invalidates existing tokens. |
| `LOGIN_RATE` | `"10/minute"` | Login and password-change attempts per client address and per username; `None` for no limit. |
| `REQUIRE_ROUTE_RULES` | `True` | Every route must say who may call it (`Require`, a user parameter, or `Public`); see "Deny by default". |
| `JWT` | see below | JSON Web Tokens, for `JWTAuthentication`. |

| `AUTH["JWT"]` key | Default | Meaning |
| --- | --- | --- |
| `SIGNING_KEY` | `None` (required) | The HMAC secret, or the private key in PEM. |
| `VERIFYING_KEY` | `None` | The public key in PEM, for asymmetric algorithms. |
| `ALGORITHM` | `"HS256"` | The one algorithm accepted: `HS*`, `RS*`, `PS*`, `ES*`, or `EdDSA`. |
| `ACCESS_TOKEN_TTL` | 5 minutes | How long an access token works. |
| `REFRESH_TOKEN_TTL` | 14 days | How long a login can be refreshed. |
| `ISSUER` | `None` | `iss`, set and then required. |
| `AUDIENCE` | `None` | `aud`, set and then required. |
| `LEEWAY` | 0 | Clock difference tolerated on `exp` and `iat`. |

## Commands

| Command | Options | Purpose |
| --- | --- | --- |
| `createsuperuser` | `--<USERNAME_FIELD>`, `--<field>` per `REQUIRED_FIELDS`, `--noinput` | Create a superuser (and staff), asking for what the options leave out. With `--noinput`, values come from `<PREFIX>SUPERUSER_<FIELD>` (or a `_FILE`), and the password from `<PREFIX>SUPERUSER_PASSWORD`, which must pass the validators; without one the password is unusable. |
| `changepassword` | `[username]` | Ask twice for a new password that passes the validators; three tries. The default user is your login name. Every token of the user ends. |
| `createtoken` | `username`, `--name`, `--days` (90), `--no-expiry` | Print a new opaque token for the user's scripts; it is shown once. |
| `cleartokens` | `--user` | Delete expired tokens (hourly with `domainkit.contrib.messaging`), or every token of one user. |

Code that sets passwords itself ends the user's sessions with
`await uow.tokens.revoke_user(user.id)`.

In tests, log in through the API, or issue a token directly:

```python
async with AuthUnitOfWork() as uow:
    token = await uow.tokens.issue(user.id, ttl=None)
    await uow.commit()
client.get("/invoices", headers={"Authorization": f"Bearer {token}"})
```
