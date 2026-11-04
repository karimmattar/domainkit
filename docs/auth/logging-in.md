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
- send password reset and verification links, and let people sign up;
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
{"username": "alice", "password": "correct horse battery"}
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
| `POST /login` | `{"username", "password"}`, and `"new_password"` where the password must change | `200` with tokens; `202` with a challenge for a user with a second factor; `401 invalid_credentials`; `429 rate_limited`; `403 password_change_required` for a password staff set, with `AUTH.PASSWORD_CHANGE_ON_FIRST_USE` ([PCI DSS v4.0's passwords](pci-dss.md)). |
| `POST /refresh` | `{"refresh_token"}` | `200` with new tokens; `401` for a used, expired, or unknown refresh token; `400 refresh_not_supported` with opaque tokens. |
| `POST /logout` | optional `{"everywhere": true}` | `204`: this login's tokens end, or every token of the user. Scoped credentials end only themselves (`403` with `everywhere`). |
| `GET /me` | | The user: `id`, `username`, `email`, `email_verified`, `is_staff`, `is_superuser`, `last_login`, `groups`, and `permissions` (with a scoped token, those its scopes allow). |
| `POST /password` | `{"old_password", "new_password"}` | `204`: every other login of the user ends; `400 wrong_password`; `422 invalid_password` if a validator refuses the new password; `429`. |
| `POST /step-up` (`auth.stepup.router`) | `{"code"}`, and `"password"` where asked | `200` with when and how the credentials stepped up ([Stepping up](step-up.md)). |

`username` is the user model's `USERNAME_FIELD` (the email, for a model
that logs in with its email). Tokens come as OAuth 2's token response.
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
  lists them, with `kind`, `created_at`, `last_used_at`, and `current`.
  `DELETE /auth/tokens/{id}` ends one, wherever it is used.
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
| `POST /mfa/totp/confirm` | a user | `{"code"}` | `{"recovery_codes"}`, shown once: the second factor is on, and every other login and token of the user ends; `401 invalid_code`; `400 no_enrolment`. |
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
- **`resetmfa <username>`** turns it off for a user who lost the app and
  the codes, and ends their sessions. Check who is asking first.

The audit trail records `auth.mfa_enabled`, `auth.mfa_disabled`,
`auth.recovery_codes_regenerated`, `auth.recovery_code_used` (with the
codes left), and refusals as `auth.mfa` (`invalid_token`,
`invalid_code`, `throttled`).

## Password reset

People who forgot their password ask for a link by mail, and choose a
new password with it. Install messaging and mail
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
AUTH = {"PASSWORD_RESET_URL": "https://app.example.com/reset-password?token={token}"}
```

```python
from domainkit.contrib.auth.recovery import router as recovery_router

router.include_router(recovery_router, prefix="/auth")
```

| Endpoint | Who | Body | Answer |
| --- | --- | --- | --- |
| `POST /password-reset` | anyone | `{"email"}` | Always `202`; `422` for a value that is not one plain address; `429 rate_limited`. |
| `POST /password-reset/confirm` | anyone | `{"token", "new_password"}` | `204`; `400 invalid_token`; `422` if a validator refuses the password. |
| `POST /email-verification` | a user, unscoped | | `202`; `403` for credentials limited to scopes; `429 rate_limited`. |
| `POST /email-verification/confirm` | anyone | `{"token"}` | `204`; `400 invalid_token`. |

How a reset works:

1. **The request tells nothing.** It answers `202` whether or not an
   account uses the address, with the same body, in the same time: it
   only queues a task.
2. **The worker sends the links.** It finds the address, in any case,
   and mails every active user of it who has a usable password a link:
   `PASSWORD_RESET_URL` with the token in place of `{token}` (the token
   alone without the setting). The link works for
   `PASSWORD_RESET_TIMEOUT` (an hour). A user whose stored address cannot
   take mail, from before addresses were checked, is skipped with a
   warning that names the user's id, never the address.
3. **Your page posts the token and the new password** to
   `/password-reset/confirm`. The validators check the password, and
   every session of the user ends.

About the tokens:

- **They are never stored**, not even in the outbox: the worker makes
  them as it mails them. They are signed with `SECRET_KEY`.
- **They rest on the user's state**: the password hash, the last login,
  the email, and the status. So a token stops working once it is used,
  once the user logs in, or once any of those changes.
- **Every refusal looks the same**: a wrong token, a used or expired one,
  and one for no user all get `400 invalid_token`.
- **Counting.** Requests for mail count against `EMAIL_RATE` (5 an hour)
  per client address and per email address. Tokens that do not hold
  count against `LOGIN_RATE` per client address, once checked. They are
  not counted per user, nor before the check: a signature cannot be
  guessed, and a count that anyone could fill with forged tokens would
  lock people out of the links they were sent.

The audit trail records `auth.password_reset_requested` for each link
mailed, `auth.password_reset` for a reset (and `denied`, with the reason
`invalid_token` or `throttled`, for refusals), and
`auth.password_changed`.

### Email verification

`AbstractUser` has `email_verified`. It is false for new users, and for
existing ones once the column is added (run `makemigrations` and
`migrate`).

1. A user asks for a link at `/email-verification`.
2. The worker mails it to their address, unless it is verified already:
   `EMAIL_VERIFICATION_URL` with the token in place of `{token}`.
3. `/email-verification/confirm` with the token sets `email_verified`.

The link works once, for `EMAIL_VERIFICATION_TIMEOUT` (3 days), and only
for the address it went to: changing the email clears `email_verified`
and ends the links sent before. Staff cannot set it in the admin API,
where it is read-only. `user.verify_email()` sets it in code.
Verifications are audited as `auth.email_verified`.

### Signing up

`domainkit.contrib.auth.signup.router` has two routes. They answer 404
until `AUTH.SIGNUP_ENABLED` is on:

```python
from domainkit.contrib.auth.signup import router as signup_router

router.include_router(signup_router, prefix="/auth")
AUTH = {
    "SIGNUP_ENABLED": True,
    "SIGNUP_URL": "https://app.example.com/signup/finish?token={token}",
}
```

| Endpoint | Who | Body | Answer |
| --- | --- | --- | --- |
| `POST /signup` | anyone | `{"username", "email", "fields"}` | `202`; `422` for fields that do not fit; `429 rate_limited`. |
| `POST /signup/confirm` | anyone | `{"token", "password"}` | `204`: the account is made; `400 invalid_token`; `409 signup_taken`; `422` for a password the validators refuse; `429 rate_limited`; `503 password_checks_busy`. |

How signing up works:

1. **The request is checked, then kept.** `fields` holds the user model's
   other `REQUIRED_FIELDS` (`signup_fields()`; none for the default
   `User`), every one, as `{"phone": "+3161234567"}`. Each is converted to
   its field's type and checked by its markers: a missing one is
   `422 invalid_user_field`, another field `422 unknown_user_field`. No
   password is taken. The request is kept as a pending signup, and the
   route answers `202`, doing the same work whatever accounts exist.
2. **The worker mails the address.** When neither the username (in any
   case) nor the address has an account, it sends a link:
   `AUTH.SIGNUP_URL` with the token in place of `{token}`, which works for
   `EMAIL_VERIFICATION_TIMEOUT`. Otherwise it sends word that an account
   uses the address or the username, and drops the pending signup.
3. **Your page asks for a password**, and posts it with the token to
   `/signup/confirm`. The validators check it; a refusal leaves the link
   working, to try another. The account is made with that password and a
   verified address, and the user can log in.

Why the password comes last:

- **Whoever reads the mail chooses the password.** So asking for an
  account with someone else's address gains nothing (pre-account
  hijacking): if the address's owner uses the link, the account is
  theirs, with a password only they know. If a password were taken with
  the request, the person who asked would set it, and an owner who used
  the link would make an account that the asker can log in to.
- **No account exists before its address is confirmed.** So nothing that
  anyone else can see differs between an address that has an account and
  one that has none. Only whoever reads mail at an address learns
  whether it has an account. Usernames are not secret that way: someone
  who signs up with their own address learns from the mail that a
  username is taken, as any signup form must tell. `EMAIL_RATE` bounds
  how fast.
- **A pending signup reserves nothing**, neither its username nor its
  address, so one that is never confirmed blocks no one. Several may name
  the same username: each address gets its link, the first link used gets
  the name, and the others answer `409 signup_taken`. A link for an
  address that got an account meanwhile answers `409` too. A link works
  once; a wrong, used, or expired one gets `400 invalid_token`, audited
  as `auth.signup`.

Pending signups keep the username and the address, and no password, in
`domainkit_pending_signups` (run `makemigrations` and `migrate`). That
table is never in fixtures: it holds the addresses of people who may
never have asked. The outbox holds only a pending signup's id. A row goes
when its link is used, when the worker has told its address that the
name or the address is taken, or, past `EMAIL_VERIFICATION_TIMEOUT`,
with the hourly purge of expired tokens. Signups count against
`EMAIL_RATE` per client address and per email address, and links that do
not hold against `LOGIN_RATE` per client address. New accounts are
audited as `auth.user_signed_up`. Two links for one new address used at
the same moment can both make an account: email addresses are not unique
in the user table.

### The mail templates

The worker renders the mail from the templates `auth/password_reset`,
`auth/email_verification`, `auth/signup_confirm`, and
`auth/signup_exists` (plain text). Replace them from
`MAIL.TEMPLATE_DIRS`.

| Template | Its variables |
| --- | --- |
| `auth/password_reset`, `auth/email_verification` | `user`, `username`, `link`, and `valid_for` (`1 hour`). |
| `auth/signup_confirm` | `link` and `valid_for` only: the username was chosen by whoever asked, who may not be the address's owner. |
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
| `ON` | `{"login", "signup", "password_reset"}` | The routes that ask: `POST /auth/login`, `POST /auth/signup`, and `POST /auth/password-reset`. |
| `MIN_SCORE` | `0.5` | reCAPTCHA v3's lowest score let through (0 to 1). |

`ON` covers those three routes only. `POST /session` (browser sessions)
does not ask for a captcha. To ask for one on a route of your own, use
the dependency `captcha(action)` with one of the three names:

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
| `PASSWORD_RESET_TIMEOUT`, `PASSWORD_RESET_URL` | 1 hour, `None` | Reset links. |
| `EMAIL_VERIFICATION_TIMEOUT`, `EMAIL_VERIFICATION_URL` | 3 days, `None` | Verification links, and signup links. |
| `SIGNUP_ENABLED`, `SIGNUP_URL` | `False`, `None` | Signing up. |
| `CAPTCHA` | `None` | The captcha. |

Each URL must hold `{token}`. Staging and production refuse `http://`
URLs (`auth.E006`). [The settings tables](../auth.md#settings) give
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
| `invalid_token` | 400 | A challenge, reset, verification, or signup token that does not hold. |
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
- **A reset URL without `{token}`.** The settings refuse it.
- **Expecting `/session` to ask for a captcha.** Only the three routes of
  `ON` do.
- **Taking a password at `/signup`.** It is taken at `/signup/confirm`,
  from whoever reads the mail.

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
