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

## Users and groups

`User` has the fields of Django's user, and is an aggregate like any other:
it records events, and repositories load it with its groups and
permissions.

| Field | Type | Notes |
| --- | --- | --- |
| `id` | UUID | |
| `username` | text, unique | 1 to 150 letters, digits, and `@ . + - _`; NFKC-normalized. Names that start with `erased-`, in any case, are kept for erased accounts. |
| `password` | `PasswordHash` | Never a raw password; see below. |
| `email`, `first_name`, `last_name` | text | The email is one plain address or empty, with its domain lower-cased (see below). |
| `email_verified` | bool | Whether the user proved they read mail at `email`; a new address clears it. |
| `is_active` | bool | Inactive users cannot log in and have no permissions. |
| `is_staff` | bool | May use the admin API (with its permissions). |
| `is_superuser` | bool | Has every permission without grants. |
| `last_login`, `date_joined` | UTC timestamps | |
| `groups`, `user_permissions` | sets | Grants, through link tables. |

An email address is one address alone, in ASCII, checked as
`domainkit.contrib.mail` checks the addresses it sends to:
`user@example.com`, never `Name <user@example.com>`, a comment, or a list,
and an internationalized domain in its `xn--` form. Creating a user,
changing the email (the admin API, `update`), signing up, and asking for
a reset all refuse anything else (`InvalidEmail`, a 422 `invalid_email`),
so the address mail goes to is the whole stored value, and no display
name reaches a mail's text. `validate_email(text)` checks one. Addresses
compare by their ASCII letters in any case and by nothing else
(`same_email`): `find_by_email` never takes a look-alike, such as one
with the KELVIN SIGN, which SQL's `lower()` makes a `k` on PostgreSQL,
for another's address. Rows stored before these checks keep their
address; the worker skips one that cannot take mail (below).

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

### Users' personal data

`AbstractUser` marks `username`, `email`, `first_name`, and `last_name`
`Personal(of="id")`, so `domainkit.contrib.privacy` exports them
(`privacy export --subject <user id>`) and erases them. Mark the personal
fields of a custom model too (a `Member`'s `email` above), and give it an
`erase_personal_data` method if they cannot be erased by default
([privacy.md](privacy.md)).

Erasing a user (`privacy erase --subject <user id>`) runs
`AbstractUser.erase_personal_data`:

- the username becomes `erased-` and 32 hex digits: an HMAC-SHA256 of the
  name (folded as lookups fold it) under a key derived from `SECRET_KEY`.
  It stays unique, and the old name stays taken: creating a user or
  signing up with it, in any case, is refused as a taken username, so
  nobody else can become the person erased. The key's fallbacks are
  checked too; a name erased under a key dropped from
  `SECRET_KEY_FALLBACKS` is free again. Names that start with `erased-`
  are kept for this: signing up, creating a user (with any user model:
  the repository checks), and `validate_username` refuse them
  (`InvalidUsername`), so no one can pass for an erased account. A name
  stored before that, which only starts with `erased-`, is erased like
  any other; one already of the erased form is left as it is, so erasing
  twice changes nothing;
- the email and names become empty, and the address unverified;
- the account ends in the same commit: it is deactivated and its password
  made unusable (both audited). Right after, every token of the user
  (logins, browser sessions, personal tokens, client certificates) and
  their second factor (secret and recovery codes) are deleted.

The row stays, with its id, so what refers to the user still does.
Erasing again changes nothing.

Fixtures (`dumpdata`, `loaddata`) never hold tokens, sessions,
certificates, TOTP secrets, or recovery codes: their tables are state
(`info={"fixtures": False}`). A fixture of users still holds password
hashes: keep it as safe as the database.

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

### Personal tokens and scopes

`domainkit.contrib.auth.personal_tokens.router` lets users manage their
own tokens, like GitHub's personal access tokens:

```python
from domainkit.contrib.auth.personal_tokens import router as tokens_router

router.include_router(tokens_router, prefix="/auth")
```

| Endpoint | Body | Answer |
| --- | --- | --- |
| `GET /tokens` | `?limit=50&cursor=` | The user's tokens, newest first: `id` (the digest), `name`, `kind` (`access`, `session`, `refresh`), `scopes`, `created_at`, `expires_at`, `last_used_at`, and `current` for the one this request uses; `next_cursor`. |
| `POST /tokens` | `{"name", "scopes", "expires_in_days"}` | `201`, with `token`: the value, shown this once and marked `Cache-Control: no-store`, so no cache or idempotency key keeps it (a retry with the key gets `409`). `expires_in_days` is 90 by default (1 to 3650), `null` for no end. `403 scope_not_held` for a scope the user lacks. |
| `DELETE /tokens/{id}` | | `204`; `404 token_not_found` for a token that is not the user's. A refresh token takes its JWT login with it. |

A token's scopes are permission keys (`shop.view_product`), at most 100,
each one the user holds when the token is made. They only narrow: a
request with a scoped token may do what both its user and its scopes
allow, so a permission taken from the user later is gone from the token
too. `HasPermissions`, `ModelPermissions`, and `HasObjectPermissions`
check both; `HasScope(*keys)` checks the scopes alone (credentials without
scopes allow every one); `IsAdminUser` and `IsSuperuser` refuse scoped
credentials, since staff and superusers hold more than any scope names;
and `IsUnscoped` keeps a route to credentials without scopes. These
routes, the admin API, `/password`, the second-factor routes,
`/email-verification`, and `/logout` with `everywhere` take only those,
so a scoped token can neither make a token with more, grant anything,
nor change or lock its account. `/me` with a scoped token lists, in
`permissions`, only those both the user and the scopes allow. `createtoken --scope KEY` makes scoped tokens
from the command line. Creating and revoking are audited
(`auth.token_created` with the scopes, `auth.token_revoked`).

Every token records when it last authenticated a request
(`last_used_at`), written at most once every five minutes, so a key no one
uses shows up without a write per request.

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

### Browser sessions in cookies

Browser apps should not keep tokens where scripts can read them.
`CookieAuthentication` keeps the session in an HttpOnly cookie instead,
and guards it against cross-site request forgery (CSRF):

```python
AUTH = {
    "AUTHENTICATION_CLASSES": [
        "domainkit.contrib.auth.authentication.TokenAuthentication",
        "domainkit.contrib.auth.cookies.CookieAuthentication",
    ],
    "CSRF_TRUSTED_ORIGINS": ["https://app.example.com"],
}
```

```python
from domainkit.contrib.auth.cookies import router as session_router

router.include_router(session_router, prefix="/auth")
```

| Endpoint | Body | Answer |
| --- | --- | --- |
| `POST /session` | `{"username", "password"}` | `200 {"csrf_token"}`, and two cookies: the session (`__Host-dk_session`) and the CSRF token (`__Host-dk_csrf`); `401`, `403 csrf_failed`, and `429` as for `/login`. |
| `GET /session` | | `200 {"csrf_token"}`: a new token for this session, also in its cookie; `400 no_session` without a session cookie. |
| `DELETE /session` | | `204`: the session ends and both cookies are cleared. |

The session cookie is HttpOnly, `Secure` unless `DEBUG` (staging and
production refuse `COOKIE.SECURE = False`), `SameSite=Lax` by default,
and holds an opaque token of the kind `session`, stored as a digest like
the others: it never works in an `Authorization` header, and logging out,
a password change, or `cleartokens --user` ends it. A request the session
authenticates with a method other than `GET`, `HEAD`, or `OPTIONS` must:

- come from a trusted origin: its `Origin` header (or `Referer`, without
  one) is the request's own origin, a host in `ALLOWED_HOSTS` with the
  request's scheme (a wildcard `*` trusts none), or one of
  `AUTH.CSRF_TRUSTED_ORIGINS` (`https://*.example.com` for subdomains). An
  HTTPS request that names neither is refused, as browsers always send one;
- send the CSRF token in `X-CSRF-Token` (`COOKIE.CSRF_HEADER`), equal to
  the CSRF cookie and signed with `SECRET_KEY` for this session, so a token
  set by another site or subdomain, or taken from another session, fails.

Anything else gets `403 csrf_failed`. Logging in at `/session` checks the
origin too. A WebSocket handshake with the session cookie must come from a
trusted origin (cross-site WebSocket hijacking). Requests with an
`Authorization` header ignore the cookie, so header-token clients are
unaffected. A session that ended, expired, or belongs to an inactive user
leaves the request anonymous rather than refused, so a stale cookie never
stops the browser from logging in again.

The cookies are named `__Host-dk_session` and `__Host-dk_csrf`. Browsers
take a cookie with the `__Host-` prefix only from the very host that sets
it, `Secure`, without `Domain`, on the path `/`: a page on a sibling
subdomain (`evil.example.com` beside `api.example.com`) cannot plant its
own session or CSRF token in the browser (cookie tossing), so it can
neither log the browser in as someone else nor fix its session. Where
the prefix cannot hold, because the cookies are not `Secure` (plain HTTP
under `DEBUG`), or have a `DOMAIN` or another `PATH`, they are
`dk_session` and `dk_csrf`. `COOKIE.NAME` and `COOKIE.CSRF_NAME` set the
names outright; in staging and production, `check` warns about a name
without the prefix where it could hold.

A single-page app on another origin needs CORS with credentials:
`CORS_ALLOWED_ORIGINS = ["https://app.example.com"]`,
`CORS_ALLOW_CREDENTIALS = True`, and `X-CSRF-Token` in
`CORS_ALLOWED_HEADERS`. `check` reports the router mounted without
`CookieAuthentication` in `AUTHENTICATION_CLASSES`. List it after the
classes that read headers: it issues no tokens for `/login`.

### Client certificates (mutual TLS)

Services that call the API can prove who they are with a client
certificate instead of a token. `ClientCertificateAuthentication` takes
the certificate from one of two places, and only these:

- the ASGI TLS extension, when the ASGI server terminates TLS and checked
  the certificate itself (`scope["extensions"]["tls"]`; a certificate it
  says did not verify gets 401);
- the header `AUTH.CLIENT_CERT.HEADER`, holding the URL-encoded PEM, only
  on connections from `AUTH.CLIENT_CERT.PROXIES`, and only with
  `VERIFY_HEADER` saying `SUCCESS` when that is set. The proxy's own
  address counts, not the client's that `ProxyHeadersMiddleware` puts in
  its place. A request from anyone else that sends the header gets 401: a
  client could otherwise name any certificate. Startup refuses `HEADER`
  without `PROXIES`.

`PROXIES` lists the exact addresses of the proxies that check
certificates, and nothing else. It is not `TRUSTED_PROXIES`, which often
holds a whole network (every pod of a cluster) and only decides whose
`X-Forwarded-For` is believed: any host of such a network could send the
header with its own `SUCCESS` and log in as anyone. Staging and
production refuse networks in `PROXIES`: each entry is one address (`/32`
or `/128`).

```python
AUTH = {
    "AUTHENTICATION_CLASSES": [
        "domainkit.contrib.auth.authentication.TokenAuthentication",
        "domainkit.contrib.auth.certificates.ClientCertificateAuthentication",
    ],
    "CLIENT_CERT": {
        "HEADER": "X-Client-Cert",
        "VERIFY_HEADER": "X-Client-Verify",
        "PROXIES": ["10.0.3.7"],  # the nginx host that checks certificates
    },
}
TRUSTED_PROXIES = ["10.0.3.7"]  # whose X-Forwarded-For is believed
```

The proxy must strip whatever a client sent in both headers, on every
request: `proxy_set_header` below replaces them with its own values (and
drops them when no certificate came). A proxy that passes a client's
headers through (a `location` without these lines, or another proxy in
front that forwards them) lets clients choose their certificate.

```nginx
server {
    listen 443 ssl;
    ssl_client_certificate /etc/nginx/clients-ca.pem;   # the CA that signs clients
    ssl_verify_client on;                               # or optional, with VERIFY_HEADER
    location / {
        # Set, never pass on: whatever the client sent in these is replaced.
        proxy_set_header X-Client-Cert   $ssl_client_escaped_cert;
        proxy_set_header X-Client-Verify $ssl_client_verify;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_pass http://api;
    }
}
```

With `MATCH = "fingerprint"` (the default), a certificate logs in as the
user `addcertificate <username> <cert.pem>` registered it for, by its
SHA-256; `cleartokens --user` and `DELETE /tokens/{id}` remove it, and
so does whatever ends every token of the user (a new password, a reset,
logging out everywhere): register it again then. With
`MATCH = "subject"`, the subject's common name is the username: everything
the CA signs logs in as someone, so use it only with a CA of your own (it
needs `cryptography`, the `mfa` extra). Through `HEADER` it also needs
`VERIFY_HEADER`, which startup checks: a proxy that passes on certificates
it did not verify would let a self-signed one name any user. Neither domainkit nor this class
checks the chain or the dates: the server or the proxy must, against the
CA you trust. Requests get `request.auth.scheme == "certificate"`.

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
| `GET /me` | | The user: `id`, `username`, `email`, `email_verified`, `is_staff`, `is_superuser`, `last_login`, `groups`, and `permissions` (with a scoped token, those its scopes allow). |
| `POST /password` | `{"old_password", "new_password"}` | `204`: every other session of the user ends; `400 wrong_password`; `422` if a validator refuses the new password. |

`username` is the user model's `USERNAME_FIELD` (the email, for `Member`).
Tokens come as OAuth 2's token response:

```json
{"access_token": "dkt_6R2v...", "token_type": "Bearer", "expires_in": 1209600}
{"access_token": "eyJhbGciOi...", "token_type": "Bearer", "expires_in": 300, "refresh_token": "dkt_q9Xc..."}
```

Logins are limited to `AUTH.LOGIN_RATE` (10 a minute) per client address
(IPv6 per network, `RATE_LIMIT_IPV6_PREFIX`) and per account, with the
`RATE_LIMIT_BACKEND` (use `RedisBackend` behind several replicas). Names
that logging in treats as one account (`alice`, `ALICE`, and their
full-width or mathematical look-alikes, under NFKC) share one count.
Password changes have a count of their own per user, so throttled logins
for a name never stop its user changing the password. Unknown users, wrong
passwords, inactive users, and users without a usable password get the
same answer in the same time. With `domainkit.contrib.audit` installed,
refused logins are audited as `auth.login` (outcome `denied`, without the
username, which may be a password typed in the wrong field), and throttled
ones once per count and period, so a flood adds no rows; logins and
logouts are audited as `auth.user_logged_in` and `auth.user_logged_out`,
attributed to the user.

Refused tokens are counted too, per client address, against `RATE_LIMIT`:
they never reach `RateLimitMiddleware`, which counts the user. Past the
limit the address gets 429, and its requests with credentials are refused
before any lookup until `Retry-After` has passed. Credentials in another
header than `Authorization`, which only a class of your own reads (an
`X-API-Key`), are read, and refused with the same 429 even when they hold,
so a guess that works never stands out. An opaque token that does not have
the form of an issued one is refused without a lookup. Only credentials
that do not hold (401) count: a cookie request refused by the CSRF checks
(403) is one any other site can make a browser send, and counting it
would let that site lock out everyone behind the browser's address.

The count per account is what stops guessing one user's password from many
addresses, and it also lets anyone hold that user's logins back: attempts
for a name count whoever sends them, so a caller who keeps sending
`LOGIN_RATE` wrong passwords for it leaves its user few attempts of their
own. Sessions already open are not affected, and password changes have
their own count. The audited `throttled` refusals show such a flood.

## Two-factor logins

`domainkit.contrib.auth.mfa` adds a second factor: an authenticator app
(TOTP, RFC 6238) and single-use recovery codes. It needs the `mfa` extra
(`pip install "domainkit[mfa]"`) and `SECRET_KEY`:

```python
from domainkit.contrib.auth.mfa import router as mfa_router

router.include_router(mfa_router, prefix="/auth")
```

| Endpoint | Who | Body | Answer |
| --- | --- | --- | --- |
| `GET /mfa` | a user | | `{"enabled", "recovery_codes_left"}`. |
| `POST /mfa/totp` | a user | `{"password"}` | `{"secret", "otpauth_uri"}`: the secret in base 32, and the URI for a QR code; `400 wrong_password`; `409 mfa_enabled`. |
| `POST /mfa/totp/confirm` | a user | `{"code"}` | `{"recovery_codes"}`, shown once: the second factor is on; `401 invalid_code`; `400 no_enrolment`. |
| `POST /mfa/totp/disable` | a user | `{"password", "code"}` | `204`: it is off. |
| `POST /mfa/recovery-codes` | a user | `{"password", "code"}` | `{"recovery_codes"}`: new codes replace the old ones. |
| `POST /mfa/login` | anyone | `{"mfa_token", "code"}` | The tokens `/login` would have returned; `400 invalid_token`; `401 invalid_code`; `429 throttled`. |

Once it is on, `/login` checks the password as before, then answers `202`
with `{"mfa_required": true, "mfa_token", "expires_in"}` instead of
tokens. The token is a challenge signed with `SECRET_KEY`: it works for
`MFA.CHALLENGE_TTL` (5 minutes), and ends once the login is done or the
password changes. `/mfa/login` takes it with a code from the app, or a
recovery code (in any case, with or without its dashes), and returns the
tokens. Browser sessions do the same: `POST /session` answers `202`, and
`POST /session/mfa` sets the cookies.

- A code from the current 30-second step, or `MFA.WINDOW` steps either
  side, is accepted once: the step is recorded, and a code of that step or
  an earlier one is refused, so a code seen over a shoulder cannot be
  replayed.
- Code checks count against `MFA.RATE` (10 an hour) per user, wherever
  they come from, and per client address at `/mfa/login`: a guesser who
  has the password gets a few hundred tries a day at a one-in-a-million
  code. At `/mfa/login` an attempt counts for the user only once its
  challenge holds, so a forged challenge cannot use up the count; anyone
  with the password can, and hold the user's logins back for the hour.
  The audited `throttled` refusals show it.
- Turning it off, and new recovery codes, need the password and a code:
  a stolen session cannot remove the factor.
- Secrets are stored encrypted with AES-256-GCM under a key derived from
  `SECRET_KEY`, bound to the user's id ([ADR 0030](adr/0030-encrypt-totp-secrets-under-a-derived-key.md)).
  Recovery codes (10 of 80 random bits, `MFA.RECOVERY_CODES`) are stored
  as digests. When `SECRET_KEY` is replaced, keep the old key in
  `SECRET_KEY_FALLBACKS` until every user with a second factor has logged
  in: each login moves their secret to the new key. Without it, those
  users cannot log in.
- `resetmfa <username>` turns it off for a user who lost the app and the
  codes, and ends their sessions; check who is asking first.

The audit trail records `auth.mfa_enabled`, `auth.mfa_disabled`,
`auth.recovery_codes_regenerated`, `auth.recovery_code_used` (with the
codes left), and refusals as `auth.mfa` (`invalid_token`, `invalid_code`,
`throttled`). Run `makemigrations` and `migrate` for the tables
`domainkit_mfa_devices` and `domainkit_recovery_codes`.

## Password reset

People who forgot their password ask for a link by mail, and choose a new
one with it. Install messaging and mail (`docs/mail.md`), and mount the
router that has these endpoints next to the one that logs in:

```python
INSTALLED_DOMAINS = [
    "domainkit.contrib.messaging",
    "domainkit.contrib.mail",
    "domainkit.contrib.contenttypes",
    "domainkit.contrib.auth",
    ...,
]
AUTH = {"PASSWORD_RESET_URL": "https://app.example.com/reset-password?token={token}"}
```

```python
from domainkit.contrib.auth.recovery import router as recovery_router

router.include_router(recovery_router, prefix="/auth")
```

| Endpoint | Who | Body | Answer |
| --- | --- | --- | --- |
| `POST /password-reset` | anyone | `{"email"}` | Always `202`; `429 throttled`. |
| `POST /password-reset/confirm` | anyone | `{"token", "new_password"}` | `204`; `400 invalid_token`; `422` if a validator refuses the password. |
| `POST /email-verification` | a user, unscoped | | `202`; `403` for credentials limited to scopes; `429 throttled`. |
| `POST /email-verification/confirm` | anyone | `{"token"}` | `204`; `400 invalid_token`. |

A reset request answers `202` whether or not an account uses the address,
with the same body, in the same time: it only queues a task (an address
that is not one plain address gets `422` at once). The worker looks the
address up, in any case, and mails every active user of it who has a
usable password a link, `PASSWORD_RESET_URL` with the token in place
of `{token}` (the token alone without one), which works for
`PASSWORD_RESET_TIMEOUT` (an hour). A user whose stored address cannot
take mail, from before addresses were checked, is skipped with a warning
that names the user's id, never the address, and the others still get
their links; verification links skip such a user the same way. Your page posts the token and the new
password to `/password-reset/confirm`. The new password is checked by the
validators, and every session of the user ends.

Reset tokens are signed with `SECRET_KEY` and never stored, not even in
the outbox: the worker makes them as it mails them. A token rests on the
user's password hash, last login, email, and status, so it stops working
once it is used, once the user logs in, or once any of those changes. A
wrong token, a used or expired one, and one for no user all get the same
`400 invalid_token`. Requests for mail count against `EMAIL_RATE` (5 an
hour) per client address and per email address; token checks count
against `LOGIN_RATE` per client address. They are not counted per user:
a signature cannot be guessed, and a count that anyone could use up with
forged tokens would lock the user out of the link they were sent.

The audit trail records `auth.password_reset_requested` for each link
mailed, `auth.password_reset` for a reset (and `denied`, with the reason
`invalid_token` or `throttled`, for refusals), and `auth.password_changed`.

### Email verification

`AbstractUser` has `email_verified`, false for new users and for existing
ones once the column is added (run `makemigrations` and `migrate`). A user
asks for a link at `/email-verification`; the worker mails it to their
address, unless it is verified, and `/email-verification/confirm` with its
token sets `email_verified`. The link works once, for
`EMAIL_VERIFICATION_TIMEOUT` (three days), and only for the address it went
to: changing the email clears `email_verified` and ends the links sent
before. Staff cannot set it in the admin API, where it is read-only.
`user.verify_email()` sets it in code. Verifications are audited as
`auth.email_verified`.

### Signing up

`domainkit.contrib.auth.signup.router` has two routes, which answer 404
until `AUTH.SIGNUP_ENABLED` is on:

| Endpoint | Who | Body | Answer |
| --- | --- | --- | --- |
| `POST /signup` | anyone | `{"username", "email", "password"}` | `202`; `422` for fields or a password the validators refuse; `429 throttled`. |
| `POST /signup/confirm` | anyone | `{"token"}` | `204`: the account is made; `400 invalid_token`; `409 signup_taken`; `429 throttled`. |

The fields and the password validators are checked at once (`422`), which
tells nothing about other accounts. Then the request is kept as a pending
signup, and it answers `202`, doing the same work whatever accounts
exist. The worker mails the address: when neither the username (in any
case) nor the address has an account, a link (`AUTH.SIGNUP_URL` with the
token in place of `{token}`, the token alone without one) that works for
`EMAIL_VERIFICATION_TIMEOUT`; otherwise, word that an account uses the
address or the username, and the pending signup is dropped. Your page
posts the token to `/signup/confirm`, which makes the account, its
address verified, and the user can log in.

No account exists before its address is confirmed. So nothing anyone else
can see differs between an address that has an account and one that has
none: an attacker who asks for an account with someone's address, then
tries to log in with the username they chose, gets `401` either way, and
can sign up with that username again either way. Whether an address has
an account is told only to whoever reads mail at it. Usernames are not
secret that way: someone who signs up with their own address learns from
the mail that a username is taken, as any signup form must tell;
`EMAIL_RATE` bounds how fast.

A pending signup reserves nothing, neither its username nor its address,
so one that is never confirmed blocks no one. Several may name the same
username: each address gets its link, the first link used gets the name,
and the others answer `409 signup_taken` (the person signs up again with
another name). A link for an address that got an account meanwhile
answers `409` too. A link works once; a wrong, used, or expired one gets
`400 invalid_token`, audited as `auth.signup`.

Pending signups keep the username, the address, and the password's hash
in `domainkit_pending_signups` (run `makemigrations` and `migrate`),
which is never in fixtures. The outbox holds only a pending signup's id.
A row goes when its link is used, when the worker finds the name or the
address taken, or, past `EMAIL_VERIFICATION_TIMEOUT`, with the hourly
purge of expired tokens. Signups count against `EMAIL_RATE` per client
address and per email address, links checked against `LOGIN_RATE` per
client address, and new accounts are audited as `auth.user_signed_up`.
Two links for one new address used at the same moment can both make an
account: email addresses are not unique in the user table.

The worker renders the mail from the templates `auth/password_reset`,
`auth/email_verification`, `auth/signup_confirm`, and
`auth/signup_exists` (plain text). Replace them from `MAIL.TEMPLATE_DIRS`;
the first two are given `user`, `username`, `link`, and `valid_for`
(`1 hour`), `auth/signup_confirm` only `link` and `valid_for` (the
username was chosen by whoever asked, maybe not the address's owner), and
`auth/signup_exists` only `email`. `check` reports
these routes mounted without messaging and mail, or with a user model
without `EMAIL_FIELD` and `email_verified`.

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
| `IsAuthenticatedOrReadOnly` | Anyone for `GET`, `HEAD`, `OPTIONS`; users otherwise. A WebSocket handshake is a `GET`, so anyone may open one: guard sockets that take writes with `IsAuthenticated`. |
| `IsAdminUser` | Staff users (`is_staff`), with credentials that carry no scopes. |
| `IsSuperuser` | Active superusers, with credentials that carry no scopes. |
| `HasPermissions(*keys)` | Users granted every permission (by any backend). |
| `ModelPermissions(model)` | Django's model permissions by method: `GET`/`HEAD` need `view`, `POST` `add`, `PUT`/`PATCH` `change`, `DELETE` `delete`; `OPTIONS` needs a user, and a WebSocket handshake `view`. `model` is a mapped class or `"<domain>.<model>"`. |
| `HasObjectPermissions(*keys)` | Users granted every permission on the object, by a backend with object permissions. The request itself needs an active user who logged in; check each object with `check_object_permissions`. |
| `HasScope(*keys)` | Users whose credentials' scopes cover every key; credentials without scopes cover all. |
| `IsUnscoped` | Users whose credentials carry no scopes (a login, not a scoped token). |

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

The framework's own routes (`/health`, `/ready`, `/metrics`, and the
`API_DOCS` pages) and the login and refresh endpoints are covered already.
Applications mounted with routes of their own (another `FastAPI`, a
Starlette `Mount` of routes) are checked route by route. What takes no
dependencies, a mounted `StaticFiles` or another ASGI application, and
Starlette's own `Route` and `WebSocketRoute`, cannot declare a rule, so it
is listed (`MOUNT /static`) until you serve it from a FastAPI route with
`Require(...)`, or mark it public with `mark_public`:

```python
from starlette.staticfiles import StaticFiles

from domainkit.contrib.auth import mark_public

router.mount("/static", mark_public(StaticFiles(directory="static")))
```

`AUTH.REQUIRE_ROUTE_RULES = False` turns the rule off; routes that read
the user still need `AuthenticationMiddleware`, and startup reports them
while it is missing. `open_routes(app)` returns the list, for a test of
your own.

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
`READ_ONLY_FIELDS` (`email`, `first_name`, `last_name`, `date_joined`, and
`email_verified` for `AbstractUser`). `fields` in a request changes only those in
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
| `LOGIN_RATE` | `"10/minute"` | Login and password-change attempts per client address and per username, and checks of link tokens per client address; `None` for no limit. |
| `EMAIL_RATE` | `"5/hour"` | Requests that send mail (resets, verifications, signups) per client address and per email address or user. |
| `PASSWORD_RESET_TIMEOUT` | 1 hour | How long a reset link works. |
| `PASSWORD_RESET_URL` | `None` | Your page that takes a reset token: `{token}` marks where. Staging and production refuse `http://`, here and in the other link URLs. |
| `EMAIL_VERIFICATION_TIMEOUT` | 3 days | How long a verification link works. |
| `EMAIL_VERIFICATION_URL` | `None` | Your page that takes a verification token. |
| `SIGNUP_ENABLED` | `False` | Let anyone ask for an account at `/signup`. |
| `SIGNUP_URL` | `None` | Your page that takes a signup link's token: `{token}` marks where. |
| `COOKIE` | see below | Browser sessions, for `CookieAuthentication`. |
| `MFA` | see below | Second factors (`auth.mfa`). |
| `CLIENT_CERT` | see below | Client certificates, for `ClientCertificateAuthentication`. |
| `CSRF_TRUSTED_ORIGINS` | `()` | Origins (`https://app.example.com`, `https://*.example.com`) whose pages may send cookie requests, besides the request's own and `ALLOWED_HOSTS`. |
| `REQUIRE_ROUTE_RULES` | `True` | Every route must say who may call it (`Require`, a user parameter, or `Public`); see "Deny by default". |
| `JWT` | see below | JSON Web Tokens, for `JWTAuthentication`. |

| `AUTH["COOKIE"]` key | Default | Meaning |
| --- | --- | --- |
| `NAME` | `None`: `"__Host-dk_session"` | The session cookie (HttpOnly). By default `__Host-dk_session` where the prefix holds (Secure, no `DOMAIN`, the `PATH` `/`), else `dk_session`. `__Host-` names need `SECURE`, no `DOMAIN`, and the `PATH` `/`. |
| `CSRF_NAME` | `None`: `"__Host-dk_csrf"` | The cookie scripts read the CSRF token from; `__Host-dk_csrf` or `dk_csrf`, as `NAME`. |
| `CSRF_HEADER` | `"X-CSRF-Token"` | The header they send it back in. |
| `SAMESITE` | `"lax"` | `lax`, `strict`, or `none` (which needs `SECURE`). |
| `SECURE` | `None` | HTTPS only; `None` means unless `DEBUG`. `False` is refused in staging and production. |
| `DOMAIN` | `None` | `None` keeps the cookies to this host. |
| `PATH` | `"/"` | |
| `TTL` | 14 days | How long a browser session works. |

| `AUTH["MFA"]` key | Default | Meaning |
| --- | --- | --- |
| `ISSUER` | `None` | The name authenticator apps show; `PROJECT_NAME` when `None`. |
| `CHALLENGE_TTL` | 5 minutes | How long the step between the password and the code may take (an hour at most). |
| `RECOVERY_CODES` | `10` | Recovery codes each user gets (1 to 50). |
| `WINDOW` | `1` | Steps of 30 seconds either side of now whose codes count (0 to 10). |
| `RATE` | `"10/hour"` | Codes checked per user, and per client address at `/mfa/login`; `None` for no limit. |

| `AUTH["CLIENT_CERT"]` key | Default | Meaning |
| --- | --- | --- |
| `MATCH` | `"fingerprint"` | `fingerprint`: the users `addcertificate` registered; `subject`: the common name is the username. |
| `HEADER` | `None` | The header a proxy of `PROXIES` sets to the URL-encoded PEM; `None` takes certificates from the ASGI TLS extension only. Needs `PROXIES`. |
| `VERIFY_HEADER` | `None` | The header in which the proxy says `SUCCESS` (nginx's `$ssl_client_verify`). Required with `HEADER` and `MATCH = "subject"`. |
| `PROXIES` | `()` | The addresses of the proxies that check certificates and set `HEADER`; requests from anywhere else that send it get 401. Single addresses in staging and production. |

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
| `createtoken` | `username`, `--name`, `--days` (90), `--no-expiry`, `--scope` | Print a new opaque token for the user's scripts; it is shown once. Each `--scope KEY` limits it to that permission, which the user must have. |
| `cleartokens` | `--user` | Delete expired tokens (hourly with `domainkit.contrib.messaging`), or every token of one user. |
| `addcertificate` | `username`, `certificate`, `--name` | Let a client certificate (a PEM file) log in as the user, by its SHA-256; prints the fingerprint. |
| `resetmfa` | `username` | Turn a user's second factor off and end their sessions, for a user who lost the app and the recovery codes. |

Code that sets passwords itself ends the user's sessions with
`await uow.tokens.revoke_user(user.id)`.

In tests, log in through the API, or issue a token directly:

```python
async with AuthUnitOfWork() as uow:
    token = await uow.tokens.issue(user.id, ttl=None)
    await uow.commit()
client.get("/invoices", headers={"Authorization": f"Bearer {token}"})
```
