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

This page has moved, a part a page:

- [Installing auth: content types and permissions](auth/install.md)
- [Users, groups, backends, and passwords](auth/users.md)
- [Authenticating requests](auth/authentication.md)
- [Logging in: sessions, two factors, and password reset](auth/logging-in.md)
- [Protecting routes, and the admin API](auth/routes.md)

## Settings

| `AUTH` key | Default | Meaning |
| --- | --- | --- |
| `USER_MODEL` | `"domainkit.contrib.auth.models.User"` | The user class. |
| `USER_TABLE` | `None` (the default user's table) | A custom user's `Table`; required with a custom `USER_MODEL`. |
| `PASSWORD_HASHERS` | PBKDF2, PBKDF2-SHA1, Argon2, scrypt | Hasher classes, at least one; the first hashes new passwords. |
| `HASHING_THREADS` | `1` | Threads per process that hash passwords; each keeps a core busy. One, like a Django worker, so with a process per core hashing never takes the event loops' cores (at most 1,024). |
| `HASHING_QUEUE` | `8` | Hashing calls that may wait for a thread (about 0.4 s each); more answer `503 password_checks_busy` (at most 10,000). |
| `HASHING_RESERVED` | `1` | Places of `HASHING_QUEUE` kept for `POST /auth/password` and the admin's password change; other calls are refused once the rest are taken. More than `HASHING_QUEUE` counts as `HASHING_QUEUE`. |
| `PASSWORD_VALIDATORS` | the four above | Rules for new passwords; `[]` checks nothing. |
| `BACKENDS` | `["domainkit.contrib.auth.backends.ModelBackend"]` | Authentication and permission backends, in order; at least one. |
| `AUTHENTICATION_CLASSES` | `["domainkit.contrib.auth.authentication.TokenAuthentication"]` | How requests prove who sent them, in order (at least one); the first issues login tokens. |
| `AUTHENTICATION_EXEMPT_PATHS` | `["/health", "/ready", "/metrics"]` | Paths served without looking at credentials. |
| `TOKEN_TTL` | 14 days | How long a login's opaque token works; `None` until revoked. |
| `TOKEN_PREFIX` | `"dkt_"` | Starts every opaque token; changing it invalidates existing tokens. |
| `MAX_LOGINS` | `100` | Logins a user holds at once: login tokens, browser sessions, and JSON Web Token logins (1 to 10,000). A new one past it ends the oldest; personal tokens neither count nor end ([Sessions](auth/logging-in.md#sessions)). |
| `IDLE_TIMEOUT` | `None` | End a login (a login token, a browser session, a JSON Web Token login's refresh) unused this long, from 5 minutes to 30 days: never before, and within a tenth of it after. `None`: logins last their whole lifetime. Personal tokens and certificates have none ([Sessions](auth/logging-in.md#sessions)). |
| `MAX_LIVE_TOKENS` | `100` | Live access tokens (logins and personal tokens) a user may hold and still make a personal token; past it, `409 too_many_tokens` (1 to 10,000). |
| `LOGIN_RATE` | `"10/minute"` | Wrong passwords at login per client address and per username, and at password changes per user; reset, verification, signup, and second-factor links that do not hold, per client address. Checks that succeed are not counted. `None` for no limit. |
| `DEVICE_TTL` | 30 days | How long a client that logged in keeps a count of its own: a login that works hands out a signed, HttpOnly device proof for its account, and logins that send it count against `LOGIN_RATE` per device, not per account or address. From an hour to 400 days; `None` hands out none, as before 0.10 ([Wrong passwords](auth/logging-in.md#wrong-passwords-and-device-proofs)). |
| `EMAIL_RATE` | `"5/hour"` | Requests that send mail (resets, verifications, signups) per client address and per email address or user. |
| `THROTTLE_FAIL_CLOSED` | `None` | While `RATE_LIMIT_BACKEND` cannot count (Redis failing), refuse password and code checks, and requests for mail, with `503 throttle_unavailable`, rather than let them through uncounted. `None`: `True` in staging and production, `False` elsewhere (since 0.11; `False` lets them through, as before) ([Wrong passwords](auth/logging-in.md#logging-in-and-out)). |
| `REQUIRE_SHARED_THROTTLES` | `True` | Staging and production refuse to start while password and code attempts are counted in each process (a `MemoryBackend` as `RATE_LIMIT_BACKEND`, `auth.E105`), since 0.11. `False` only warns (`auth.W004`), as 0.10 did: for a deployment of one process. |
| `PASSWORD_RESET_TIMEOUT` | 1 hour | How long a reset link works. |
| `PASSWORD_RESET_URL` | `None` | Your page that takes a reset token: `{token}` marks where. Staging and production refuse `http://`, here and in the other link URLs. |
| `EMAIL_VERIFICATION_TIMEOUT` | 3 days | How long a verification link works. |
| `EMAIL_VERIFICATION_URL` | `None` | Your page that takes a verification token. |
| `SIGNUP_ENABLED` | `False` | Let anyone ask for an account at `/signup`. |
| `SIGNUP_URL` | `None` | Your page that takes a signup link's token: `{token}` marks where. |
| `COOKIE` | see below | Browser sessions, for `CookieAuthentication`. |
| `MFA` | see below | Second factors (`auth.mfa`). |
| `CLIENT_CERT` | see below | Client certificates, for `ClientCertificateAuthentication`. |
| `PRINCIPAL_CACHE` | see below | Principals kept between requests ([the principal cache](auth/authentication.md#the-principal-cache)). |
| `CSRF_TRUSTED_ORIGINS` | `()` | Origins (`https://app.example.com`, `https://*.example.com`) whose pages may send cookie requests, besides the request's own and `ALLOWED_HOSTS`. |
| `REQUIRE_ROUTE_RULES` | `True` | Deprecated: the core's `REQUIRE_ROUTE_RULES` ([reference](reference.md)); `False` here still turns the check off until 0.11, with a warning. |
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
| `END_PREVIOUS_SESSION` | `True` | A login from a browser ends the session its cookie held; `False` keeps it, as before 0.10 ([Sessions](auth/logging-in.md#sessions)). |

| `AUTH["MFA"]` key | Default | Meaning |
| --- | --- | --- |
| `ISSUER` | `None` | The name authenticator apps show: not empty, and at most 100 characters; `PROJECT_NAME` when `None`. |
| `CHALLENGE_TTL` | 5 minutes | How long the step between the password and the code may take (an hour at most). |
| `RECOVERY_CODES` | `10` | Recovery codes each user gets (1 to 50). |
| `WINDOW` | `1` | Steps of 30 seconds either side of now whose codes count (0 to 10). |
| `RATE` | `"10/hour"` | Wrong codes per user, wherever they come from; `None` for no limit. |
| `ADDRESS_RATE` | `"60/hour"` | Wrong codes per client address at `/mfa/login`, with a challenge that holds; `None` for no limit. |

| `AUTH["CLIENT_CERT"]` key | Default | Meaning |
| --- | --- | --- |
| `MATCH` | `"fingerprint"` | `fingerprint`: the users `addcertificate` registered; `subject`: the common name is the username. |
| `HEADER` | `None` | The header a proxy of `PROXIES` sets to the URL-encoded PEM; `None` takes certificates from the ASGI TLS extension only. Needs `PROXIES`. |
| `VERIFY_HEADER` | `None` | The header in which the proxy says `SUCCESS` (nginx's `$ssl_client_verify`). Required with `HEADER` and `MATCH = "subject"`. |
| `PROXIES` | `()` | The addresses of the proxies that check certificates and set `HEADER`; requests from anywhere else that send it get 401. Single addresses in staging and production. |

| `AUTH["PRINCIPAL_CACHE"]` key | Default | Meaning |
| --- | --- | --- |
| `ENABLED` | `True` | Keep principals between requests; `False` reads them on every request. |
| `TTL` | 5 seconds | How long a principal is used before it is read again, at most 5 minutes: the longest a change made in another process takes to be seen without `STAMPS`. |
| `MAX_ENTRIES` | `10000` | Principals kept per process (1 to 100,000); the least recently used go first. |
| `STAMPS` | `None` | A `CACHES` alias (not empty; at most 100 characters) of a Redis or Valkey cache every process shares; changes then reach every process at once. |

| `AUTH["JWT"]` key | Default | Meaning |
| --- | --- | --- |
| `SIGNING_KEY` | `None` (required) | The HMAC secret, or the private key in PEM. |
| `VERIFYING_KEY` | `None` | The public key in PEM, for asymmetric algorithms. |
| `ALGORITHM` | `"HS256"` | The algorithm `SIGNING_KEY` signs with, and the only one its tokens are checked with: `HS*`, `RS*`, `PS*`, `ES*`, or `EdDSA`. A token's own header never chooses one. |
| `PREVIOUS_KEYS` | `()` | Keys that signed before `SIGNING_KEY`, at most 3, each kept to verify what it signed until it retired plus `ACCESS_TOKEN_TTL` and `LEEWAY` (below; [Replacing a key](auth/authentication.md#replacing-a-key)). |
| `ACCESS_TOKEN_TTL` | 5 minutes | How long an access token works. |
| `REFRESH_TOKEN_TTL` | 14 days | How long a login can be refreshed. |
| `ISSUER` | `None` | `iss`, set and then required. |
| `AUDIENCE` | `None` | `aud`, set and then required. |
| `LEEWAY` | 0 | Clock difference tolerated on `exp` and `iat`. |

Each `PREVIOUS_KEYS` entry:

| Key | Default | Meaning |
| --- | --- | --- |
| `KEY` | required | The old HMAC secret, or the old public key in PEM. A private key is refused: a retired key only verifies. Kept secret in reprs. |
| `ALGORITHM` | `None` | The algorithm it signed with; `None`: `ALGORITHM`. Its tokens are checked with this one only. |
| `RETIRED_AT` | required | When the last process stopped signing with it, with a time zone: the end of the rollout of the new key, at most a day after startup (`auth.E009`). Its tokens are refused from `RETIRED_AT` plus `ACCESS_TOKEN_TTL` and `LEEWAY` on. |

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
