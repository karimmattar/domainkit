# Authenticating requests

Authenticating a request means finding out who sent it.
`AuthenticationMiddleware` does it for every request, before anything
counts or records the request. A client proves who it is with a token,
a browser session cookie, or a client certificate. This page explains
each kind of credential, the limits you can put on a token, and how the
user behind a credential is kept between requests.

On this page you learn how to:

- send a token, and read what a bad token gets;
- let users make personal tokens limited to some permissions (scopes);
- use JSON Web Tokens, and replace their signing key;
- keep a browser's session in a cookie, safe from forged requests;
- accept client certificates, or credentials of your own;
- tune the principal cache that saves a query per request.

## A first example

A client logs in at `POST /auth/login`, and sends the token it gets in
the `Authorization` header:

```python
from fastapi.testclient import TestClient

from domainkit.http import get_asgi_application

with TestClient(get_asgi_application()) as client:
    login = client.post(
        "/auth/login", json={"username": "alice", "password": "correct horse battery"}
    )
    token = login.json()["access_token"]
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    print(me.json()["username"])
    bad = client.get("/catalog", headers={"Authorization": "Bearer dkt_" + "A" * 43})
    print(bad.status_code, bad.headers["WWW-Authenticate"])
    print(bad.json()["code"], bad.json()["detail"])
```

```text
alice
401 Bearer realm="api", error="invalid_token"
authentication_failed Invalid token.
```

`/catalog` is a public route, yet the bad token gets a 401. A client
never mistakes a bad token for being anonymous.

## What the middleware does

`AuthenticationMiddleware` tries the classes of
`AUTH.AUTHENTICATION_CLASSES` in order:

- **The user becomes `request.user`**, or `AnonymousUser` when the request
  has no credentials. The `Credentials` used become `request.auth`.
- **The user's id becomes the request's actor.** So rate limits,
  idempotency keys, logs, and the audit trail count per user, not per
  client address. The actor lives in the request context that
  `RequestContextMiddleware` makes, so startup refuses a `MIDDLEWARE`
  without it, or with it after `AuthenticationMiddleware`.
- **Credentials that a class recognizes but that do not hold** (an
  unknown, expired, or revoked token; an inactive user) get a 401 with
  `WWW-Authenticate: Bearer realm="api", error="invalid_token"`, even on
  public routes.
- **Credentials that no class recognizes** are left to the route
  (`/metrics` has its own token).
- **`AUTHENTICATION_EXEMPT_PATHS`** (`/health`, `/ready`, `/metrics`) are
  served without looking at credentials at all.

`Credentials` holds `user`, `scheme` (`token`, `jwt`, `cookie`,
`certificate`, or your class's), `token_id` (the digest of the token),
`login`, `scopes`, and `impersonator` (the staff member, during an
[impersonation](routes.md#impersonation)). Its `repr` shows the user's
id, the scheme, and the scopes, never a token.

WebSocket handshakes are authenticated the same way, and refused with
close code 1008. The user is the connection's actor, so the audit trail
records what the connection does under their id. `Require(...)` and
`AuthenticatedUser` guard WebSocket routes as they guard others: a
refused handshake gets the same 401 or 403 answer, before the socket
opens. An open connection has its credentials checked again every
`LIVE_REAUTH_INTERVAL`, so a revoked token or a deactivated user closes
it.

## Opaque tokens

`TokenAuthentication`, the default class, accepts tokens such as
`dkt_6R2v...`: `AUTH.TOKEN_PREFIX` and 256 random bits. The prefix lets
secret scanners and people recognize a leaked token.

- **Only a digest is stored.** The table keeps each token's SHA-256, so
  it never holds a working token.
- **A login's token works for `AUTH.TOKEN_TTL`** (14 days).
  `createtoken` makes tokens for scripts and other services, and users
  make their own [personal tokens](#personal-tokens-and-scopes).
- **Revoking is fast.** A request finds its token in the table, or in the
  [principal cache](#the-principal-cache). Logging out, a password change,
  or `cleartokens --user` ends a token at once in the process that made
  the change, and in the others within `AUTH.PRINCIPAL_CACHE.TTL`
  (5 seconds), or at once with `STAMPS`.
- **A token of the wrong form costs no query.** A value that cannot be an
  issued token is refused before any lookup.

The table holds one `AuthToken` per credential: its digest (the `id`),
`user_id`, `kind`, `name`, `scopes`, `created_at`, `expires_at`, and
`last_used_at`. `TokenKind` names the kinds: `access`, `refresh`,
`session`, `certificate`, and `device` (a device proof, which never
authenticates a request). `uow.tokens.issue(user_id, ttl=..., name=..., scopes=...)`
stores a new token and returns its value, shown only then:

```python
from datetime import timedelta

from domainkit.contrib.auth import AuthUnitOfWork

async with AuthUnitOfWork() as uow:
    raw = await uow.tokens.issue(
        user.id,
        ttl=timedelta(days=30),
        name="nightly export",
        scopes=frozenset({"shop.view_product"}),
    )
    await uow.commit()
```

Every token records when it last authenticated a request
(`last_used_at`). It is written at most once every 5 minutes (a tenth of
`AUTH.IDLE_TIMEOUT` when that is shorter), so you can find a token that
nobody uses, without a write on every request.

## Personal tokens and scopes

A personal token is a token that a user makes for a script, like
GitHub's personal access tokens. Mount the router that lets users list,
make, and revoke their own:

```python
from domainkit.contrib.auth.personal_tokens import router as tokens_router

router.include_router(tokens_router, prefix="/auth")
```

A user makes a token limited to `shop.view_product`. A user without a
second factor sends the password again:

```http
POST /auth/tokens
Authorization: Bearer <a login token>

{"name": "stock report", "scopes": ["shop.view_product"], "password": "correct horse battery"}
```

```json
{
  "id": "a770678eaa21784dcbbfb147637b4033f8a67c7fd68b6ce3d1b32db2638b70ec",
  "name": "stock report",
  "kind": "access",
  "scopes": ["shop.view_product"],
  "created_at": "2026-11-04T18:25:13.288706Z",
  "expires_at": "2027-02-02T18:25:13.288706Z",
  "last_used_at": null,
  "current": false,
  "token": "dkt_XrIMcWKZG0EA6qCRF4M-IOiGenpcDIj9_-qaWJ9Hx1E"
}
```

| Endpoint | Body | Answer |
| --- | --- | --- |
| `GET /tokens` | `?limit=50&cursor=` | `{"items", "next_cursor"}`: the user's tokens, newest first, each with `id` (the digest), `name`, `kind` (`access`, `session`, `refresh`, `certificate`), `scopes`, `created_at`, `expires_at`, `last_used_at`, and `current` for the one this request uses. At most 100 a page. |
| `POST /tokens` | `{"name", "scopes", "expires_in_days", "password"}` | `201`, with `token`: the value, shown this once. |
| `DELETE /tokens/{id}` | | `204`; `404 token_not_found` for a token that is not the user's. A refresh token takes its JSON Web Token login with it. |

Rules of `POST /tokens`:

- **The answer is shown once.** It is marked `Cache-Control: no-store`,
  so no cache or idempotency key keeps it. A retry with the same
  idempotency key gets `409`.
- **The user authenticates again.** A user with a second factor steps up
  (`POST /auth/step-up`) within the 5 minutes before, or gets
  `401 step_up_required`. A user without one sends `password`:
  `400 password_required` without it, `400 wrong_password` for a wrong
  one, counted with their other password checks. A token outlives the
  login that made it, so a stolen session must not be enough to make one.
- **The lifetime is bounded.** `expires_in_days` is 90 when you leave it
  out, or `AUTH.PERSONAL_TOKEN_MAX_AGE` if that is shorter. More than
  that, or `null` for no end, is `422 token_lifetime_too_long`, unless
  the setting is `None`.
- **`name`** is one line of at most 100 characters, and may not be
  `login`, the name of logins' tokens.
- **Scopes are permissions the user holds.** At most 100 permission keys.
  A scope the user lacks is `403 scope_not_held`.
- **A user holds at most `AUTH.MAX_LIVE_TOKENS`** live tokens (100); past
  it, `409 too_many_tokens`.

In the [phone modes](ciba.md#managing-logins-from-the-phone), a user
with a bound device makes a personal token only by a request the device
signs (`403 phone_required` otherwise, since 2.2.2).

A personal token is not a login. `IDLE_TIMEOUT` and `MAX_LOGINS` do not
end it, and neither does logging out of the session that made it
(`DELETE /session`, `/logout` without `everywhere`). A password change
or reset, turning on a second factor, `/logout` with `everywhere`, and an
admin's change end it, as they end every token of the user. Making and
revoking tokens are audited (`auth.token_created` with the scopes,
`auth.token_revoked`). `createtoken --scope KEY` makes scoped tokens from
the command line, with any lifetime.

### What scopes allow

A token's scopes only narrow what its user may do. A request with a
scoped token may do what both the user and the scopes allow. A
permission taken from the user later is gone from the token too. With
the token above:

```text
GET  /products   200   needs shop.view_product: in the scopes
POST /products   403   needs shop.add_product: the user has it, the scopes do not
GET  /orders     403   Credentials limited to scopes cannot call this route: it declares no rule that checks scopes.
GET  /auth/me    200   "permissions": ["shop.view_product"]
```

- `HasPermissions`, `ModelPermissions`, and `HasObjectPermissions` check
  both the user and the scopes.
- `HasScope(*keys)` checks the scopes alone. Credentials without scopes
  allow every scope.
- `IsAdminUser` and `IsSuperuser` refuse scoped credentials, because staff
  and superusers hold more than any scope names.
- `IsUnscoped` keeps a route to credentials without scopes. These routes
  take only those: the admin API, the token routes, `/password`, the
  second factor's routes, `/email-verification`, and `/logout` with
  `everywhere`. So a scoped token can neither make a token with more,
  grant anything, nor change or lock its account.
- `/me` with a scoped token lists, in `permissions`, only those that both
  the user and the scopes allow.

### Scopes on every route

A scoped token is bounded on every route, not only on routes that check
a permission. Before 0.10 it passed any route guarded only by a login
(`Require()`, `IsAuthenticated`, `IsAuthenticatedOrReadOnly`,
`AuthenticatedUser`): a key limited to `shop.view_product` could write
wherever no permission was checked. Now a route reached with scoped
credentials must declare a rule that checks scopes, on itself or on a
router it is included through:

- `HasScope(...)`, a permission rule (`HasPermissions`,
  `ModelPermissions`, `HasObjectPermissions`), `IsUnscoped`,
  `IsAdminUser`, or `IsSuperuser`;
- or `AllowScoped()`, for a route whose work no scope names and none
  needs to (`/logout` and `/me` use it). It lets in, on purpose, any
  principal who proved who they are, scoped or not, as `Public` lets in
  anonymous callers;
- or a rule of your own that reads `scopes_of(request)` and sets
  `bounds_scopes = True`. `A & B` checks scopes if either rule does,
  `A | B` only if both do, and `~A` never.

The setting `SCOPED_CREDENTIALS` decides what happens on a route that
declares no such rule:

- `"refuse"`, the default since 0.11, answers `403 permission_denied`,
  as above.
- `"warn"`, the default of 0.10, serves the request, and logs a line on
  the `domainkit.security` logger once per route for a while. Use it while
  you add the rules to an older project.

The check runs once per request, wherever the principal is first read:
`Require`, `CurrentUser`, `AuthenticatedUser`, and `current_user` itself.
So a dependency of your own that reads the principal through them is
held too, and so is a `Public` route that reads it. Live events check it
before the first event (`live_events.stream`, `forward`, and
`subscribe`). `CurrentUser` and `Require` record the refusal in the
audit trail. A synchronous `current_user` refuses without the record, so
a dependency of your own may `await check_scopes_bounded(request)` first.
A middleware that reads the principal straight from `scope["user"]` is
not checked.

`HasObjectPermissions` checks the scopes on the request as well as on
each object, so a key without the permission cannot list what no single
object check looks at. `ModelPermissions` lets scoped credentials make an
`OPTIONS` request only with the model's `view` permission in a scope.

## JSON Web Tokens

`JWTAuthentication` issues short-lived signed access tokens and opaque
refresh tokens. It needs the `jwt` extra (`pip install "domainkit[jwt]"`):

```python
AUTH = {
    "AUTHENTICATION_CLASSES": [
        "domainkit.contrib.auth.authentication.JWTAuthentication",
        "domainkit.contrib.auth.authentication.TokenAuthentication",  # createtoken
    ],
    "JWT": {"SIGNING_KEY": env.str("JWT_SIGNING_KEY")},
}
```

The first class issues the login tokens, so `/auth/login` now answers:

```json
{"access_token": "eyJhbGciOiJIUzI1NiIsImtpZCI6IjJaRWVIcmVZNWxTUzA4NUR2OThVa3ciLCJ0eXAiOiJKV1QifQ...", "token_type": "Bearer", "expires_in": 300, "refresh_token": "dkt_PaXVehuN8IEOBwoxLoAX4lTHrXIOLzoiVHA4mw5OHGg"}
```

The access token's header and claims:

```text
{'alg': 'HS256', 'kid': '2ZEeHreY5lSS085Dv98Ukw', 'typ': 'JWT'}
{'sub': '19007565-e3cf-4672-8e89-bc6abb0e74c4', 'iat': 1793816724, 'exp': 1793817024, 'jti': '9d8f818a7f4f484faf20f85794a02cd7', 'sid': '8a7fc076-70b4-4fb6-b0b0-fc97236a15df'}
```

- **An access token** carries `sub` (the user id), `sid` (the login),
  `iat`, `exp`, `jti`, and `iss` and `aud` when they are set. Its header
  names its key (`kid`). It is checked with that key's algorithm only,
  whatever its own header says. It works for `ACCESS_TOKEN_TTL`
  (5 minutes), and only while its login lasts: logging out ends it at
  once.
- **A refresh token works once**, at `POST /auth/refresh`, for a new
  pair. The new refresh token expires with the login
  (`REFRESH_TOKEN_TTL`, 14 days), so a user logs in again at least that
  often.
- **A used refresh token that comes back** means that someone else may
  hold a copy. The whole login ends, `RefreshTokenReused` is recorded,
  and the refresh answers `401` "Refresh token reused; log in again.".
  The login's access tokens then get "Session ended; log in again.".
- **Keys.** HMAC keys (`HS256`, `HS384`, `HS512`) must be at least as
  long as the hash: 32, 48, or 64 bytes. For `RS*`, `PS*`, `ES*`, and
  `EdDSA`, set `SIGNING_KEY` to the private key and `VERIFYING_KEY` to
  the public key, in PEM. Other services can then check access tokens
  with the public key alone. Startup signs and verifies a token, so wrong
  keys fail early.

With opaque tokens, `/auth/refresh` answers `400 refresh_not_supported`.

### Replacing a key

Each token names the key that signed it. So a new key can sign while the
old one still verifies what it signed, for one token lifetime, and no
client is logged out
([ADR 0097](../adr/0097-name-the-key-of-every-json-web-token-and-retire-keys-within-a-token-lifetime.md)):

```python
AUTH = {
    "JWT": {
        "SIGNING_KEY": env.str("JWT_SIGNING_KEY"),  # The new key.
        "PREVIOUS_KEYS": [
            {
                "KEY": env.str("JWT_OLD_KEY"),  # The old secret, or public key.
                "RETIRED_AT": "2026-11-02T09:30:00Z",  # When the rollout ends.
            },
        ],
    },
}
```

1. Make the new key. Deploy it as `SIGNING_KEY`, with the old one in
   `PREVIOUS_KEYS`. `RETIRED_AT` is when the last process stops signing
   with the old key: the end of the rollout, at most a day ahead
   (`auth.E009` refuses later). For `RS*`, `PS*`, `ES*`, and `EdDSA`,
   the old key is its public key; a private key is refused. Give
   `ALGORITHM` too when the old key signed with another algorithm than the
   new one, as from `HS256` to `ES256`.
2. Tokens that the old key signed work until `RETIRED_AT` plus
   `ACCESS_TOKEN_TTL` and `LEEWAY`, and are refused after, expired or
   not. Refresh tokens are not signed, so they keep working: a client
   refused after that time refreshes, and gets a token of the new key.
3. After that time, remove the entry, and destroy the old key.

A key that leaked is not listed. Replace it alone: it verifies nothing
from the next request on, and every client refreshes once.

A token's `kid` comes from its key and shows nothing of it. For an HMAC
secret, it is the first 16 bytes of HMAC-SHA256 of `domainkit:jwt-key-id`
under the secret. For a public key, it is the key's JWK thumbprint
(RFC 7638, SHA-256), which another service that holds the public key
computes the same way. Both are in base64url without padding. A token
whose `kid` names no key is refused without trying another key. At most
3 previous keys are kept. `check` refuses a previous key that is the
signing key, or one listed twice (`auth.E008`).

Tokens without `kid`, issued by 0.10, are checked with the signing key
alone, so upgrading logs nobody out. They expire within
`ACCESS_TOKEN_TTL`. Replace the key no sooner than that after upgrading:
a previous key never checks them.

The JSON Web Token keys are apart from `SECRET_KEY`
([ADR 0028](../adr/0028-one-secret-key-signs-and-derives.md)). Replacing
`SECRET_KEY`, with `SECRET_KEY_FALLBACKS`, never touches tokens, and
`PREVIOUS_KEYS` never touches what `SECRET_KEY` signs.

## Browser sessions in cookies

A browser app should not keep tokens where scripts can read them.
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

List `CookieAuthentication` after the classes that read headers: it
issues no tokens for `/login`. `check` reports the session router
mounted without `CookieAuthentication` in `AUTHENTICATION_CLASSES`:

```text
auth.E102: Routes of domainkit.contrib.auth.cookies start browser sessions, but AUTH.AUTHENTICATION_CLASSES lacks domainkit.contrib.auth.cookies.CookieAuthentication.
```

| Endpoint | Body | Answer |
| --- | --- | --- |
| `POST /session` | `{"username", "password"}`, and `"new_password"` where the password must change | `200 {"csrf_token"}`, and two cookies: the session and the CSRF token. `202` with a challenge for a user with a second factor (then `POST /session/mfa`). `401`, `403 csrf_failed`, and `429`, as for `/login`. |
| `POST /session/mfa` | `{"mfa_token", "code"}` | The cookies, once the code holds ([Two-factor logins](logging-in.md#two-factor-logins)). |
| `GET /session` | | `200 {"csrf_token"}`: a new CSRF token for this session, also in its cookie; `400 no_session` without a session cookie. |
| `DELETE /session` | | `204`: the session ends, and both cookies are cleared. |

The session cookie holds an opaque token of the kind `session`, stored as
a digest like the others. It never works in an `Authorization` header.
Logging out, a password change, or `cleartokens --user` ends it. The
cookie is HttpOnly, `Secure` unless `DEBUG` (staging and production
refuse `COOKIE.SECURE = False`, `auth.E005`), and `SameSite=Lax` by
default.

### The CSRF checks

A request that the session authenticates, with a method other than
`GET`, `HEAD`, or `OPTIONS`, must:

- **come from a trusted origin.** Its `Origin` header (or `Referer`,
  without one) is the request's own origin, a host in `ALLOWED_HOSTS`
  with the request's scheme (a wildcard `*` trusts none), or one of
  `AUTH.CSRF_TRUSTED_ORIGINS` (`https://*.example.com` for subdomains). An
  HTTPS request that sends neither header is refused, because browsers
  always send one;
- **send the CSRF token** in `X-CSRF-Token` (`COOKIE.CSRF_HEADER`). It
  must equal the CSRF cookie and be signed with `SECRET_KEY` for this
  session. So a token set by another site or subdomain, or taken from
  another session, fails.

Anything else gets `403 csrf_failed`. Logging in at `/session` checks the
origin too. A WebSocket handshake with the session cookie must come from
a trusted origin (cross-site WebSocket hijacking). A request with an
`Authorization` header ignores the cookie, so clients that send header
tokens are not affected. A session that ended, expired, or belongs to an
inactive user leaves the request anonymous instead of refused, so a
stale cookie never stops the browser from logging in again.

### Cookie names

The cookies are named `__Host-dk_session` and `__Host-dk_csrf`. A browser
takes a cookie with the `__Host-` prefix only from the very host that
sets it, `Secure`, without `Domain`, on the path `/`. So a page on a
sibling subdomain (`evil.example.com` beside `api.example.com`) cannot
plant its own session or CSRF token in the browser (cookie tossing). It
can neither log the browser in as someone else nor fix its session.

Where the prefix cannot hold, because the cookies are not `Secure` (plain
HTTP under `DEBUG`), or have a `DOMAIN` or another `PATH`, they are named
`dk_session` and `dk_csrf`. `COOKIE.NAME` and `COOKIE.CSRF_NAME` set the
names yourself. In staging and production, `check` warns about a name
without the prefix where the prefix could hold (`auth.W005`).

A single-page app on another origin needs CORS with credentials:
`CORS_ALLOWED_ORIGINS = ["https://app.example.com"]`,
`CORS_ALLOW_CREDENTIALS = True`, and `X-CSRF-Token` added to
`CORS_ALLOWED_HEADERS`, keeping its defaults (`If-Match` and
`Idempotency-Key` among them).

## Client certificates (mutual TLS)

A service that calls the API can prove who it is with a client
certificate instead of a token. `ClientCertificateAuthentication` takes
the certificate from one of two places, and only these:

- **the ASGI TLS extension**, when the ASGI server ends TLS and checked
  the certificate itself (`scope["extensions"]["tls"]`). A certificate
  that the server says did not verify gets 401;
- **the header `AUTH.CLIENT_CERT.HEADER`**, holding the URL-encoded PEM,
  only on connections from `AUTH.CLIENT_CERT.PROXIES`, and only with
  `VERIFY_HEADER` saying `SUCCESS` when that is set. The proxy's own
  address counts, not the client's address that
  `ProxyHeadersMiddleware` puts in its place. A request from anyone else
  that sends the header gets 401: a client could otherwise name any
  certificate. Startup refuses `HEADER` without `PROXIES` (`auth.E001`).

`PROXIES` lists the exact addresses of the proxies that check
certificates, and nothing else. It is not `TRUSTED_PROXIES`, which often
holds a whole network and only decides whose `X-Forwarded-For` is
believed: any host of such a network could send the header with its own
`SUCCESS` and log in as anyone. Staging and production refuse networks
in `PROXIES`: each entry is one address, `/32` or `/128` (`auth.E007`).

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

The proxy must remove whatever a client sent in both headers, on every
request. `proxy_set_header` below replaces them with the proxy's own
values, and drops them when no certificate came. A proxy that passes a
client's headers on (a `location` without these lines, or another proxy
in front that forwards them) lets clients choose their certificate.

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

`AUTH.CLIENT_CERT.MATCH` decides who a certificate logs in as:

- **`"fingerprint"`**, the default: the user that
  `addcertificate <user> <cert.pem>` registered it for, by its
  SHA-256. `cleartokens --user` and `DELETE /auth/tokens/{id}` remove it,
  and so does whatever ends every token of the user (a new password, a
  reset, logging out everywhere). Register it again then.
- **`"subject"`**: the subject's common name is the username. Everything
  the CA signs then logs in as someone, so use it only with a CA of your
  own. It needs `cryptography`, from the `mfa` extra. Through `HEADER` it
  also needs `VERIFY_HEADER` (`auth.E002`): a proxy that passes on
  certificates it did not verify would let a self-signed one name any
  user.

Neither domainkit nor this class checks the chain or the dates: the
server or the proxy must check them, against the CA you trust. Requests
get `request.auth.scheme == "certificate"`.

## Other credentials

A class of your own subclasses `BaseAuthentication`. It returns
`Credentials` for its own credentials, `None` for others, and raises
`AuthenticationFailed` when its own do not hold:

```python
from uuid import UUID

from domainkit.contrib.auth import AuthUnitOfWork, BaseAuthentication, Credentials
from domainkit.contrib.auth.authentication import active_user


class GatewayAuthentication(BaseAuthentication):
    """Users named by the gateway in front of the API (trust it alone)."""

    guessable = False  # Only the gateway sets the header.

    async def authenticate(self, connection):
        user_id = connection.headers.get("x-gateway-user")
        if user_id is None:
            return None
        async with AuthUnitOfWork() as uow:
            return Credentials(await active_user(uow, UUID(user_id)), scheme="gateway")
```

`active_user(uow, user_id)` raises `AuthenticationFailed` for unknown and
inactive users. A class may also override:

| Member | What it does |
| --- | --- |
| `scheme`, `realm` | The `WWW-Authenticate` of a 401: `Bearer realm="api"` by default. |
| `challenge(error=None)` | Builds that header. |
| `guessable` | `True` by default. While a client address is held for refused credentials, credentials of a guessable class are refused even when they hold, so a guess that works never stands out. Set it `False` for credentials that no client can send at will. |
| `issue(uow, user)` | Tokens for a user who has logged in now, as `IssuedTokens(access_token, expires_in, refresh_token)`. Only the first class in the list issues them. |
| `refresh(uow, refresh_token)` | New tokens; the default raises `RefreshNotSupported` (`400 refresh_not_supported`). |

## Refused credentials and the client address

Refused credentials never reach `RateLimitMiddleware`, which counts
users. So the middleware counts them itself, per client address, against
`RATE_LIMIT`. Past the limit, the address gets 429. Its requests with an
`Authorization` header are then refused before any lookup, until
`Retry-After` has passed.

- **Credentials in another header**, which only a class of your own reads
  (an `X-API-Key`), are read, and refused with the same 429 even when
  they hold, unless the class sets `guessable = False`.
- **Session cookies and client certificates** cannot be guessed (a
  cookie that does not hold leaves the request anonymous; a certificate
  needs its private key). Those that hold are served while their address
  is held, so guesses from an address shared with browsers (an office,
  a carrier, a proxy) never log its users out.
- **Only credentials that do not hold (401) count.** A cookie request
  refused by the CSRF checks (403) is one that any other site can make a
  browser send. Counting it would let that site lock out everyone behind
  the browser's address.

The security log has a line when an address is held, and counts what it
refuses while it is held.

## The principal cache

Each request's credential names a user, with groups and permissions: its
principal. Reading it took four or five queries, about 70% of an
authenticated read. Now the first request of a credential reads it in
two queries (the token, then the user with every group and permission).
The next ones find it in memory, with no query at all, for
`AUTH.PRINCIPAL_CACHE.TTL` (5 seconds). Opaque tokens, personal tokens,
browser sessions, JSON Web Tokens' logins, and client certificates are
all kept this way. A request over its rate limit is refused without a
query too. ADR [0058](../adr/0058-cache-principals-and-stamp-revocations.md)
explains the design, and
[0059](../adr/0059-read-principals-in-two-queries-without-the-hash.md)
how principals are read.

- **What is kept**: the user's columns, their groups and permissions, and
  the credential's kind, expiry, scopes, and login. Never a second
  factor's secret, recovery codes, or the token itself: entries are keyed
  by the token's digest. The password hash is not kept, only whether a
  password can log the user in. Each request gets its own user, built
  from what is kept. `has_perm` answers from the keys of what the user
  holds, worked out once per principal, so a user with hundreds of
  permissions costs a request what a user with two does.
- **One read at a time, and read again ahead.** Requests of one
  credential that miss at once wait for one read, unless a change was
  invalidated since it began. In the last fifth of the `TTL`, the first
  request to use a kept principal reads it again, while the others are
  served the kept one, never past the `TTL`. If what it reads differs,
  the kept one is dropped at once.
- **What every request checks**: a token's expiry (an expired token is
  refused from memory as from the table), a cookie's CSRF checks, and a
  JSON Web Token's signature and times. `last_used_at` is still written
  once every 5 minutes, by one request.
- **Bounds**: `MAX_ENTRIES` principals per process (10,000 by default,
  100,000 at most; the least recently used go first), each for `TTL` at
  most (5 minutes at most). Principals share their grants, so an entry
  costs a few kilobytes whatever the role holds. `AUTH.MAX_LIVE_TOKENS`
  bounds the tokens one user can make.
- **A change reaches the process that made it at once.** Logging out,
  revoking a token (or all of a user's), a password change or reset,
  deactivating or deleting a user, joining or leaving a group, a
  permission given or taken back (directly or through a group), renaming
  or deleting a group, a new login, and erasure all drop what they
  change. Repositories and the ORM mark what each commit changes, so the
  API, the admin API, the commands, and your own units of work all
  count. So do bulk statements a session runs on the tables that
  principals are read from (`update_rows`, `delete_rows`,
  `update(User)`, or a Core `UPDATE`, `DELETE`, or link `INSERT`): they
  drop every principal at the commit. The transaction's commit counts,
  not a savepoint's release.
- **Other processes see a change within `TTL`.** To make it at once, name
  a Redis cache that every process shares in `STAMPS`. A commit stamps
  what it changed there, and each request compares its principal's
  stamps: one round trip instead of two queries.

  ```python
  CACHES = {
      "default": {"URL": "memory://"},
      "shared": {"URL": env.str("CACHE_URL"), "KEY_PREFIX": "billing"},
  }
  AUTH = {"PRINCIPAL_CACHE": {"STAMPS": "shared", "TTL": 60}}
  ```

  Every service that reads the same auth tables must name the same Redis
  and the same `KEY_PREFIX`, or it never sees the others' stamps. Give
  that Redis `maxmemory-policy noeviction`, or enough memory: a lost
  stamp is safe, because it reads as a change, but every principal that
  rests on it is then read again. Each commit stamps twice: before the
  commit, and after it, in a task that a cancelled request does not stop
  (bounded by 5 seconds). A commit outside a unit of work
  (`connections.session()`) stamps too.
- **When the cache fails**, a request whose stamps cannot be read uses
  nothing kept: it reads its principal from the database, and does not
  keep it. A stamp that cannot be written is logged on the security
  logger, and the change then reaches other processes within the `TTL`.
- **Changes outside domainkit**, such as SQL run outside any session
  (`connections.engine().begin()`, `loaddata`, a snapshot restore), or
  another service that writes the tables without stamps, are seen within
  the `TTL`. To see them at once, drop what they changed yourself:

  ```python
  from domainkit.apps import registry
  from domainkit.contrib.auth import AuthSettings, principals

  principals.invalidate([principals.user_target(user_id)])  # this process
  principals.invalidate_all()  # this process, everything
  await principals.invalidate_everywhere(
      [principals.user_target(user_id)], registry.settings(AuthSettings).PRINCIPAL_CACHE
  )  # every process, with STAMPS
  ```

  Each returns how many principals it dropped here.

- **Open connections** are checked again through the cache, so they see
  a change as soon as requests do.
- **`request.user.password` was removed in 0.10.** The principal holds no
  hash. Reading it raises `AttributeError`, which names what replaces it
  (so `getattr(user, "password", None)` is `None`). To check a password,
  read the hash afresh: `await password_holds(user, raw)`
  (`domainkit.contrib.auth.api`), or `await uow.users.password_hash(user.id)`.
  `has_usable_password()` keeps working, without the hash.
- A user model that loads relationships of its own with the user (other
  than `groups` and `user_permissions`) is read by the ORM every time,
  and never kept.

`check` warns, in staging and production, about a `TTL` above 5 seconds
without `STAMPS` (`auth.W001`): a revocation would keep working that long
in the other processes. `STAMPS` that names anything but a Redis cache of
`CACHES` stops startup (`auth.E003`). With `domainkit[metrics]`,
`auth_principal_cache_total` counts hits, misses, invalidations,
evictions, requests served by another's read (`coalesced`), and reads
ahead (`refresh`, `refresh_failed`), by authentication class (`token`,
`cookie`, `jwt`, `certificate`), never by user. `ENABLED = False` reads
the principal on every request, in the same two queries.

## Options

| `AUTH` key | Default | What it does |
| --- | --- | --- |
| `AUTHENTICATION_CLASSES` | `TokenAuthentication` | Classes tried in order; the first issues login tokens. |
| `AUTHENTICATION_EXEMPT_PATHS` | `/health`, `/ready`, `/metrics` | Served without reading credentials. |
| `TOKEN_TTL`, `TOKEN_PREFIX` | 14 days, `dkt_` | Opaque login tokens. |
| `PERSONAL_TOKEN_MAX_AGE`, `MAX_LIVE_TOKENS` | 90 days, 100 | Personal tokens. |
| `JWT` | see the table | `SIGNING_KEY`, `VERIFYING_KEY`, `ALGORITHM`, `PREVIOUS_KEYS`, `ACCESS_TOKEN_TTL`, `REFRESH_TOKEN_TTL`, `ISSUER`, `AUDIENCE`, `LEEWAY`. |
| `COOKIE`, `CSRF_TRUSTED_ORIGINS` | see the table | Browser sessions. |
| `CLIENT_CERT` | see the table | `MATCH`, `HEADER`, `VERIFY_HEADER`, `PROXIES`. |
| `PRINCIPAL_CACHE` | see the table | `ENABLED`, `TTL`, `MAX_ENTRIES`, `STAMPS`. |

[The settings tables](../auth.md#settings) give every key with its
bounds. `SCOPED_CREDENTIALS` and `LIVE_REAUTH_INTERVAL` are top-level
settings ([reference/settings.md](../reference/settings.md)).

## Errors and refusals

| What the client sees | Why |
| --- | --- |
| `401 authentication_failed`, `error="invalid_token"` | A token that is unknown, expired, revoked, or of an inactive user. |
| `401` "Session ended after inactivity; log in again." | The login idled out (`IDLE_TIMEOUT`). |
| `401` "Refresh token reused; log in again." | A used refresh token came back; the login ended. |
| `400 refresh_not_supported` | `/auth/refresh` with opaque tokens. |
| `403 permission_denied` "Credentials limited to scopes cannot call this route ..." | A scoped token on a route without a rule that checks scopes. |
| `403 scope_not_held`, `409 too_many_tokens`, `422 token_lifetime_too_long` | `POST /auth/tokens` refusals. |
| `400 password_required`, `400 wrong_password`, `401 step_up_required` | `POST /auth/tokens` needs the user again. |
| `403 csrf_failed` | A cookie request from an untrusted origin, or without a valid CSRF token. |
| `400 no_session` | `GET /session` without a session cookie. |
| `429` | Too many refused credentials from one address. |

## Common mistakes

- **Listing `CookieAuthentication` first.** It issues no tokens, so
  `/auth/login` fails. List it after a class that reads headers.
- **Putting a network in `CLIENT_CERT.PROXIES`.** Every host there could
  name any certificate. List single addresses.
- **Dropping an old JSON Web Token key at once.** Keep it in
  `PREVIOUS_KEYS` until the rollout ends, or every client refreshes.
- **Reading `request.user.password`.** It was removed; use
  `password_holds`.
- **A `TTL` above 5 seconds without `STAMPS`.** A revoked token works
  that long in other processes.

## See also

- [Logging in](logging-in.md): the routes that issue tokens and
  sessions.
- [Protecting routes](routes.md): rules that read `request.user` and
  the scopes.
- [Stepping up](step-up.md): proving it is you again before one act.
- [The settings tables](../auth.md#settings): every key and its bounds.
