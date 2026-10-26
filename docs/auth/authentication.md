# Authenticating requests

`AuthenticationMiddleware` finds who sent each request, before anything
counts or records it. It tries `AUTH.AUTHENTICATION_CLASSES` in order:

- the user becomes `request.user` (`AnonymousUser` without credentials),
  and the `Credentials` used become `request.auth`;
- the user's id becomes the request's actor, so rate limits, idempotency
  keys, logs, and the audit trail are per user, not per address. The actor
  lives in the context `RequestContextMiddleware` makes, so startup
  refuses a `MIDDLEWARE` without it, or with it listed after
  `AuthenticationMiddleware`;
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

## Opaque tokens

`TokenAuthentication`, the default, accepts tokens such as
`dkt_6R2v...`: `AUTH.TOKEN_PREFIX` and 256 random bits. Only a token's
SHA-256 is stored, so the table never holds a working token. A request
looks its token up, or finds it in the
[principal cache](#the-principal-cache): logging out, a password change,
or `cleartokens --user` ends it at once in the process that made the
change, and in the others within `AUTH.PRINCIPAL_CACHE.TTL` (5 seconds),
or at once with `STAMPS`. A login's token works for
`AUTH.TOKEN_TTL` (14 days); `createtoken` makes longer-lived ones for
scripts and other services. The prefix lets secret scanners and people
recognize a leaked token.

## Personal tokens and scopes

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
`permissions`, only those both the user and the scopes allow.
`createtoken --scope KEY` makes scoped tokens from the command line.
Creating and revoking are audited (`auth.token_created` with the scopes,
`auth.token_revoked`).

### Scopes on every route

A scoped token is bounded on every route, not only on routes that check a
permission. Before 0.10 it passed any route guarded by authentication
alone (`Require()`, `IsAuthenticated`, `IsAuthenticatedOrReadOnly`,
`AuthenticatedUser`): a key limited to `shop.view_product` could write
wherever no permission was checked. A route reached with scoped
credentials must now declare a rule that checks scopes, on itself or a
router it is included through:

- `HasScope(...)`, a permission rule (`HasPermissions`, `ModelPermissions`,
  `HasObjectPermissions`), `IsUnscoped`, `IsAdminUser`, or `IsSuperuser`;
- or `AllowScoped()`, for a route whose work no scope names and none needs
  to (`/logout` and `/me` say it): it lets any principal who proved who
  they are through, scoped or not, on purpose, as `Public` does for
  anonymous callers;
- or a rule of your own that reads `scopes_of(request)` and sets
  `bounds_scopes = True`. `A & B` checks scopes if either does, `A | B`
  only if both do, and `~A` never.

`SCOPED_CREDENTIALS` decides what happens on a route that declares none.
By default (`"refuse"`, since 0.11) it answers `403 permission_denied`
("Credentials limited to scopes cannot call this route"). `"warn"`, 0.10's
default, serves the request as before, with a line on the
`domainkit.security` logger once per route a while: a project that still
has such routes keeps them working while it adds their rules. It is
decided once a request,
wherever the principal is first read: `Require`, `CurrentUser`,
`AuthenticatedUser`, and `current_user` itself, so a dependency of your
own that reads the principal through it, and a `Public` route that reads
it, are held too. `CurrentUser` and `Require` record the refusal in the
audit trail; a synchronous `current_user` refuses without the record, so
a dependency of your own may `await check_scopes_bounded(request)` first.
A middleware's principal read straight from `scope["user"]` is not
checked.

`HasObjectPermissions` checks the scopes on the request as well as on
each object, so a key without the permission cannot list what no single
object check looks at; `ModelPermissions` lets scoped credentials make an
`OPTIONS` request only with the model's `view` permission in a scope.

Every token records when it last authenticated a request
(`last_used_at`), written at most once every five minutes (a tenth of
`AUTH.IDLE_TIMEOUT` when that is shorter), so a key no one uses shows up
without a write per request.

## JSON Web Tokens

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

## Browser sessions in cookies

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

## Client certificates (mutual TLS)

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

## Other credentials

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

## The principal cache

Each request's credential names a user, with groups and permissions: its
principal. Reading it took four or five queries in a transaction of its
own, about 70% of an authenticated read. Now the first request of a
credential reads it in two (the token, then the user with every group and
permission), and the next ones find it in memory, with no query at all,
for `AUTH.PRINCIPAL_CACHE.TTL` (5 seconds). Opaque tokens, personal
tokens, browser sessions, JSON Web Tokens' logins, and client
certificates are all kept this way; a request over its rate limit is
refused without a query too. ADR
[0058](../adr/0058-cache-principals-and-stamp-revocations.md) explains the
design, and [0059](../adr/0059-read-principals-in-two-queries-without-the-hash.md)
how principals are read.

- **What is kept**: the user's columns, their groups and permissions,
  and the credential's kind, expiry, scopes, and login. Never a second
  factor's secret, recovery codes, or the token itself: entries are keyed
  by the token's digest. The password hash is kept, in process memory, only
  while `request.user.password` is deprecated (below), and never shows in a
  `repr`, a log, a key, or a stamp. Each request gets its own user, built
  from what is kept, the same as a query builds; its groups and
  permissions are built the first time the request reads them, and
  `has_perm` (and the rules that ask it) answers from the keys of what the
  user holds, worked out once per principal, until then (0.10, ADR 0059's
  amendment): a user with hundreds of permissions costs a request what a
  user with two does.
- **One read at a time, and read again ahead** (0.10, ADR 0058's
  amendment): requests of one credential that miss at once wait for one
  read, unless a change was invalidated since it began. In the last fifth
  of the `TTL`, the first request to use a kept principal reads it again
  while the others are served it, never past the `TTL`; if what it reads
  differs, the kept one is dropped at once.
- **What every request checks**: a token's expiry, on `domainkit.clock`
  (an expired token is refused from memory as from the table); a cookie's
  CSRF checks; a JSON Web Token's signature and times. `last_used_at` is
  still written once every five minutes, by one request.
- **Bounds**: `MAX_ENTRIES` principals per process (10,000 by default,
  100,000 at most; the least recently used go first), each for `TTL` at
  most (5 minutes at most). Principals share their grants: a user's
  credentials, and the users of a group, hold one copy of its
  permissions, so an entry costs a few kilobytes whatever the role holds
  (5.6 kB each for 200 tokens of a user with 1,000 permissions, was
  368 kB). `AUTH.MAX_LIVE_TOKENS` bounds the tokens one user can make.
- **In the process that makes a change, at once**: logging out, revoking
  a token (or all of a user's), a password change or reset, deactivating
  or deleting a user, joining or leaving a group, a permission given or
  taken back (directly or through a group), renaming or deleting a group,
  a new login (it changes `last_login`), and erasure. Repositories and the
  ORM mark what each commit changes, so the API, the admin API, the
  management commands, and your own units of work all count. So do bulk
  statements a session executes on the tables principals are read from
  (users, their groups and permissions, groups, permissions, content
  types, tokens): `update_rows`, `delete_rows`, `update(User)`, or a Core
  `UPDATE`, `DELETE`, or link `INSERT`. They drop every principal at the
  commit. It is the transaction's commit that counts, not a savepoint's
  release, and a principal read while such a change commits is not kept.
- **In other processes**: within `TTL`. To make it at once, name a Redis
  cache that every process shares in `STAMPS`: a commit stamps what it
  changed there, and each request compares its principal's stamps, one
  round trip instead of two queries:

  ```python
  CACHES = {
      "default": {"URL": "memory://"},
      "shared": {"URL": env.str("CACHE_URL"), "KEY_PREFIX": "billing"},
  }
  AUTH = {"PRINCIPAL_CACHE": {"STAMPS": "shared", "TTL": 60}}
  ```

  Every service that reads the same auth tables must name the same Redis
  and the same `KEY_PREFIX`, or it never sees the others' stamps. Give
  that Redis `maxmemory-policy noeviction` (or enough memory): an evicted
  or lost stamp is safe, as it reads as a change, but every principal
  resting on it is then read again. Each commit stamps twice: before the
  commit, then after it, in a task a cancelled request does not stop
  (bounded by 5 seconds). A commit outside a unit of work
  (`connections.session()`) stamps too. A principal is kept with the
  stamps it was read with, and a stamp that is absent when it is read is
  given one first: a stamp the cache later loses (a restart without
  persistence, an eviction, `FLUSHDB`) never matches it again.

- **When the cache fails**: a request whose stamps cannot be read uses
  nothing kept, and reads its principal from the database, which it does
  not keep; nothing stale is ever served past the TTL. A stamp that
  cannot be written is logged (security logger): the change then reaches
  other processes within the TTL.
- **Changes outside domainkit**: SQL run on a connection outside any
  session (`connections.engine().begin()`, `loaddata`, a snapshot
  restore), or another service writing the tables without these stamps,
  is seen within the TTL. Drop what it changed yourself to see it at once, with
  `from domainkit.contrib.auth import principals`:
  `principals.invalidate([principals.user_target(user_id)])`, or
  `principals.invalidate_all()`, in this process; with `STAMPS`,
  `await principals.invalidate_everywhere(targets, auth_settings().PRINCIPAL_CACHE)`
  reaches the others too.
- **Open connections** are re-checked through the cache too, so they see a
  change as soon as requests do.
- **`request.user.password` was removed in 0.10.** The principal holds no
  hash, and the query does not read one: reading it raises
  `AttributeError`, naming what replaces it (so
  `getattr(user, "password", None)` is None). To check a password, read the
  hash afresh: `await password_holds(user, raw)`
  (`domainkit.contrib.auth.api`), or `await uow.users.password_hash(user.id)`.
  `has_usable_password()` keeps working, without the hash. A user's `repr`
  leaves the password out.
- A user model that loads relationships of its own with the user (other
  than `groups` and `user_permissions`) is read by the ORM every time, and
  never kept.

`check` warns, in staging and production, about a `TTL` above 5 seconds
without `STAMPS`: a revocation would keep working that long in the other
processes. `STAMPS` naming anything but a Redis cache of `CACHES` stops
startup. With `domainkit[metrics]`, `auth_principal_cache_total` counts
hits, misses, invalidations, and evictions, requests served by another's
read (`coalesced`), and reads ahead (`refresh`, `refresh_failed`), by
authentication class (`token`, `cookie`, `jwt`, `certificate`), never by
user.
`ENABLED = False` reads the principal on every request, in the same two
queries.

