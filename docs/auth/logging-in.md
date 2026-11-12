# Logging in: sessions, two factors, and password reset

Logging in turns a username and a password into a token or a browser
session. This page covers the routes that log users in and out, how
wrong passwords are counted, the second factor (an authenticator app),
password resets, email verification, signing up, and the captcha. Each
part is a router that you mount next to the others.

On this page you learn how to:

- log in, log out, refresh, and change a password;
- understand how wrong passwords are counted, and why a user is not
  locked out by strangers;
- bound how many logins a user holds, and end idle ones;
- turn on a second factor and log in with it;
- send password reset and verification codes, and let people sign up;
- ask for a captcha on the routes that bots try.

## A first example

`domainkit.contrib.auth.api.router` has the login routes:

```python
from fastapi import APIRouter

from domainkit.contrib.auth.api import router as auth_router

router = APIRouter()
router.include_router(auth_router, prefix="/auth")
```

```http
POST /auth/login
{"identifier": "alice", "password": "correct horse battery"}
```

```json
{"access_token": "dkt_snZPF_UTvBVauITuAhrPF8logExpedTsqM8cVan9Ifs", "token_type": "Bearer", "expires_in": 1209600}
```

A wrong password gets the same answer for every reason (an unknown user,
a wrong password, an inactive user, a user without a usable password),
in the same time:

```json
{"type": "about:blank", "title": "Unauthorized", "status": 401, "detail": "Unable to log in with the given credentials.", "instance": "/auth/login", "code": "invalid_credentials", "request_id": "e1b3d99e9bf56110ee34a1738c46b6e9"}
```

## Logging in and out

| Endpoint | Body | Answer |
| --- | --- | --- |
| `POST /login` | `{"identifier", "password"}`, and `"new_password"` where the password must change | `200` with tokens; `202` with a challenge for a user with a second factor; `401 invalid_credentials`; `429 rate_limited`; `403 password_change_required` for a password staff set, with `AUTH.PASSWORD_CHANGE_ON_FIRST_USE` ([PCI DSS v4.0's passwords](pci-dss.md)). |
| `POST /refresh` | `{"refresh_token"}` | `200` with new tokens; `401` for a used, expired, or unknown refresh token; `400 refresh_not_supported` with opaque tokens. |
| `POST /logout` | optional `{"everywhere": true}` or `{"others": true}` | `204`: this login's tokens end; with `everywhere`, every token of the user; with `others`, every other login of the user, while this one and the personal tokens stay. Scoped credentials end only themselves (`403` with either). Both at once: `422`. |
| `GET /me` | | The user: `id`, `username`, `email`, `email_verified`, `is_staff`, `is_superuser`, `last_login`, `groups`, and `permissions` (with a scoped token, those its scopes allow). |
| `POST /password` | `{"old_password", "new_password"}`, and `"keep_device_logins"` with `AUTH.CREDENTIAL_CHANGE_DEVICE_LOGINS = "ask"` | `204`: every other login of the user ends, the bound devices' as [the setting says](#credential-changes-and-the-phone); `400 wrong_password`; `422 invalid_password` if a validator refuses the new password; `422 device_logins_not_asked`; `429`. |
| `POST /step-up` (`auth.stepup.router`) | `{"code"}`, and `"password"` where asked | `200` with when and how the credentials stepped up ([Stepping up](step-up.md)). |

`identifier` is a value of one of the fields that `AUTH.IDENTIFIERS`
names: an email address, a mobile number, or a username. By default it
is the user model's `USERNAME_FIELD` alone (the email, for a model that
logs in with its email). The old name, `username`, still works. Tokens
come as OAuth 2's token response.
With [JSON Web Tokens](authentication.md#json-web-tokens), the answer
also has a `refresh_token`:

```json
{"access_token": "eyJhbGciOi...", "token_type": "Bearer", "expires_in": 300, "refresh_token": "dkt_q9Xc..."}
```

With `domainkit.contrib.audit` installed, logins and logouts are audited
as `auth.user_logged_in` and `auth.user_logged_out`, under the user. A
refused login is audited as `auth.login` with the outcome `denied`,
without the username, which may be a password typed in the wrong field.
Throttled logins are audited once per count and period, so a flood adds
no rows.

Refusals reach the trail through a hook, `on_refusal`
(`domainkit.contrib.auth.integration`). An app of your own that feeds
another sink registers one in its `ready()`: it gets the action and the
reason's code, never what was typed.

### Logging in with an email address or a mobile number

`AUTH.IDENTIFIERS` names the user fields that identify a user, in the
order a login tries them:

```python
AUTH = {
    "IDENTIFIERS": ("email", "mobile_number"),
}
```

Then each of these logs in the same user:

```http
POST /auth/login
{"identifier": "Ann@Example.com", "password": "correct horse battery"}
```

```http
POST /auth/login
{"identifier": "+33 6 12 34 56 78", "password": "correct horse battery"}
```

domainkit reads the form of the value:

- A value with `@` is an email address. It matches in any case of its
  ASCII letters.
- A value that is a number in international form (`+`, a country code,
  and up to 15 digits) is a mobile number. Spaces, dots, dashes, and
  brackets do not count.
- Any other value is tried against the other fields you named, in order.
- The `USERNAME_FIELD` always matches exactly, as before 1.8.

Where `email` is in `AUTH.IDENTIFIERS`, two users cannot have one
address: a second one gets `409 email_taken`. An address that two users
already shared before (rows written before 1.8) logs in neither of them,
until you change one. Wrong passwords count against one account however
its number or address is written. Startup refuses a field the user model
does not have (`auth.E011`).

Without `AUTH.IDENTIFIERS`, only the `USERNAME_FIELD` logs in, as in
1.7. [Users](users.md) shows the `mobile_number` field and the optional
`username`.

### Wrong passwords

Wrong passwords are limited to `AUTH.LOGIN_RATE` (10 a minute) per client
address and per account. The `RATE_LIMIT_BACKEND` counts them;
`RedisBackend` counts once for every process.

- **Only wrong passwords count.** Each attempt is counted before the
  password is checked, and given back if it holds. So logins that work
  never use the count up: a service account that logs in often, or an
  office behind one address, is not throttled by its own logins. A
  backend of your own without `refund` counts every attempt.
- **A full count refuses the next attempt** before its password is
  checked, a right password too, with `429 rate_limited`.
- **Names that logging in treats as one account share one count**:
  `alice`, `ALICE`, and their full-width or mathematical look-alikes,
  under NFKC.
- **Password changes have a count of their own** per user, so throttled
  logins never stop the user from changing the password.
- **The address is a network for IPv6**: the prefix of
  `RATE_LIMIT_IPV6_PREFIX`.
- **One count for every process.** In staging and production the
  application refuses to start, and `check` fails, when the backend
  counts in each process (`auth.E105`, since 0.11), because each process
  would allow the whole rate. A deployment of one process sets
  `AUTH.REQUIRE_SHARED_THROTTLES = False`, which only warns
  (`auth.W004`).

When `RATE_LIMIT_BACKEND` cannot count (`RedisBackend` while Redis is
down), staging and production refuse password and code checks, and
requests that send mail, with `503 throttle_unavailable` and
`Retry-After`, until the backend counts again
([runbook.md](../runbook.md#logins-are-refused-while-redis-is-down)). A
brute force timed with a cache outage gets no attempts, and neither do
your users. Other requests are served. Elsewhere these checks go through
uncounted, as other requests do. `AUTH.THROTTLE_FAIL_CLOSED` decides
either way: `False` lets them through where deployed too, as before
0.11, and `True` refuses them anywhere. The local fallback of
`RATE_LIMIT_FALLBACK_REPLICAS` counts in one process only, so what it
lets through counts as not counted and is refused too.

The count per account stops someone guessing one user's password from
many addresses. It also lets anyone hold that user's logins back. What
holds, and what does not:

- **Per period**: at most `LOGIN_RATE` wrong passwords are checked for
  one account, from however many addresses, and at most `LOGIN_RATE` from
  one address (an IPv6 network), for however many accounts.
- **Per day**: at most `AUTH.ACCOUNT_LOGIN_RATE` (100) wrong passwords
  are checked for one account in each UTC day, however slowly or quickly
  they come. NIST SP 800-63B 5.2.2 limits an account to 100 failed
  attempts, and `"10/minute"` alone allowed 14,400 a day (AUTH-19). The
  count is a fixed window, not a bucket that refills: a bucket of 100 a
  day let a burst of 100 and then one every 14.4 minutes, 199 within
  24 hours (SEC-16a-2). Across midnight two windows meet, so at most 200
  can come within minutes. `check` warns where deployed when the setting
  is `None` or allows more than 100 a day (`auth.W006`).
- **Signed-in users' password checks** (`/auth/password`, a step-up, the
  second factor's changes, `POST /auth/tokens`) count at `LOGIN_RATE` and
  at `ACCOUNT_LOGIN_RATE` per user. So a session, or a script that rides
  one, holds no more guesses at the password than a stranger
  (SEC-16a-4). With `PASSWORD_CHECKS_COUNT_AS_LOGINS`, they also count
  against the account's login counts.
- **Strangers can hold a user back.** `LOGIN_RATE` wrong passwords for a
  username in a period, from any addresses, get the user's right password
  refused with 429 everywhere. The count frees a place every
  `period / limit` seconds (6 for `"10/minute"`), so a caller who keeps
  sending one wrong password that often keeps the user out.
  `LOGIN_RATE` wrong passwords from one address get every login from that
  address refused the same way. Sessions and tokens already issued are
  not affected. The audited `throttled` refusals show such a flood.
- **Known devices are not held back.** Since 0.10, neither count holds
  back a client that logged in to the account before: see below.

### Wrong passwords and device proofs

A login that works, its second factor included, hands its client a
device proof (`auth.devices`). It is a cookie, `__Host-dk_device`
(`dk_device` over plain HTTP; `DEVICE_COOKIE` in
`domainkit.contrib.auth.devices` holds the name without the prefix), HttpOnly and `SameSite=Lax`, that names a
random device, signed with `SECRET_KEY` for that account. Browsers keep
the cookie, and so does any HTTP client with a cookie jar.

- **The device is stored with the user's tokens** for `AUTH.DEVICE_TTL`
  (30 days). `GET /auth/tokens` does not list it. Whatever ends every
  token of the user ends it too: a password change or reset, `/logout`
  with `everywhere`, an admin's change, deletion.
- **A user keeps at most 20 devices.** A new one past it ends the oldest.
- **A proven login counts apart.** A login to the same account that sends
  a live proof counts against `LOGIN_RATE` per device, and against the
  account's ceiling of proven attempts, twice `LOGIN_RATE`. It does not
  count against the account's own count, nor the address's.

What this gives:

- **Strangers' wrong passwords never fill those counts.** The user gets
  in from a browser they used before, and so does everyone behind an
  office's address whom one person there is flooding. One copied proof
  fills only its own device's count, half the ceiling.
- **Guessing stays bounded per account.** At most `LOGIN_RATE` wrong
  passwords a period without proofs, and twice as many with proofs,
  however many proofs one holds. Over a day: `ACCOUNT_LOGIN_RATE` without
  proofs, the same per proven device, and twice it for all proven devices
  together: 300 a day in all, by default, of which a stranger without a
  proof gets 100, and one with a copied proof 100 more. Proofs come only
  from logins that held, second factor included, and end with the
  password. A proof made for another account, forged, or of a device that
  ended counts as none. Each login that works renews its device.
- **A client without a proof** (a new browser, a script without a cookie
  jar) is counted per address and per account, as before, and may still
  be held back while someone floods the account. A password reset, which
  needs the user's mail, ends that for the user.

`DEVICE_TTL = None` hands out no proofs, as before 0.10.
[ADR 0087](../adr/0087-count-proven-devices-apart-and-bound-scopes-by-route.md)
has the measurements behind this choice: under an hour's attack from
200 addresses, a stored lockout checked 40 guesses and let none of the
user's logins in; device proofs checked 609, and let every login from a
known device in. With six valid proofs and no ceiling they checked
4,263; the ceiling holds them to 1,828.

### Sessions

A login is a login token, a browser session, or a JSON Web Token login.

- **A new login ends the browser's last session.** `POST /session` and
  `/session/mfa` from a browser whose cookie still holds a session end
  that session. The browser replaces the cookie, so a copy of the old one
  would otherwise work on, unseen, for `COOKIE.TTL`.
  `COOKIE.END_PREVIOUS_SESSION = False` keeps it.
- **Logins are bounded.** A user holds at most `AUTH.MAX_LOGINS` (100)
  logins at once. Refreshing adds none. A new one past the limit ends the
  oldest, so no user is refused for their own history. Personal tokens
  neither count nor end.
- **The session list.** `GET /auth/tokens` (`personal_tokens.router`)
  lists them, with `kind`, `created_at`, `last_used_at`, `current`, and
  where each came from (below). `DELETE /auth/tokens/{id}` ends one,
  wherever it is used. `POST /auth/logout` with `{"others": true}` ends
  all but the one asking.
- **Idle logins end.** With `AUTH.IDLE_TIMEOUT` (off by default; 5 minutes
  to 30 days), a login token or a browser session that authenticated
  nothing for that long is refused: `401` "Session ended after
  inactivity; log in again.", or an anonymous request for a cookie. A
  JSON Web Token login not refreshed for that long cannot refresh, and an
  access token issued longer ago than the timeout no longer works,
  whatever its lifetime.

More about idle logins:

- **Live connections are not a use.** An open WebSocket or event stream
  is checked again every `LIVE_REAUTH_INTERVAL`, but these checks write
  no last use. So the connection closes once its login idles out
  (since 1.0).
- **Last uses are written every tenth of the timeout**, so a login ends
  no sooner than the timeout, and within a tenth of it after. That costs
  one more `UPDATE` per active login per tenth: 40 an hour for
  15 minutes, against 12 without a timeout.
- **A kept principal that looks idle is read again** before it is
  refused: another process may have served it since.
- **Personal tokens and certificates have no idle timeout.**

#### Where each login came from

Each login records how it signed in, and from what (since 2.2,
[ADR 0130](../adr/0130-show-where-each-login-came-from-and-manage-logins-from-the-phone.md)).
`GET /auth/tokens` answers them with each token, so a user can tell
"Chrome on Windows, by QR, two minutes ago" from the login they want to
end:

| Field | What it holds |
| --- | --- |
| `method` | `password`, `passkey`, `device` (a bound device's signature or enrollment), `push` or `qr` ([approved from the phone](ciba.md)), `provider` (an identity provider), or `impersonation`. |
| `agent` | The request's `User-Agent`, at most 200 characters. |
| `ip` | The client's address, as `TRUSTED_PROXIES` resolves it. |
| `device_id` | The bound device that logged in, for a device's own login. |

They are `null` for personal tokens, and for logins made before 2.2. A
refreshed JSON Web Token login keeps its first request's origin. They
are personal data: they end with the login, `privacy export` lists them
with the user's tokens, and `privacy erase` deletes the tokens.

A login route of your own names its method:
`issue_login(user, method=LoginMethod.PASSWORD)`, or
`start_browser_session(request, user, response, method=...)`
(`LoginMethod` is in `auth.integration`). Without one, the login records
its agent and address, and no method.

#### Who manages logins

The hook point `on_manage_logins` (`domainkit.contrib.auth.gates`) runs
its checks before:

- listing the user's tokens (`GET /auth/tokens`);
- ending a token that is not the request's own login;
- logging out `everywhere` or `others`;
- adding or removing a way into the account: a bound device, a push
  token, a personal token, a second factor, a passkey, a linked identity
  ([ADR 0134](../adr/0134-bind-another-device-from-a-bound-one.md)).

A route declares the check as a dependency (since 2.3,
[ADR 0136](../adr/0136-declare-the-login-check-on-the-route.md)), and so
does a route of your own that adds a way into an account:

```python
from fastapi import APIRouter

from domainkit.contrib.auth.integration import MANAGES_LOGINS

router = APIRouter()


@router.post("/api-keys", dependencies=[MANAGES_LOGINS])
async def add_api_key() -> dict[str, str]:
    return {"key": "..."}
```

The check then runs before the body is read. A route where only some
requests manage logins calls `check_manage_logins(request, user)` in its
body instead.

A check takes the request and the user, and refuses by raising. With
none registered, any login of the user manages the others. Logging out
of the request's own login is never checked: a user can always sign out
of the browser in front of them. With `CIBA.APPROVE_LOGINS`, the ciba app
registers one: [only the phone manages
logins](ciba.md#managing-logins-from-the-phone).

#### Credential changes and the phone

A password change ends every other login of the user, a reset all of
them, and turning the second factor on every other: whoever held one
before is out. The logins of the user's bound devices
([Bound devices](devices.md#bound-devices)) follow
`AUTH.CREDENTIAL_CHANGE_DEVICE_LOGINS` (since 2.2,
[ADR 0131](../adr/0131-choose-what-a-password-change-does-to-the-devices-logins.md),
[ADR 0132](../adr/0132-manage-the-second-factors-from-the-phone.md)):

| Value | `POST /password`, `POST /password-reset/code`, `POST /mfa/totp/confirm` |
| --- | --- |
| `"end"` (default) | End them too, as before 2.2. |
| `"keep"` | Keep them, as Google keeps the devices a user verifies sign-ins with. |
| `"ask"` | Do as the request's `keep_device_logins` says: `true`, the default, keeps them; `false` ends them. |

```http
POST /auth/password
{"old_password": "...", "new_password": "...", "keep_device_logins": false}
```

- **Under `"ask"`, `false` is managing logins.** The
  [`on_manage_logins` checks](#who-manages-logins) run first: in the
  phone modes, only a request the phone signs may end the phone's login
  (`403 phone_required`). On a reset, they run once the code names the
  user; a refusal spends no code.
- **Under `"end"` or `"keep"`, the setting decides.** A request that sends
  `keep_device_logins` is refused (`422 device_logins_not_asked`), so a
  client that asks to sign the phone out is never kept in silently.
- **A device's login** is one the device made (`/devices/login`, its
  enrollment), one whose request it signed, or the login that bound it.
  `GET /auth/tokens` shows its `device_id`.
- **Staff and operators end everything.** A password set through the
  admin API or `changepassword`, and one changed at login, end every
  login: those follow an incident as often as not.
- **In the phone modes, the phone manages the second factors.** Turning
  the authenticator app on or off, new recovery codes, and adding or
  removing a passkey run the same checks: a web session gets
  `403 phone_required`, so a thief who knows the password cannot add a
  factor of their own and step up without the phone
  ([ADR 0132](../adr/0132-manage-the-second-factors-from-the-phone.md)).

With [the phone as identity](ciba.md#the-mobile-app-as-identity), choose
`"keep"` or `"ask"`: under `"end"`, anyone who knows the password, from a
stolen web session or with the user's mail, signs the phone out too.

## Two-factor logins

`domainkit.contrib.auth.mfa` adds a second factor: an authenticator app
(TOTP, RFC 6238) and single-use recovery codes. It needs the `mfa` extra
(`pip install "domainkit[mfa]"`) and `SECRET_KEY`. Run `makemigrations`
and `migrate` for the tables `domainkit_mfa_devices` and
`domainkit_recovery_codes`.

```python
from domainkit.contrib.auth.mfa import router as mfa_router

router.include_router(mfa_router, prefix="/auth")
```

A user turns it on in two steps. First, with the password:

```http
POST /auth/mfa/totp
{"password": "correct horse battery"}
```

```json
{"secret": "HH3QQ2LGSPSCO3SIFRBV5QWWQPSHEZAE", "otpauth_uri": "otpauth://totp/API%3Aalice?secret=HH3QQ2LGSPSCO3SIFRBV5QWWQPSHEZAE&issuer=API&algorithm=SHA1&digits=6&period=30"}
```

Your page shows the URI as a QR code. The user scans it and confirms
with a code from the app (`POST /auth/mfa/totp/confirm`,
`{"code": "123456"}`), and gets the recovery codes, shown once:

```json
{"recovery_codes": ["we2o-76du-e5yh-kqjv", "7hij-xmbm-knjo-to3i", "xrfl-26rn-m5a4-n7hi", "..."]}
```

From then on, `/login` checks the password as before, then answers
`202` instead of tokens:

```json
{"mfa_required": true, "mfa_token": "a55d341cdc5545c9bcf5db54a3456cfc:1xOfiI:hvWlBlVvGYLtjgMC-j3W8YuBkHxaspkOVQci0HA12pU", "expires_in": 300}
```

The client sends the `mfa_token` with a code from the app, or a recovery
code, to `/mfa/login`, and gets the tokens that `/login` would have
returned. Browser sessions do the same: `POST /session` answers `202`,
and `POST /session/mfa` sets the cookies.

| Endpoint | Who | Body | Answer |
| --- | --- | --- | --- |
| `GET /mfa` | a user | | `{"enabled", "recovery_codes_left"}`. |
| `POST /mfa/totp` | a user | `{"password"}` | `{"secret", "otpauth_uri"}`: the secret in base 32, and the URI for a QR code; `400 wrong_password`; `409 mfa_enabled`. |
| `POST /mfa/totp/confirm` | a user | `{"code"}`, and `"keep_device_logins"` with `AUTH.CREDENTIAL_CHANGE_DEVICE_LOGINS = "ask"` | `{"recovery_codes"}`, shown once: the second factor is on, and every other login and token of the user ends, the bound devices' as [the setting says](#credential-changes-and-the-phone); `401 invalid_code`; `400 no_enrolment`; `422 device_logins_not_asked`. |
| `POST /mfa/totp/disable` | a user | `{"password", "code"}` | `204`: it is off. |
| `POST /mfa/recovery-codes` | a user | `{"password", "code"}` | `{"recovery_codes"}`: new codes replace the old ones. |
| `POST /mfa/login` | anyone | `{"mfa_token", "code"}`, and `"new_password"` where the password must change | The tokens `/login` would have returned; `400 invalid_token`; `401 invalid_code`; `429 rate_limited`. |

The challenge and the codes:

- **The challenge** is signed with `SECRET_KEY`. It works for
  `MFA.CHALLENGE_TTL` (5 minutes), and ends once the login is done or the
  password changes.
- **A code is accepted once.** A code from the current 30-second step,
  or `MFA.WINDOW` steps either side, is accepted, and the step is
  recorded. A code of that step or an earlier one is then refused, so a
  code seen over a shoulder cannot be used again.
- **Recovery codes** (10 of 80 random bits, `MFA.RECOVERY_CODES`) are
  accepted in any case, with or without their dashes. Each works once.
- **The authenticator's name** in the app is `MFA.ISSUER`, or
  `PROJECT_NAME` when that is `None`.

The counts:

- **Wrong codes count against `MFA.RATE`** (10 an hour) per user,
  wherever they come from, and at `/mfa/login` against
  `MFA.ADDRESS_RATE` (60 an hour) per client address. A guesser who has
  the password gets a few hundred tries a day at a one-in-a-million code.
- **Codes that work are not counted**, so an office or a carrier's
  network behind one address logs in as often as it needs.
- **At `/mfa/login` a code counts only once its challenge holds**, so a
  forged challenge cannot use up either count. Challenges that do not
  hold count against `LOGIN_RATE` per address once checked, and never
  hold a valid one back.
- **Anyone with the password can use up the user's count**, and hold the
  user's logins back for the hour. The audited `throttled` refusals show
  it.

What changes with the factor:

- **Turning it on ends every other login and token of the user**
  (personal tokens, devices, and certificates included), as a new
  password does: what logged in with the password alone may not be the
  user's. The request's own login stays.
- **Turning it off, and new recovery codes, need the password and a
  code**, so a stolen session cannot remove the factor.
- **Secrets are encrypted** with AES-256-GCM, under a key derived from
  `SECRET_KEY` and bound to the user's id
  ([ADR 0030](../adr/0030-encrypt-totp-secrets-under-a-derived-key.md)).
  Recovery codes are stored as digests.
- **Replacing `SECRET_KEY`.** Keep the old key in `SECRET_KEY_FALLBACKS`:
  each login moves its user's secret to the new key. Before you drop the
  old key, run `manage.py resealmfa`, which moves every secret still
  under a fallback, then `resealmfa --check`, which fails while one is
  (AUTH-17). A secret that no key opens is refused with
  `409 second_factor_unreadable`, audited and logged, wherever a code is
  checked. Recovery codes still work, and `resetmfa` turns the factor
  off.
- **`resetmfa <user>`** (a username, an email address, or a mobile number) turns it off for a user who lost the app and
  the codes, and ends their sessions. Check who is asking first.

The audit trail records `auth.mfa_enabled`, `auth.mfa_disabled`,
`auth.recovery_codes_regenerated`, `auth.recovery_code_used` (with the
codes left), and refusals as `auth.mfa` (`invalid_token`,
`invalid_code`, `throttled`).

## Password reset

People who forgot their password ask for a code, and choose a new
password with it. The code has 6 digits, and goes by mail, or by text
message to a mobile number (ADR 0126). Install messaging and mail
([mail.md](../mail.md)), and mount the router next to the one that logs
in:

```python
INSTALLED_DOMAINS = [
    "domainkit.contrib.messaging",
    "domainkit.contrib.mail",
    "domainkit.contrib.contenttypes",
    "domainkit.contrib.auth",
    "shop",
]
```

```python
from domainkit.contrib.auth.recovery import router as recovery_router

router.include_router(recovery_router, prefix="/auth")
```

| Endpoint | Who | Body | Answer |
| --- | --- | --- | --- |
| `POST /password-reset` | anyone | `{"identifier"}`, or `{"email"}` | Always `202`; `422 missing_value` without either; `429 rate_limited`. |
| `POST /password-reset/code` | anyone | `{"identifier", "code", "new_password"}`, and `"keep_device_logins"` with `AUTH.CREDENTIAL_CHANGE_DEVICE_LOGINS = "ask"` | `204`: every login ends, the bound devices' as [the setting says](#credential-changes-and-the-phone); `400 invalid_verification_code`; `422` if a validator refuses the password. |
| `POST /email-verification` | a user, unscoped | | `202`; `403` for credentials limited to scopes; `429 rate_limited`. |
| `POST /email-verification/code` | the same user | `{"code"}` | `204`; `400 invalid_verification_code`. |
| `POST /mobile-verification` | a user, unscoped | | `202`; `429 rate_limited`. |
| `POST /mobile-verification/code` | the same user | `{"code"}` | `204`; `400 invalid_verification_code`. |

The person gives any identifier: an email address, a mobile number, or
a username, by the fields of `AUTH.IDENTIFIERS`:

```http
POST /auth/password-reset
{"identifier": "+33 6 12 34 56 78"}
```

```json
{"detail": "If an account uses it, a code is on its way."}
```

How a reset works:

1. **The request tells nothing.** It answers `202` whether or not an
   account uses the value, with the same body, in the same time: it only
   queues a task.
2. **The worker sends the code.** It finds the one account the
   identifier names, active and with a usable password. A mobile number
   gets a text message through `domainkit.contrib.notifications`; any
   other identifier gets a mail to the account's address. An email
   address names the one account that has it, even where addresses do
   not log in. A user whose stored address cannot take mail, from before
   addresses were checked, is skipped with a warning that names the
   user's id, never the address.
3. **Your app posts the same identifier, the code, and the new
   password:**

   ```http
   POST /auth/password-reset/code
   {"identifier": "+33612345678", "code": "482913", "new_password": "an0ther-Strong-pass!"}
   ```

   The answer is `204`, and every session of the user ends. A password
   that a validator refuses gets `422`, and the code still works.

The text message reads:

```text
482913 is your code to reset your password. It works for 10 minutes. Never give it to anyone.
```

About the codes:

- **They work for `AUTH.CODE_TTL`** (10 minutes), only for the
  identifier they were sent for, and once. A new code replaces the one
  before, and `AUTH.CODE_ATTEMPTS` (5) wrong tries spend it.
- **They are kept only as HMACs.** The table `domainkit_auth_codes` keeps
  an HMAC of the code and of the address or number, keyed from
  `SECRET_KEY`. The worker makes the code, so it is never in the outbox.
  The hourly task and `cleartokens` delete codes that no longer work.
- **Every refusal looks the same**: a wrong code, a spent or expired one,
  and one for an identifier that no account uses all get
  `400 invalid_verification_code`.
- **Counting.** Requests for a code count against `EMAIL_RATE` (5 an
  hour) per client address and per recipient. Wrong codes count against
  `LOGIN_RATE` per client address. A right code is never counted: the
  code spends itself after its wrong tries instead.

The audit trail records `auth.password_reset_requested` for each code
sent, `auth.password_reset` for a reset (and `denied`, with the reason
`invalid_verification_code` or `throttled`, for refusals), and
`auth.password_changed`.

Where `mobile_number` is in `AUTH.IDENTIFIERS`, a project that serves the
reset or signup routes needs `domainkit.contrib.notifications`: `check`
refuses it without (`auth.E107`).

1.x also mailed links, with `AUTH.VERIFICATION = "link"`; 2.0 removed
them, and the settings they took
([upgrading.md](../upgrading.md#18-to-20)).

### Email verification

`AbstractUser` has `email_verified`. It is false for new users. A user
asks for a code, and types it in the app that asked:

```http
POST /auth/email-verification
Authorization: Bearer dkt_...
```

```json
{"detail": "A code to verify your address is on its way."}
```

```http
POST /auth/email-verification/code
Authorization: Bearer dkt_...
{"code": "482913"}
```

The answer is `204`, and `email_verified` is now true. Nothing is sent
to an address that is verified already. The code holds only for the
address it went to: changing the email clears `email_verified`, and the
code no longer works. Staff cannot set the mark in the admin API, where
it is read-only. `user.verify_email()` sets it in code. Verifications
are audited as `auth.email_verified`; wrong codes as
`auth.email_verification`.

### Mobile number verification

`AbstractUser` has `mobile_number` and `mobile_number_verified`. A user
asks for a code at `/mobile-verification`. The worker texts it to their
number through `domainkit.contrib.notifications`, unless the number is
verified already. `/mobile-verification/code` with the code sets
`mobile_number_verified`. The code follows the same rules as an email
code. Changing the number clears the mark. Verifications are audited as
`auth.mobile_number_verified`; refusals as `auth.mobile_verification`.

### Signing up

`domainkit.contrib.auth.signup.router` has two routes. They answer 404
until `AUTH.SIGNUP_ENABLED` is on:

```python
from domainkit.contrib.auth.signup import router as signup_router

router.include_router(signup_router, prefix="/auth")
AUTH = {"SIGNUP_ENABLED": True}
```

| Endpoint | Who | Body | Answer |
| --- | --- | --- | --- |
| `POST /signup` | anyone | `{"email", "fields"}`, an optional `"username"`, or `"mobile_number"` where numbers identify users | `202`; `422` for fields that do not fit, or `missing_value` without an address (or number); `429 rate_limited`. |
| `POST /signup/code` | anyone | `{"identifier", "code", "password"}` | `204`: the account is made; `400 invalid_verification_code`; `409 signup_taken`; `422` for a password the validators refuse; `429 rate_limited`; `503 password_checks_busy`. |

```http
POST /auth/signup
{"email": "nina@example.com"}
```

```json
{"detail": "Check your email to finish signing up."}
```

Where `mobile_number` is in `AUTH.IDENTIFIERS`, a person may sign up
with a number alone, and the code goes by text message:

```http
POST /auth/signup
{"mobile_number": "+33 6 12 34 56 78"}
```

```json
{"detail": "Check your text messages to finish signing up."}
```

How signing up works:

1. **The request is checked, then kept.** `fields` holds the user model's
   other `REQUIRED_FIELDS` (`signup_fields()`; none for the default
   `User`), every one, as `{"phone": "+3161234567"}`. Each is converted to
   its field's type and checked by its markers: a missing one is
   `422 invalid_user_field`, another field `422 unknown_user_field`. No
   password is taken. The request is kept as a pending signup, and the
   route answers `202`, doing the same work whatever accounts exist.
2. **The worker sends the code.** When neither the username (in any
   case) nor the address or number has an account, it sends a code, which
   works for `AUTH.CODE_TTL`. Otherwise it sends word that an account uses
   the address or the username, and drops the pending signup.
3. **Your app asks for a password**, and posts it with the address or
   number and the code:

   ```http
   POST /auth/signup/code
   {"identifier": "+33612345678", "code": "482913", "password": "correct horse battery"}
   ```

   The validators check it; a refusal leaves the code working, to try
   another. The account is made with that password, and the address, or
   the number, verified.

Why the password comes last:

- **Whoever reads the code chooses the password.** So asking for an
  account with someone else's address gains nothing (pre-account
  hijacking): if the address's owner uses the code, the account is
  theirs, with a password only they know. If a password were taken with
  the request, the person who asked would set it, and an owner who used
  the code would make an account that the asker can log in to.
- **No account exists before its address is confirmed.** So nothing that
  anyone else can see differs between an address that has an account and
  one that has none. Only whoever reads mail at an address learns
  whether it has an account. Usernames are not secret that way: someone
  who signs up with their own address learns from the mail that a
  username is taken, as any signup form must tell. `EMAIL_RATE` bounds
  how fast.
- **A pending signup reserves nothing**, neither its username nor its
  address, so one that is never confirmed blocks no one. Several may name
  the same username: each address gets its code, the first code used
  gets the name, and the others answer `409 signup_taken`. A code for an
  address that an account proved meanwhile answers `409` too, and is spent.
- **An address held unproved belongs to whoever proves it** (since 2.2.1,
  [ADR 0133](../adr/0133-a-signup-takes-an-address-its-holder-never-proved.md)).
  An account may hold an address it never proved: staff or the project's
  code made it, or its user changed to the address and never confirmed.
  That does not stop a signup: the code goes out, and the signup that
  uses it takes the address, verified. The holder loses it
  (`auth.email_released` in the audit trail), and its codes sent there
  stop working. A number held unproved moves the same way
  (`auth.mobile_number_released`).
  - The code's mail warns the reader (`auth/signup_code_held`): an
    account that is theirs keeps the address if they log in or reset
    their password instead. A reset whose code was sent to the address
    or the number proves it.
  - A holder left with nothing it logs in by (`AUTH.IDENTIFIERS`) is
    ended as an erased account is: deactivated, its password unusable,
    its tokens and second factor deleted. Its data stays, for staff.
  - An address or a number an account proved, and a username, still
    stop a signup, as before.

Pending signups keep the username and the address or number, and no
password, in `domainkit_pending_signups`. That table is never in
fixtures: it holds the addresses of people who may never have asked. The
outbox holds only a pending signup's id. A row goes when its code is
used, when the worker has told its address that the name or the address
is taken, or, past `AUTH.CODE_TTL`, with the hourly purge of expired
tokens. Signups count against `EMAIL_RATE` per client address and per
address or number, and wrong codes against `LOGIN_RATE` per client
address. New accounts are audited as `auth.user_signed_up`; refusals as
`auth.signup`. A number given where numbers do not identify users gets
`422 unknown_user_field`.

### The mail templates

The worker renders the mail from the templates
`auth/password_reset_code`, `auth/email_verification_code`,
`auth/signup_code`, `auth/signup_code_held`, and `auth/signup_exists`
(plain text). Replace them from `MAIL.TEMPLATE_DIRS`.

| Template | Its variables |
| --- | --- |
| `auth/password_reset_code`, `auth/email_verification_code` | `user`, `username`, `code`, and `valid_for` (`10 minutes`). |
| `auth/signup_code` | `code` and `valid_for` only: the username was chosen by whoever asked, who may not be the address's owner. |
| `auth/signup_code_held` | The same, sent instead when an account holds the address unproved: it says what using the code does to that account. |
| `auth/signup_exists` | `email` only. |

`check` reports these routes mounted without messaging and mail
(`auth.E103`), or with a user model without `EMAIL_FIELD` and
`email_verified` (`auth.E104`).

## Captcha

With `AUTH.CAPTCHA`, chosen routes ask for a solved captcha before
anything else. A bot's guesses then cost it a captcha each, not a check
of the password:

```python
AUTH = {
    **AUTH,
    "CAPTCHA": {"PROVIDER": "turnstile", "SECRET": env.str("TURNSTILE_SECRET")},
}
```

Your page shows the provider's widget (Cloudflare Turnstile, Google
reCAPTCHA, or hCaptcha), and sends the token it gives in the
`X-Captcha-Token` header. The provider checks it once:

- a missing or refused token is `400 captcha_failed`;
- a provider that cannot be reached refuses the request with
  `503 captcha_unavailable` (`CaptchaUnavailable`), so bots never pass
  while it is down;
- reCAPTCHA v3 scores each token: below `MIN_SCORE` (0.5) it is refused.

Only the token and the secret are sent, never the client's address.

| `CAPTCHA` key | Default | What it does |
| --- | --- | --- |
| `PROVIDER` | required | `"turnstile"`, `"recaptcha"`, or `"hcaptcha"`. |
| `SECRET` | required | The provider's secret key for the site. |
| `ON` | `{"login", "signup", "password_reset"}` | The routes that ask. `login` covers both ways to log in with a password: `POST /auth/login` and `POST /auth/session` (browser sessions). `signup` covers `POST /auth/signup`, and `password_reset` covers `POST /auth/password-reset`. |
| `MIN_SCORE` | `0.5` | reCAPTCHA v3's lowest score let through (0 to 1). |

To ask for a captcha on a route of your own, use the dependency
`captcha(action)` with one of the three names:

```python
from domainkit.contrib.auth.captcha import captcha
from domainkit.http.access import Public


@router.post("/newsletter", dependencies=[Public, captcha("signup")])
async def subscribe() -> None: ...
```

## Options

| `AUTH` key | Default | What it does |
| --- | --- | --- |
| `LOGIN_RATE`, `ACCOUNT_LOGIN_RATE` | `"10/minute"`, `"100/day"` | Wrong passwords per address and account; per day. |
| `DEVICE_TTL` | 30 days | Device proofs; `None` hands out none. |
| `PASSWORD_CHECKS_COUNT_AS_LOGINS` | `False` | Signed-in users' checks also count as logins. |
| `THROTTLE_FAIL_CLOSED`, `REQUIRE_SHARED_THROTTLES` | `None`, `True` | Refuse when counting fails; refuse per-process counts. |
| `MAX_LOGINS`, `IDLE_TIMEOUT` | 100, `None` | Logins a user holds; idle logins end. |
| `MFA` | see the table | `ISSUER`, `CHALLENGE_TTL`, `RECOVERY_CODES`, `WINDOW`, `RATE`, `ADDRESS_RATE`. |
| `EMAIL_RATE` | `"5/hour"` | Requests that send mail. |
| `CODE_TTL`, `CODE_ATTEMPTS` | 10 minutes, `5` | Codes for resets, verification, and signups. |
| `IDENTIFIERS` | `None` | The fields that identify users. |
| `SIGNUP_ENABLED` | `False` | Signing up. |
| `CAPTCHA` | `None` | The captcha. |

[The settings tables](../auth.md#settings) give
every key with its bounds.

## Errors and refusals

| Code | Status | When |
| --- | --- | --- |
| `invalid_credentials` | 401 | A wrong username or password, an inactive user, no usable password. |
| `rate_limited` | 429 | A count is full; see `Retry-After`. |
| `throttle_unavailable` | 503 | The rate-limit backend cannot count, where deployed. |
| `password_checks_busy` | 503 | The hashing queue is full. |
| `password_change_required` | 403 | A password staff set must change first. |
| `wrong_password` | 400 | `/password`, `/mfa/totp`, and the second factor's changes. |
| `invalid_password` | 422 | A validator or the history refuses the new password. |
| `invalid_token` | 400 | A second factor's challenge token that does not hold. |
| `invalid_verification_code` | 400 | A wrong, spent, or expired reset, verification, or signup code. |
| `invalid_code` | 401 | A wrong or used code. |
| `mfa_enabled`, `no_enrolment` | 409, 400 | Starting a factor twice; confirming without starting. |
| `second_factor_unreadable` | 409 | No key opens the secret: run `resealmfa`, or `resetmfa`. |
| `signup_taken` | 409 | The username or address got an account first. |
| `captcha_failed`, `captcha_unavailable` | 400, 503 | The captcha. |

## Common mistakes

- **Counting in each process.** A `MemoryBackend` lets each process
  allow the whole rate. Use `RedisBackend` where you run more than one.
- **Dropping a `SECRET_KEY` fallback before `resealmfa`.** Users whose
  secret is still under it can no longer use their app.
- **Keeping a 1.x link setting.** `AUTH.VERIFICATION`, `PASSWORD_RESET_URL`,
  and the other link keys are refused since 2.0: delete them.
- **Forgetting the captcha token on a browser login.** With `login` in
  `ON`, `POST /auth/session` needs `X-Captcha-Token` too.
- **Taking a password at `/signup`.** It is taken at `/signup/code`, from
  whoever reads the code.

## See also

- [Authenticating requests](authentication.md): the tokens and cookies
  that a login gives.
- [Stepping up](step-up.md): asking for the second factor again before
  one act.
- [Passkeys and bound devices](devices.md): logging in without a
  password.
- [Identity providers](identity.md): logging in with Google, SAML, or
  LDAP.
- [PCI DSS v4.0's passwords](pci-dss.md): the preset.
