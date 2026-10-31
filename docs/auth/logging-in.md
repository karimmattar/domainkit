# Logging in: sessions, two factors, and password reset

## Logging in and out

`domainkit.contrib.auth.api.router` has the endpoints; errors use the usual
error body.

| Endpoint | Body | Answer |
| --- | --- | --- |
| `POST /login` | `{"username", "password"}`, and `"new_password"` where the password must change | `200` with tokens; `401 invalid_credentials`; `429 login_throttled`; `403 password_change_required` for a password staff set, with `AUTH.PASSWORD_CHANGE_ON_FIRST_USE` ([PCI DSS v4.0's passwords](pci-dss.md)). |
| `POST /step-up` (`auth.stepup.router`) | `{"code"}`, and `"password"` where asked | `200` with when and how the credentials stepped up, for routes that ask (`StepUp`, [Stepping up](step-up.md)); `401 invalid_code`; `409 no_second_factor`. |
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

Wrong passwords are limited to `AUTH.LOGIN_RATE` (10 a minute) per client
address (IPv6 per network, `RATE_LIMIT_IPV6_PREFIX`) and per account, with
the `RATE_LIMIT_BACKEND`: `RedisBackend` counts once for every process.
In staging and production the application refuses to start, and `check`
fails, when the backend counts in each process (`auth.E105`, since 0.11),
since each process would allow the whole rate; a deployment of one process
sets `AUTH.REQUIRE_SHARED_THROTTLES = False`, which only warns
(`auth.W004`). Each attempt is counted before the password is checked, and
given back if it holds, so logins that work never use the count up: a
service account that logs in often, or an office behind one address, is
not throttled by its own logins. Wrong passwords do use it up, whoever
sends them, and once a count is full the next attempt is refused before
its password is checked, a right one too (see below). A backend of your
own without `refund` counts every attempt. Names
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
limit the address gets 429, and its requests with an `Authorization`
header are refused before any lookup until `Retry-After` has passed.
Credentials in another header, which only a class of your own reads (an
`X-API-Key`), are read, and refused with the same 429 even when they hold,
so a guess that works never stands out; set `guessable = False` on a class
whose credentials no client can send at will (a header only your gateway
sets). Session cookies and client certificates are not guessed (a cookie
that does not hold leaves the request anonymous; a certificate takes its
private key), so those that hold are served while their address is held:
guesses from an address shared with browsers (an office, a carrier, a
proxy) never log its users out. An opaque token that does not have
the form of an issued one is refused without a lookup. Only credentials
that do not hold (401) count: a cookie request refused by the CSRF checks
(403) is one any other site can make a browser send, and counting it
would let that site lock out everyone behind the browser's address.

When `RATE_LIMIT_BACKEND` cannot count (`RedisBackend` while Redis fails),
staging and production refuse password and code checks, and requests for
mail, with `503 throttle_unavailable` and `Retry-After`, until the backend
counts again: a brute force timed with a cache outage gets no attempts,
and neither do your users ([runbook.md](../runbook.md#logins-are-refused-while-redis-is-down)).
Other requests are served. Elsewhere they are let through uncounted, as
other requests are. `AUTH.THROTTLE_FAIL_CLOSED` decides either way:
`False` lets them through where deployed too, as before 0.11, and `True`
refuses them anywhere. The local fallback of
`RATE_LIMIT_FALLBACK_REPLICAS` counts in one process only, so what it
lets through counts as not counted and is refused too; once a caller's
share there is spent, it answers 429 itself.

The count per account is what stops guessing one user's password from many
addresses, and it also lets anyone hold that user's logins back. What
holds, and what does not:

- At most `LOGIN_RATE` wrong passwords a period are checked for one
  account, from however many addresses, and at most `LOGIN_RATE` from one
  address (an IPv6 network), for however many accounts.
- Logins that work never use a count up, so many users behind one address,
  or one that logs in often, are never refused for their own logins.
- Wrong passwords use the counts up whoever sends them. `LOGIN_RATE` wrong
  passwords for a username in a period, from any addresses, get its
  user's right password refused with 429 everywhere; the count frees a
  place every `period / limit` seconds (6 for `"10/minute"`), so a caller
  who keeps sending one wrong password that often keeps the user out.
  `LOGIN_RATE` wrong passwords from one address, for any usernames, get
  every login from that address refused the same way: one person behind
  an office's or a carrier's address can keep everyone there out.
- Sessions and tokens already issued are not affected, and password
  changes have their own count per user. The audited `throttled`
  refusals (once per count and period) show such a flood.
- Since 0.10, neither holds a client back that logged in to the account
  before: see below.

### Wrong passwords and device proofs

A login that works, its second factor included, hands its client a
device proof (`auth.devices`): a cookie, `__Host-dk_device` (`dk_device`
over plain HTTP), HttpOnly, `SameSite=Lax`, naming a random device signed
with `SECRET_KEY` for that account. The device is stored with the user's
tokens for `AUTH.DEVICE_TTL` (30 days; not listed by `GET /auth/tokens`),
so whatever ends every token of the user ends it too: a password change
or reset, `/logout` with `everywhere`, an admin's change, deletion. A user
keeps at most 20 (`devices.MAX_DEVICES`); a new one past it ends the
oldest. Browsers keep the cookie, and so does any HTTP client with a
cookie jar. A login to the same account that sends a live proof is
counted against `LOGIN_RATE` per device, and against the account's
ceiling of proven attempts, twice `LOGIN_RATE`
(`devices.PROVEN_CEILING`), not the account's own count nor the
address's:

- Strangers' wrong passwords, from anywhere, never fill those counts: the
  user gets in from a browser they logged in from before, and so does
  everyone behind an office's address whom one person there is flooding.
  One copied proof fills only its own device's count, half the ceiling.
- Guessing stays bounded per account: at most `LOGIN_RATE` wrong
  passwords a period without proofs, and twice as many with proofs,
  however many proofs one holds. Proofs come only from logins that held,
  second factor included, and end with the password. A proof made for
  another account, forged, or of a device that ended counts as none.
  Each login that works renews its device.
- A client without one (a new browser, a script without a cookie jar) is
  counted per address and per account, as before, and may still be held
  back while someone floods the account; a password reset, which needs
  the user's mail, ends that for the user. Guessing from many addresses
  stays bounded by the account's count.

`DEVICE_TTL = None` hands out no proofs, as before 0.10.
[ADR 0087](../adr/0087-count-proven-devices-apart-and-bound-scopes-by-route.md)
has the measurements behind this choice: under an hour's attack from 200
addresses, a stored lockout checked 40 guesses and let none of the user's
logins in; device proofs checked 609, as before, and let every login from
a known device in. With six valid proofs and no ceiling they checked
4,263; the ceiling holds them to 1,828.

### Sessions

- **A new login ends the browser's last session.** `POST /session` and
  `/session/mfa` from a browser whose cookie still holds a session end
  that session: the browser replaces the cookie, so a copy of the old one
  would otherwise work on, unseen, for `COOKIE.TTL`
  (`COOKIE.END_PREVIOUS_SESSION = False` keeps it).
- **Logins are bounded.** A user holds at most `AUTH.MAX_LOGINS` (100)
  logins at once: login tokens, browser sessions, and JSON Web Token
  logins (refreshing adds none). A new one past it ends the oldest, so no
  user is refused for their own history; personal tokens neither count
  nor end.
- **The session list.** `GET /auth/tokens` (`personal_tokens.router`)
  lists them, with `kind`, `created_at`, `last_used_at`, and `current`;
  `DELETE /auth/tokens/{id}` ends one, wherever it is used.
- **Idle logins end.** With `AUTH.IDLE_TIMEOUT` (off by default; 5 minutes
  to 30 days), a login token or a browser session that authenticated
  nothing that long is refused (`401`, "Session ended after inactivity";
  a cookie leaves the request anonymous), and a JSON Web Token login not
  refreshed that long cannot refresh, nor can an access token issued
  longer ago than the timeout authenticate, whatever its lifetime. An open
  WebSocket or event stream does not use its login: its re-checks
  (`LIVE_REAUTH_INTERVAL`) write no last use, so it closes once its login
  idles out (since 0.15). Last uses are then written every
  tenth of the timeout, so a login ends no sooner than the timeout and
  within a tenth of it after, at one more `UPDATE` per active login per
  tenth (40 an hour for 15 minutes, 12 by default). A kept principal that
  looks idle is read again before it is refused: another process may have
  served it since. Personal tokens and certificates have no idle timeout.

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
- Wrong codes count against `MFA.RATE` (10 an hour) per user, wherever
  they come from, and at `/mfa/login` against `MFA.ADDRESS_RATE` (60 an
  hour) per client address: a guesser who has the password gets a few
  hundred tries a day at a one-in-a-million code. Codes that work are not
  counted, so an office or a carrier's network behind one address logs
  in as often as it needs. At `/mfa/login` a code counts only once its
  challenge holds, so a forged challenge cannot use up either count;
  challenges that do not hold count against `LOGIN_RATE` per address once
  checked, and never hold a valid one back. Anyone with the password can
  use up the user's count, and hold the user's logins back for the hour.
  The audited `throttled` refusals show it.
- Turning it off, and new recovery codes, need the password and a code:
  a stolen session cannot remove the factor.
- Secrets are stored encrypted with AES-256-GCM under a key derived from
  `SECRET_KEY`, bound to the user's id ([ADR 0030](../adr/0030-encrypt-totp-secrets-under-a-derived-key.md)).
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
their links; verification links skip such a user the same way. Your page
posts the token and the new password to `/password-reset/confirm`. The
new password is checked by the validators, and every session of the
user ends.

Reset tokens are signed with `SECRET_KEY` and never stored, not even in
the outbox: the worker makes them as it mails them. A token rests on the
user's password hash, last login, email, and status, so it stops working
once it is used, once the user logs in, or once any of those changes. A
wrong token, a used or expired one, and one for no user all get the same
`400 invalid_token`. Requests for mail count against `EMAIL_RATE` (5 an
hour) per client address and per email address; tokens that do not hold
count against `LOGIN_RATE` per client address, once checked. They are not
counted per user, nor before the check: a signature cannot be guessed,
and a count that anyone could use up with forged tokens would lock people
out of the links they were sent. A token that holds always works.

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
| `POST /signup` | anyone | `{"username", "email"}` | `202`; `422` for fields that do not fit; `429 throttled`. |
| `POST /signup/confirm` | anyone | `{"token", "password"}` | `204`: the account is made; `400 invalid_token`; `409 signup_taken`; `422` for a password the validators refuse; `429 throttled`; `503 password_checks_busy`. |

The fields are checked at once (`422`), which tells nothing about other
accounts; no password is taken. Then the request is kept as a pending
signup, and it answers `202`, doing the same work whatever accounts
exist. The worker mails the address: when neither the username (in any
case) nor the address has an account, a link (`AUTH.SIGNUP_URL` with the
token in place of `{token}`, the token alone without one) that works for
`EMAIL_VERIFICATION_TIMEOUT`; otherwise, word that an account uses the
address or the username, and the pending signup is dropped. Your page
asks for a password and posts it with the token to `/signup/confirm`,
which checks it with `AUTH.PASSWORD_VALIDATORS` (a refusal leaves the
link working, to try another), makes the account with it, its address
verified, and the user can log in.

Whoever reads the mail chooses the password, so asking for an account
with someone else's address gains nothing (pre-account hijacking): if
the address's owner uses the link, the account is theirs, with a
password only they know. Were a password taken when asking, as in
Django-style forms, the person who asked would set it, and an owner who
used the link would make an account its asker can log in to.

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

Pending signups keep the username and the address, and no password, in
`domainkit_pending_signups` (run `makemigrations` and `migrate`), which
is never in fixtures: it holds the addresses of people who may never
have asked. The outbox holds only a pending signup's id.
A row goes when its link is used, when the worker has told its address
that the name or the address is taken, or, past
`EMAIL_VERIFICATION_TIMEOUT`, with the hourly purge of expired tokens.
Signups count against `EMAIL_RATE` per client address and per email
address, links that do not hold against `LOGIN_RATE` per client address
(once checked, so forged ones never hold a valid one back), and new
accounts are audited as `auth.user_signed_up`.
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

