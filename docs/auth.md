# Users, groups, and permissions

`domainkit.contrib.auth` gives a service its users and says who may call
each route. It has users, groups, permissions per content type, password
hashing and validation, backends, object permissions, and a user model
that you can replace. Requests prove who sent them with tokens (opaque
by default, or JSON Web Tokens), browser session cookies, or client
certificates. Routes say who may call them with
[rules](learning.md#words). These are Django's `contrib.auth` and
`contrib.contenttypes`, made for REST APIs.

On this page you learn how to:

- protect your first routes, and log in to call them;
- find the page for each part of auth;
- look up every `AUTH` setting, check, and command;
- make users and tokens in tests.

The design is in [ADR 0015](adr/0015-auth-and-content-types.md),
[ADR 0016](adr/0016-authenticating-requests.md), and
[ADR 0017](adr/0017-deny-by-default-and-admin-api.md).

## A first example

A new project has auth installed ([Installing auth](auth/install.md)).
Three routes: one for anyone, one for users who logged in, and one for
users with a permission:

```python
from fastapi import APIRouter, Depends

from domainkit.contrib.auth import AuthenticatedUser
from domainkit.contrib.auth.api import router as auth_router
from domainkit.http.access import HasPermissions, Public, Require

router = APIRouter()
router.include_router(auth_router, prefix="/auth")


@router.get("/catalog", dependencies=[Public])
async def catalog() -> list[str]:
    return ["lamp", "chair"]


@router.get("/orders")
async def my_orders(user: AuthenticatedUser) -> dict[str, str]:
    return {"customer": user.get_username()}


@router.post(
    "/products", dependencies=[Depends(Require(HasPermissions("shop.add_product")))]
)
async def add_product() -> dict[str, str]:
    return {"added": "lamp"}
```

A user `alice` without permissions calls them:

```text
GET  /catalog                    200 ['lamp', 'chair']
GET  /orders                     401 not_authenticated  Authentication credentials were not provided.
POST /auth/login                 200 {'access_token': 'dkt_snZPF_UTvBVauITuAhrPF8logExpedTsqM8cVan9Ifs', 'token_type': 'Bearer', 'expires_in': 1209600}
GET  /orders  (with the token)   200 {'customer': 'alice'}
POST /products (with the token)  403 permission_denied  You do not have permission to perform this action.
```

Every route said who may call it. A route that says nothing stops a
deployed service from starting ([Deny by default](auth/routes.md#deny-by-default)).

## The pages

| Page | What it covers |
| --- | --- |
| [Installing auth: content types and permissions](auth/install.md) | The domains, the middleware, the tables, and the permissions of your classes. |
| [Users, groups, backends, and passwords](auth/users.md) | Making users and groups, checking permissions, a custom user model, backends, object permissions, hashers, and validators. |
| [Authenticating requests](auth/authentication.md) | Opaque tokens, personal tokens and scopes, JSON Web Tokens, browser sessions, client certificates, and the principal cache. |
| [Logging in: sessions, two factors, and password reset](auth/logging-in.md) | Login routes, wrong passwords and device proofs, sessions, the second factor, resets, signup, and the captcha. |
| [Protecting routes, and the admin API](auth/routes.md) | Rules, deny by default, another authentication, the admin API, and impersonation. |
| [Stepping up, and transactions authorized for one amount and payee](auth/step-up.md) | `StepUp`, PSD2's dynamic linking, and `auth.integration`. |
| [PCI DSS v4.0's passwords](auth/pci-dss.md) | The preset, requirement by requirement. |
| [Identity providers](auth/identity.md) | OpenID Connect, OAuth 2, SAML, and LDAP (`domainkit.contrib.identity`). |
| [Passkeys and bound devices](auth/devices.md) | WebAuthn passkeys, signed requests, and push tokens (`domainkit.contrib.devices`). |
| [Phone numbers](auth/phones.md) | Verifying a number with a text message (`domainkit.contrib.phones`). |
| [Sign-in approvals](auth/ciba.md) | Sign-ins approved from the user's phone, by push or QR code; OIDC CIBA; the mobile app as identity (`domainkit.contrib.ciba`). |

## Settings

Every key of `AUTH` is optional. A settings file other than the base one
extends it: `AUTH = {**AUTH, ...}`. The settings are validated by the
model `AuthSettings`, with `JWTSettings` for `AUTH["JWT"]`. Code reads
them through the registry:

```python
from domainkit.apps import registry
from domainkit.contrib.auth import AuthSettings

config = registry.settings(AuthSettings)
print(config.TOKEN_TTL, config.JWT.ACCESS_TOKEN_TTL)
```

```text
14 days, 0:00:00 0:05:00
```

| `AUTH` key | Default | Meaning |
| --- | --- | --- |
| `USER_MODEL` | `"domainkit.contrib.auth.models.User"` | The user class. |
| `USER_TABLE` | `None` (the default user's table) | A custom user's `Table`; required with a custom `USER_MODEL`. |
| `PASSWORD_HASHERS` | PBKDF2, PBKDF2-SHA1, Argon2, scrypt | Hasher classes, at least one. The first hashes new passwords; all check old ones. |
| `HASHING_THREADS` | `1` | Threads per process that hash passwords (at most 1,024). Each keeps a core busy. One, like a Django worker, so with a process per core, hashing never takes the event loops' cores. |
| `HASHING_QUEUE` | `8` | Hashing calls that may wait for a thread (about 0.4 seconds each); more answer `503 password_checks_busy` (at most 10,000). |
| `HASHING_RESERVED` | `1` | Places of `HASHING_QUEUE` kept for `POST /auth/password` and the admin's password change; other calls are refused once the rest are taken. More than `HASHING_QUEUE` counts as `HASHING_QUEUE`. |
| `PASSWORD_VALIDATORS` | the four rules | Rules for new passwords ([Validators](auth/users.md#validators)); `[]` checks nothing. |
| `PRESET` | `None` | `"pci-dss-4"` sets `PASSWORD_VALIDATORS`, `PASSWORD_HISTORY`, `PASSWORD_CHANGE_ON_FIRST_USE`, `LOGIN_RATE`, `PASSWORD_CHECKS_COUNT_AS_LOGINS`, and `IDLE_TIMEOUT` as PCI DSS v4.0 asks, where they are left unset. It refuses to start where one you set contradicts it (`auth.E010`) ([PCI DSS v4.0's passwords](auth/pci-dss.md)). |
| `PASSWORD_HISTORY` | `0` | Passwords of a user's own, the current one among them, that a new one may not repeat (0 to 24); `0` keeps none. |
| `PASSWORD_CHANGE_ON_FIRST_USE` | `False` | A password that staff set (the admin API, `changepassword <user>`) logs in only to be changed: `/login` and `/session` answer `403 password_change_required` until the login sends `new_password`. With a second factor, the change is sent with the code (`/mfa/login`, `/session/mfa`). |
| `PASSWORD_CHANGE_DEVICE_LOGINS` | `"end"` | What the user's password change (`/password`) or reset (`/password-reset/code`) does to the logins of their bound devices: `"end"` ends them with the other logins, `"keep"` keeps them, `"ask"` does as the request's `keep_device_logins` says (true by default). Every other login ends in each case ([Password changes and the phone](auth/logging-in.md#password-changes-and-the-phone)). |
| `BACKENDS` | `["domainkit.contrib.auth.backends.ModelBackend"]` | Authentication and permission backends, asked in order; at least one. |
| `AUTHENTICATION_CLASSES` | `["domainkit.contrib.auth.authentication.TokenAuthentication"]` | How requests prove who sent them, tried in order (at least one); the first issues login tokens. |
| `AUTHENTICATION_EXEMPT_PATHS` | `["/health", "/ready", "/metrics"]` | Paths served without looking at credentials. |
| `TOKEN_TTL` | 14 days | How long a login's opaque token works; `None`: until revoked. |
| `TOKEN_PREFIX` | `"dkt_"` | Starts every opaque token. Changing it makes existing tokens invalid. |
| `MAX_LOGINS` | `100` | Logins a user holds at once: login tokens, browser sessions, and JSON Web Token logins (1 to 10,000). A new one past it ends the oldest; personal tokens neither count nor end ([Sessions](auth/logging-in.md#sessions)). |
| `IDLE_TIMEOUT` | `None` | End a login (a login token, a browser session, a JSON Web Token login's refresh, and access tokens issued longer ago) unused for this long, from 5 minutes to 30 days: never before, and within a tenth of it after. An open WebSocket or event stream does not count as a use. `None`: logins last their whole lifetime. Personal tokens and certificates have none ([Sessions](auth/logging-in.md#sessions)). |
| `MAX_LIVE_TOKENS` | `100` | Live access tokens (logins and personal tokens) a user may hold and still make a personal token; past it, `409 too_many_tokens` (1 to 10,000). |
| `PASSWORD_CHECKS_COUNT_AS_LOGINS` | `False` | Count wrong passwords of signed-in users (`/password`, a step-up, the second factor's changes) against the account's count of wrong logins too, so `LOGIN_RATE` bounds every guess at the password. `False` counts them apart, so others' wrong logins never keep a signed-in user from changing the password. |
| `PERSONAL_TOKEN_MAX_AGE` | 90 days | The longest a personal token made at `POST /auth/tokens` may work, from a day to 3,650 days. A token asked without `expires_in_days` gets the shorter of 90 days and this; longer asks are `422 token_lifetime_too_long`. `None` allows tokens that work until revoked (`expires_in_days: null`), as before 1.0 ([Personal tokens](auth/authentication.md#personal-tokens-and-scopes)). |
| `LOGIN_RATE` | `"10/minute"` | Wrong passwords at login per client address and per username, and at password changes per user. Also wrong reset, verification, and signup codes, and second-factor tokens that do not hold, per client address. Checks that succeed are not counted. `None` for no limit (`auth.W002` where deployed). |
| `ACCOUNT_LOGIN_RATE` | `"100/day"` | Wrong passwords per username, from anywhere, over the day: `LOGIN_RATE` bounds bursts, this the total, as NIST SP 800-63B 5.2.2 asks (100 at most). It is counted in fixed windows, each UTC day for `"100/day"` (SEC-16a-2). Clients with a device proof for the account count at this rate per device, under a ceiling of twice it for all of them (SEC-16a-7). A signed-in user's password checks (`/auth/password`, a step-up, `POST /auth/tokens`) count at this rate per user too (SEC-16a-4). `None`: `LOGIN_RATE` alone, as before 1.0. `check` warns of that, or of more than 100 a day, where deployed (`auth.W006`) ([Wrong passwords](auth/logging-in.md#wrong-passwords-and-device-proofs)). |
| `DEVICE_TTL` | 30 days | How long a client that logged in keeps a count of its own. A login that works hands out a signed, HttpOnly device proof for its account, and logins that send it count against `LOGIN_RATE` per device, not per account or address. From an hour to 400 days; `None` hands out none, as before 0.10 ([Wrong passwords](auth/logging-in.md#wrong-passwords-and-device-proofs)). |
| `EMAIL_RATE` | `"5/hour"` | Requests that send mail (resets, verifications, signups) per client address and per email address or user. |
| `THROTTLE_FAIL_CLOSED` | `None` | While `RATE_LIMIT_BACKEND` cannot count (Redis failing), refuse password and code checks, and requests for mail, with `503 throttle_unavailable`, instead of letting them through uncounted. `None`: `True` in staging and production, `False` elsewhere (since 0.11) ([Logging in and out](auth/logging-in.md#logging-in-and-out)). |
| `REQUIRE_SHARED_THROTTLES` | `True` | Staging and production refuse to start while password and code attempts are counted in each process (a `MemoryBackend` as `RATE_LIMIT_BACKEND`, `auth.E105`), since 0.11. `False` only warns (`auth.W004`), as 0.10 did: for a deployment of one process. |
| `IDENTIFIERS` | `None` | The user fields that identify a user (`None`: the user model's `USERNAME_FIELD` alone), in the order a login tries them, such as `("email", "mobile_number")`; at least one. Each must be a field of the user model, and named once (`auth.E011`) (ADR 0126). |
| `CODE_TTL` | 10 minutes | How long a code works, from 1 minute to 1 hour. Signup, email and mobile verification, and password resets prove that someone reads an address or a number with a code of 6 digits; the links of 1.x, and their settings (`VERIFICATION`, `PASSWORD_RESET_URL`, `PASSWORD_RESET_TIMEOUT`, `EMAIL_VERIFICATION_URL`, `EMAIL_VERIFICATION_TIMEOUT`, `SIGNUP_URL`), were removed in 2.0: setting one is refused. |
| `CODE_ATTEMPTS` | `5` | Wrong tries that spend a code, from 1 to 10. |
| `SIGNUP_ENABLED` | `False` | Let anyone ask for an account at `/signup`. |
| `COOKIE` | see below | Browser sessions, for `CookieAuthentication`. |
| `MFA` | see below | Second factors (`auth.mfa`). |
| `STEP_UP` | see below | Authenticating again for one act (`StepUp`, [Stepping up](auth/step-up.md)). |
| `SCA` | see below | Transactions authorized for one amount and payee ([Stepping up](auth/step-up.md#transactions-authorized-for-one-amount-and-payee)). |
| `CLIENT_CERT` | see below | Client certificates, for `ClientCertificateAuthentication`. |
| `PRINCIPAL_CACHE` | see below | Principals kept between requests ([the principal cache](auth/authentication.md#the-principal-cache)). |
| `CAPTCHA` | `None` | A captcha on login, signup, and password reset: `{"PROVIDER": "turnstile" \| "recaptcha" \| "hcaptcha", "SECRET": ..., "ON": ["login", "signup", "password_reset"], "MIN_SCORE": 0.5}`. Each route that `ON` names asks for the widget's token in `X-Captcha-Token`, checked with the provider first: missing or refused, `400 captcha_failed`; the provider unreachable, `503 captcha_unavailable`. `MIN_SCORE` (0 to 1) is reCAPTCHA v3's lowest score let through. Only the token and the secret are sent, never the client's address ([Captcha](auth/logging-in.md#captcha)). |
| `IMPERSONATION_TTL` | `None` | How long a token that staff get to act as a user works, up to 8 hours; `None` turns impersonation off ([Impersonation](auth/routes.md#impersonation)). |
| `CSRF_TRUSTED_ORIGINS` | `()` | Origins (`https://app.example.com`, `https://*.example.com`) whose pages may send cookie requests, besides the request's own and `ALLOWED_HOSTS`. |
| `JWT` | see below | JSON Web Tokens, for `JWTAuthentication`. |

| `AUTH["COOKIE"]` key | Default | Meaning |
| --- | --- | --- |
| `NAME` | `None`: `"__Host-dk_session"` | The session cookie (HttpOnly). By default `__Host-dk_session` where the prefix holds (Secure, no `DOMAIN`, the `PATH` `/`), else `dk_session`. `__Host-` names need `SECURE`, no `DOMAIN`, and the `PATH` `/`. |
| `CSRF_NAME` | `None`: `"__Host-dk_csrf"` | The cookie that scripts read the CSRF token from; `__Host-dk_csrf` or `dk_csrf`, as `NAME`. |
| `CSRF_HEADER` | `"X-CSRF-Token"` | The header that scripts send it back in. |
| `SAMESITE` | `"lax"` | `lax`, `strict`, or `none` (which needs `SECURE`). |
| `SECURE` | `None` | HTTPS only; `None` means unless `DEBUG`. `False` is refused in staging and production (`auth.E005`). |
| `DOMAIN` | `None` | `None` keeps the cookies to this host. |
| `PATH` | `"/"` | The cookies' path. |
| `TTL` | 14 days | How long a browser session works. |
| `END_PREVIOUS_SESSION` | `True` | A login from a browser ends the session its cookie held; `False` keeps it, as before 0.10 ([Sessions](auth/logging-in.md#sessions)). |

| `AUTH["MFA"]` key | Default | Meaning |
| --- | --- | --- |
| `ISSUER` | `None` | The name authenticator apps show: not empty, and at most 100 characters; `PROJECT_NAME` when `None`. |
| `CHALLENGE_TTL` | 5 minutes | How long the step between the password and the code may take (an hour at most). |
| `RECOVERY_CODES` | `10` | Recovery codes each user gets (1 to 50). |
| `WINDOW` | `1` | Steps of 30 seconds either side of now whose codes count (0 to 10). |
| `RATE` | `"10/hour"` | Wrong codes per user, wherever they come from; `None` for no limit (`auth.W003` where deployed). |
| `ADDRESS_RATE` | `"60/hour"` | Wrong codes per client address at `/mfa/login`, with a challenge that holds; `None` for no limit. |

| `AUTH["STEP_UP"]` key | Default | Meaning |
| --- | --- | --- |
| `METHODS` | `["otp"]` | What a step-up proves: a code of the authenticator app (`otp`), or the password with it (`["pwd", "otp"]`). It must hold `otp`, each method once. |

| `AUTH["SCA"]` key | Default | Meaning |
| --- | --- | --- |
| `METHODS` | `["otp"]` | What confirming a transaction proves, as `STEP_UP.METHODS`. |
| `TTL` | 5 minutes | How long a transaction's authorization request works, from when it is made, its confirmation and use included (30 minutes at most). |

| `AUTH["CLIENT_CERT"]` key | Default | Meaning |
| --- | --- | --- |
| `MATCH` | `"fingerprint"` | `fingerprint`: the users that `addcertificate` registered; `subject`: the common name is the username. |
| `HEADER` | `None` | The header a proxy of `PROXIES` sets to the URL-encoded PEM; `None` takes certificates from the ASGI TLS extension only. Needs `PROXIES` (`auth.E001`). |
| `VERIFY_HEADER` | `None` | The header in which the proxy says `SUCCESS` (nginx's `$ssl_client_verify`). Required with `HEADER` and `MATCH = "subject"` (`auth.E002`). |
| `PROXIES` | `()` | The addresses of the proxies that check certificates and set `HEADER`; requests from anywhere else that send it get 401. Single addresses in staging and production (`auth.E007`). |

| `AUTH["PRINCIPAL_CACHE"]` key | Default | Meaning |
| --- | --- | --- |
| `ENABLED` | `True` | Keep principals between requests; `False` reads them on every request. |
| `TTL` | 5 seconds | How long a principal is used before it is read again, at most 5 minutes: the longest a change made in another process takes to be seen without `STAMPS` (`auth.W001` above 5 seconds where deployed). |
| `MAX_ENTRIES` | `10000` | Principals kept per process (1 to 100,000); the least recently used go first. |
| `STAMPS` | `None` | A `CACHES` alias (not empty; at most 100 characters) of a Redis or Valkey cache that every process shares; changes then reach every process at once (`auth.E003` for anything else). |

| `AUTH["JWT"]` key | Default | Meaning |
| --- | --- | --- |
| `SIGNING_KEY` | `None` (required) | The HMAC secret (32 bytes or more for `HS256`), or the private key in PEM. |
| `VERIFYING_KEY` | `None` | The public key in PEM, for asymmetric algorithms. |
| `ALGORITHM` | `"HS256"` | The algorithm that `SIGNING_KEY` signs with, and the only one its tokens are checked with: `HS*`, `RS*`, `PS*`, `ES*`, or `EdDSA`. A token's own header never chooses one. |
| `PREVIOUS_KEYS` | `()` | Keys that signed before `SIGNING_KEY`, at most 3, each kept to verify what it signed until it retired plus `ACCESS_TOKEN_TTL` and `LEEWAY` (below; [Replacing a key](auth/authentication.md#replacing-a-key)). |
| `ACCESS_TOKEN_TTL` | 5 minutes | How long an access token works. |
| `REFRESH_TOKEN_TTL` | 14 days | How long a login can be refreshed. |
| `ISSUER` | `None` | `iss`, set and then required. |
| `AUDIENCE` | `None` | `aud`, set and then required. |
| `LEEWAY` | 0 | Clock difference allowed on `exp` and `iat`. |

Each `PREVIOUS_KEYS` entry:

| Key | Default | Meaning |
| --- | --- | --- |
| `KEY` | required | The old HMAC secret, or the old public key in PEM. A private key is refused: a retired key only verifies. Kept secret in reprs. |
| `ALGORITHM` | `None` | The algorithm it signed with; `None`: `ALGORITHM`. Its tokens are checked with this one only. |
| `RETIRED_AT` | required | When the last process stopped signing with it, with a time zone: the end of the rollout of the new key, at most a day after startup (`auth.E009`). Its tokens are refused from `RETIRED_AT` plus `ACCESS_TOKEN_TTL` and `LEEWAY` on. |

`AUTH.REQUIRE_ROUTE_RULES` was removed in 0.11; the top-level
`REQUIRE_ROUTE_RULES` replaces it ([reference/settings.md](reference/settings.md)).
The settings of the other auth domains are on their pages:
[`IDENTITY`](auth/identity.md#settings), [`DEVICES`](auth/devices.md#settings),
[`PHONES`](auth/phones.md#settings), and [`CIBA`](auth/ciba.md#settings).

## Checks

`check` and startup run these checks. An error (`E`) stops the
application; a warning (`W`) is reported.

| Id | Where | What it finds |
| --- | --- | --- |
| `auth.E001` | anywhere | `CLIENT_CERT.HEADER` without `CLIENT_CERT.PROXIES`. |
| `auth.E002` | anywhere | `CLIENT_CERT.MATCH = "subject"` through `HEADER` without `VERIFY_HEADER`. |
| `auth.E003` | anywhere | `PRINCIPAL_CACHE.STAMPS` that is not a Redis cache of `CACHES`. |
| `auth.E004` | staging, production | A hasher made for tests in `PASSWORD_HASHERS`. |
| `auth.E005` | staging, production | `COOKIE.SECURE = False`. |
| `auth.E007` | staging, production | A network, not one address, in `CLIENT_CERT.PROXIES`. |
| `auth.E008` | anywhere | A JSON Web Token previous key that is the signing key, or one listed twice. |
| `auth.E009` | anywhere | A previous key's `RETIRED_AT` more than a day after startup. |
| `auth.E010` | anywhere | A setting that contradicts `PRESET`. |
| `auth.E101` | anywhere | Routes read users, but `MIDDLEWARE` lacks `AuthenticationMiddleware`. |
| `auth.E102` | anywhere | The session router is mounted without `CookieAuthentication`. |
| `auth.E103` | anywhere | Routes that send mail without `domainkit.contrib.messaging` and `domainkit.contrib.mail`. |
| `auth.E104` | anywhere | Routes that send mail with a user model without `EMAIL_FIELD` and `email_verified`. |
| `auth.E105` | staging, production | Attempts counted in each process, with `REQUIRE_SHARED_THROTTLES`. |
| `auth.E106` | anywhere | A `StepUp` asks for methods that no step-up proves. |
| `auth.W001` | staging, production | `PRINCIPAL_CACHE.TTL` above 5 seconds without `STAMPS`. |
| `auth.W002` | staging, production | `LOGIN_RATE = None`. |
| `auth.W003` | staging, production | `MFA.RATE = None`. |
| `auth.W004` | staging, production | Attempts counted in each process, without `REQUIRE_SHARED_THROTTLES`. |
| `auth.W005` | staging, production | A cookie name without `__Host-` where the prefix could hold. |
| `auth.W006` | staging, production | `ACCOUNT_LOGIN_RATE` of `None`, or more than 100 a day. |
| `auth.W007` | anywhere | A backend that does not list its grants on objects (`object_grants`). |
| `security.E101` | staging, production | A route that declares no access rule. |
| `security.E020` | staging, production | `IDENTITY.LDAP` over `ldap://` without `START_TLS`. |

## Commands

| Command | Options | Purpose |
| --- | --- | --- |
| `createsuperuser` | `--<USERNAME_FIELD>`, `--<field>` per `REQUIRED_FIELDS`, `--noinput`, `--temporary`/`--no-temporary` | Create a superuser (and staff), asking for what the options leave out. With `--noinput`, values come from `<PREFIX>SUPERUSER_<FIELD>` (or a `_FILE`), and the password from `<PREFIX>SUPERUSER_PASSWORD`, which must pass the validators; without one the password is unusable. With `PASSWORD_CHANGE_ON_FIRST_USE`, a password read with `--noinput` changes at the first login, unless `--no-temporary`. A refused password is kept on request only without `PRESET` and outside staging and production. |
| `changepassword` | `[username]`, `--temporary`/`--no-temporary` | Ask twice for a new password that passes the validators and the history; three tries. The default user is your login name. Every token of the user ends. With `PASSWORD_CHANGE_ON_FIRST_USE`, the password of a user you name changes at the first login, unless `--no-temporary`. |
| `createtoken` | `username`, `--name` (`cli`), `--days` (90), `--no-expiry`, `--scope` | Print a new opaque token for the user's scripts; it is shown once. Each `--scope KEY` limits it to that permission, which the user must have. `--name login` is refused. |
| `cleartokens` | `--user` | Delete expired tokens, old step-ups, and expired transaction requests (hourly with `domainkit.contrib.messaging`), or every token of one user. |
| `addcertificate` | `username`, `certificate`, `--name` | Let a client certificate (a PEM file) log in as the user, by its SHA-256; prints the fingerprint. |
| `resetmfa` | `username` | Turn a user's second factor off and end their sessions, for a user who lost the app and the recovery codes. Check who is asking first. |
| `resealmfa` | `--check`, `--batch-size` (500) | Encrypt every TOTP secret again under `SECRET_KEY`, so a key of `SECRET_KEY_FALLBACKS` can be dropped; `--check` changes nothing and fails while a secret is under a fallback or under no key ([Two-factor logins](auth/logging-in.md#two-factor-logins)). |

```bash
python manage.py createtoken alice --name "stock report" --scope shop.view_product --days 30
```

Code that sets passwords itself ends the user's sessions with
`await uow.tokens.revoke_user(user.id)`.
[reference/commands.md](reference/commands.md) lists every command.

## Tests

In tests, hash with the fast hasher, and make users with
`domainkit.contrib.auth.testing` ([Users in tests](auth/users.md#users-in-tests)).
To call a route as a user, log in through the API, or issue a token
directly:

```python
from fastapi.testclient import TestClient

from domainkit.contrib.auth import AuthUnitOfWork
from domainkit.contrib.auth.testing import create_user
from domainkit.http import get_asgi_application


async def token_for_alice() -> str:
    alice = await create_user("shop.add_product", username="alice")
    async with AuthUnitOfWork() as uow:
        token = await uow.tokens.issue(alice.id, ttl=None)
        await uow.commit()
    return token


with TestClient(get_asgi_application()) as client:
    token = client.portal.call(token_for_alice)
    answer = client.post("/products", headers={"Authorization": f"Bearer {token}"})
    print(answer.status_code, answer.json())
```

```text
200 {'added': 'lamp'}
```

The test clients of `domainkit.testing` also log in for you:
`await async_client.authenticated_as(user)` ([testing.md](testing.md)).

## Common mistakes

- **A route without a rule.** `check` lists it, and production does not
  start. Add `Depends(Require(...))`, `AuthenticatedUser`, or `Public`.
- **`AUTH = {...}` in a settings file other than the base one.** It
  replaces the base settings, and the user model with them. Write
  `AUTH = {**AUTH, ...}`.
- **A `MemoryBackend` for rate limits in production.** Each process then
  allows the whole rate; startup refuses it (`auth.E105`).
- **Swapping the user model after the first `migrate`.** Choose it first.

## See also

- [The tutorial's permissions part](tutorial.md): a first protected
  domain, step by step.
- [Resources](resources.md): `ResourceRouter` takes a `rule=`.
- [Security](security.md): how the parts fit together.
- [Problems](reference/problems.md): every error code a client can get.
