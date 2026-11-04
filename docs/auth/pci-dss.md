# PCI DSS v4.0's passwords: the preset, requirement by requirement

PCI DSS is the security standard for services that store, process, or
send card data. Its requirement 8 asks how users authenticate. Most of
what it asks of passwords is a setting of `domainkit.contrib.auth`, and
one preset sets them all. This page shows the preset, what each of its
settings answers, and what changes for your users.

On this page you learn how to:

- turn the preset on, and ask for more than it asks;
- read the check that refuses settings which contradict it;
- see which domainkit control answers each part of requirement 8;
- tell users what changes for them.

## A first example

The preset is opt-in:

```python
AUTH = {
    "PRESET": "pci-dss-4",
}
```

It sets each of the keys below that `AUTH` leaves out. A key you set
yourself is kept. Startup then checks the result. A setting that
contradicts the preset (a minimum under 12 characters, a history under
4, more than 10 wrong passwords before refusing, ...) is refused with
`auth.E010`, in every environment. The message names each contradiction
and its requirement:

```python
AUTH = {"PRESET": "pci-dss-4", "PASSWORD_HISTORY": 2, "LOGIN_RATE": "10/minute"}
```

```text
AUTH settings cannot be used:
  auth.E010: AUTH.PRESET 'pci-dss-4' is contradicted: PASSWORD_HISTORY is 2, under 4 (8.3.7); LOGIN_RATE '10/minute' allows more than 10 wrong passwords, or more than 10 every 30 minutes (8.3.4). Leave these settings out for the preset's, or drop the preset.
```

Ask for more than PCI asks (a history of 12, 16 characters, `"5/hour"`)
and it holds. Under the preset, a short password is refused for three
reasons:

```text
['This password is too short. It must contain at least 12 characters.', 'This password is too common.', 'This password must contain both letters and digits.']
```

## What the preset sets

| Key | The preset's value | Without the preset |
| --- | --- | --- |
| `PASSWORD_VALIDATORS` | similarity to the user's details, `MinimumLengthValidator` with `min_length` 12, common passwords, `LettersAndDigitsValidator` | similarity, 8 characters, common passwords, not only digits |
| `PASSWORD_HISTORY` | `4` | `0` |
| `PASSWORD_CHANGE_ON_FIRST_USE` | `True` | `False` |
| `LOGIN_RATE` | `"10/hour"` | `"10/minute"` |
| `PASSWORD_CHECKS_COUNT_AS_LOGINS` | `True` | `False` |
| `IDLE_TIMEOUT` | 13 minutes | `None` |

`auth.E010` refuses, when the preset is on:

- validators without a `MinimumLengthValidator` of 12 or more, or without
  `LettersAndDigitsValidator` (8.3.6);
- a `PASSWORD_HISTORY` under 4 (8.3.7);
- `PASSWORD_CHANGE_ON_FIRST_USE` off (8.3.5);
- a `LOGIN_RATE` of `None`, or one that lets more than 10 attempts pass,
  or more than 10 every 30 minutes (8.3.4);
- `PASSWORD_CHECKS_COUNT_AS_LOGINS` off (8.3.4);
- an `IDLE_TIMEOUT` of `None`, or more than about 13.6 minutes (8.2.8);
- with `JWTAuthentication`, `JWT.ACCESS_TOKEN_TTL` and `JWT.LEEWAY`
  together above 15 minutes (8.2.8).

## Requirement by requirement

| Requirement | What answers it |
| --- | --- |
| 8.2.8: a session idle for more than 15 minutes asks the user to authenticate again | `IDLE_TIMEOUT`: 13 minutes. A login ends between the timeout and a tenth more after its last use (last uses are written a tenth as often), so by 14.3 minutes: within 15. `auth.E010` refuses a timeout that could end later (more than 13.6 minutes, or none). A JSON Web Token's access token works unrefreshed for `AUTH.JWT.ACCESS_TOKEN_TTL` plus `LEEWAY`: `auth.E010` refuses the two above 15 minutes together, and any access token issued longer ago than `IDLE_TIMEOUT` is refused anyway. An open WebSocket or event stream is no use of its login: its checks write no last use, so it closes when the login idles out. Personal tokens are no session: they do not idle out, so making one asks the user to authenticate again (a step-up, or the password), and it works `PERSONAL_TOKEN_MAX_AGE` (90 days) at most. |
| 8.3.1: every user's access is authenticated | Every route declares who may call it ([deny by default](routes.md#deny-by-default), `security.E101`); tokens, sessions, and certificates name a user ([Authenticating requests](authentication.md)). |
| 8.3.2: factors unreadable in transit and storage | Passwords are hashed (PBKDF2-SHA256 by default, `PASSWORD_HASHERS`; hashers made for tests are refused where deployed, `auth.E004`). Tokens are stored as digests. TOTP secrets are encrypted ([Logging in](logging-in.md#two-factor-logins)). Cookies are Secure where deployed (`auth.E005`). HTTPS is the deployment's. |
| 8.3.4: invalid attempts are limited, locking the account out after at most 10, for at least 30 minutes or until the user's identity is confirmed | `LOGIN_RATE` `"10/hour"`: ten wrong passwords for an account, from any address, and the next attempt is refused before its password is checked. Places then free one every 6 minutes, so the account takes at most 10 guesses an hour, fewer than PCI's 10 every 30 minutes. There is no stored lockout, by decision ([ADR 0087](../adr/0087-count-proven-devices-apart-and-bound-scopes-by-route.md)): measured, a lockout let an attacker keep the user out for as long as they liked, for the same bound on guesses. Clients that logged in to the account before carry a device proof and are counted apart, so a stranger's failures never lock the user out of their own devices. This is the control to describe under PCI's customized approach: the objective (guesses bounded per account) is met, without the denial of service. `ACCOUNT_LOGIN_RATE` (100 a day) bounds the day as well. Wrong passwords of signed-in users (`/auth/password`, a step-up, the second factor's changes) count on the account's count too (`PASSWORD_CHECKS_COUNT_AS_LOGINS`), so they add no guesses. The price is that a stranger's failures can hold back a signed-in user's password change for a while, as they hold back logins from unknown devices. |
| 8.3.5: a password set or reset for a user is unique, and changed at its first use | `PASSWORD_CHANGE_ON_FIRST_USE`. A password that staff set through the admin API (`POST /admin/auth/users` with a password, or `POST /users/{id}/password` for another user), or that an operator set with `changepassword <user>` (unless `--no-temporary`), logs in only to be changed. `/login` and `/session` answer `403 password_change_required` until the login sends `new_password`. The new password then replaces it (checked by the validators and the history), ends the user's other logins, and logs in. A user with a second factor sends `new_password` with the code (`/mfa/login`, `/session/mfa`): the password alone never sets a lasting one. `createsuperuser --noinput` makes the password it reads from the environment temporary too, unless `--no-temporary`. Self-service resets are chosen by the user, and need no change. |
| 8.3.6: at least 12 characters, with both numeric and alphabetic characters | `MinimumLengthValidator` (`min_length` 12) and `LettersAndDigitsValidator` (a letter and a digit, of any script). `createsuperuser` offers no way past them under the preset, nor in staging and production (AUTH-18). |
| 8.3.7: not the same as any of the last four | `PASSWORD_HISTORY` 4: the hashes of each user's last passwords are kept (`domainkit_password_history`), and a new one that matches any of them, or the current one, is refused (`422 invalid_password`). It is checked wherever a password is stored: `/auth/password`, resets, the admin API, `changepassword`, and the change at first use. The password chosen at signup is kept too (AUTH-15). A kept hash of a hasher since removed from `PASSWORD_HASHERS` cannot be compared: it is skipped, with a security log line, because counting it as a match would refuse every new password. Keep a removed hasher listed for as long as its hashes are among the last four. |
| 8.3.8: policies are documented and communicated | The project's. `password_help_texts()` lists what each configured rule asks, for the page where users choose a password. |
| 8.3.9 and 8.3.10.1: a password that is the only factor is changed every 90 days, or access is analysed dynamically | Not set by the preset. The answer is a second factor (8.4), with `auth.mfa` and, for the acts that matter, `StepUp`. With a second factor, the password is not the only one. |
| 8.4: multi-factor authentication | `auth.mfa` (an authenticator app; recovery codes). `StepUp(max_age=...)` asks for a code again for an act ([Stepping up](step-up.md)). Requiring it of every user is the project's choice. |
| 10.2.1.4: invalid access attempts are logged | Refused logins, codes, step-ups, transactions, links, and `403`s go to the audit trail with `domainkit.contrib.audit` installed. |

## What changes for users

- **New passwords.** At least 12 characters, with letters and digits;
  none of their last four. The refusal lists each rule broken, as
  `InvalidPassword` always does.
- **Passwords set for them.** The first login sends the password staff
  gave, and a new one, in one request:

  ```http
  POST /auth/login
  {"username": "alice", "password": "<given>", "new_password": "<chosen>"}
  ```

  Without `new_password`, the answer is `403 password_change_required`,
  recorded in the audit trail. With a second factor, `/login` answers
  the challenge and changes nothing; `new_password` goes with the code to
  `/mfa/login`.
- **Idle sessions.** After 13 minutes without a request, a login token
  or a browser session ends, an open live page left alone included. A
  JSON Web Token login's access token stops working 13 minutes after it
  was issued, unless it is refreshed.

The history and the change at first use keep rows only while their
settings are on. Turning them off leaves the rows, which deleting or
erasing a user deletes.

## Errors and refusals

| What you see | Why |
| --- | --- |
| `auth.E010` at startup | A setting contradicts the preset. Leave it out, or ask for more. |
| `403 password_change_required` | A password staff set; send `new_password`. |
| `422 invalid_password` | A rule, or the history, refuses the new password. |
| `401` "Session ended after inactivity; log in again." | The login idled out. |
| `429 rate_limited` | Ten wrong passwords this hour. |

## Common mistakes

- **Setting `LOGIN_RATE = "10/minute"` with the preset.** It lets 300
  guesses in 30 minutes; `auth.E010` refuses it.
- **Turning `PASSWORD_CHECKS_COUNT_AS_LOGINS` off.** Signed-in users'
  checks would add guesses; `auth.E010` refuses it.
- **Removing a hasher while its hashes are in the history.** The history
  skips them. Keep the hasher listed until four new passwords have
  passed.
- **A long JSON Web Token lifetime.** `ACCESS_TOKEN_TTL` plus `LEEWAY`
  may not pass 15 minutes.

## See also

- [Users, groups, backends, and passwords](users.md#passwords): hashers
  and validators.
- [Logging in](logging-in.md): wrong passwords, device proofs, and idle
  sessions.
- [Stepping up](step-up.md): a second factor for the acts that matter.
- [The settings tables](../auth.md#settings): every key.
