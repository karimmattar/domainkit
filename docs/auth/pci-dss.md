# PCI DSS v4.0's passwords: the preset, requirement by requirement

PCI DSS v4.0's requirement 8 asks how users authenticate. Most of what it
asks of passwords is a setting of `domainkit.contrib.auth`; one preset sets
them all, opt-in:

```python
AUTH = {
    "PASSWORD_PRESET": "pci-dss-4",
}
```

It sets each of these keys that `AUTH` leaves out; a key set explicitly
is kept. Startup then checks the result: a setting that contradicts the
preset (a minimum under 12 characters, a history under 4, more than 10
wrong passwords before refusing, ...) is refused with `auth.E010`, in
every environment, naming each contradiction and its requirement. Ask for
more than PCI does (a history of 12, 16 characters, `"5/hour"`) and it
holds.

| Key | The preset's value | Without the preset |
| --- | --- | --- |
| `PASSWORD_VALIDATORS` | similarity to the user's details, `MinimumLengthValidator` with `min_length` 12, common passwords, `LettersAndDigitsValidator` | similarity, 8 characters, common passwords, not only digits |
| `PASSWORD_HISTORY` | `4` | `0` |
| `PASSWORD_CHANGE_ON_FIRST_USE` | `True` | `False` |
| `LOGIN_RATE` | `"10/hour"` | `"10/minute"` |
| `IDLE_TIMEOUT` | 13 minutes | `None` |

## Requirement by requirement

| Requirement | What answers it |
| --- | --- |
| 8.2.8: a session idle for more than 15 minutes asks the user to authenticate again | `IDLE_TIMEOUT`: 13 minutes. A login ends between the timeout and a tenth more after its last use (last uses are written a tenth as often), so by 14.3 minutes: within 15. `auth.E010` refuses a timeout that could end later (more than 13.6 minutes, or none). |
| 8.3.1: every user's access is authenticated | Every route declares who may call it (deny by default, `security.E101`); tokens, sessions, and certificates name a user ([Authenticating requests](authentication.md)). |
| 8.3.2: factors unreadable in transit and storage | Passwords are hashed (PBKDF2-SHA256 by default, `PASSWORD_HASHERS`; hashers made for tests are refused where deployed, `auth.E004`); tokens are stored as digests; TOTP secrets are encrypted ([Logging in](logging-in.md#two-factor-logins)). Cookies are Secure where deployed (`auth.E005`); HTTPS is the deployment's. |
| 8.3.4: invalid attempts are limited, locking the account out after at most 10, for at least 30 minutes or until the user's identity is confirmed | `LOGIN_RATE` `"10/hour"`: ten wrong passwords for an account, from any address, and the next attempt is refused before its password is checked; places then free one every 6 minutes, so the account takes at most 10 guesses an hour, fewer than PCI's 10 every 30 minutes. There is no stored lockout, by decision ([ADR 0087](../adr/0087-count-proven-devices-apart-and-bound-scopes-by-route.md)): measured, a lockout let an attacker keep the user out for as long as they cared, for the same bound on guesses. Clients that logged in to the account before carry a device proof and are counted apart, so a stranger's failures never lock the user out of their own devices. This is the control to describe under PCI's customized approach: the objective (guesses bounded per account) is met, without the denial of service. `auth.E010` refuses a rate that passes more than 10 attempts, or more than 10 every 30 minutes. |
| 8.3.5: a password set or reset for a user is unique, and changed at its first use | `PASSWORD_CHANGE_ON_FIRST_USE`: a password staff set through the admin API (`POST /admin/auth/users` with a password, or `POST /users/{id}/password` for another user) logs in only to be changed: `/login` and `/session` answer `403 password_change_required` until the login sends `new_password`, which then replaces it (checked by the validators and the history), ends the user's other logins, and logs in. Self-service resets are chosen by the user, and need no change. |
| 8.3.6: at least 12 characters, with both numeric and alphabetic characters | `MinimumLengthValidator` (`min_length` 12) and `LettersAndDigitsValidator` (a letter and a digit, of any script). |
| 8.3.7: not the same as any of the last four | `PASSWORD_HISTORY` 4: the hashes of each user's last passwords are kept (`domainkit_password_history`), and a new one that matches any of them, or the current one, is refused (`422 invalid_password`). It is checked wherever a password is stored: `/auth/password`, resets, the admin API, `changepassword`, and the change at first use. |
| 8.3.8: policies are documented and communicated | The project's. `password_help_texts()` lists what each configured rule asks, for the page where users choose a password. |
| 8.3.9 and 8.3.10.1: a password that is the only factor is changed every 90 days, or access is analysed dynamically | Not set by the preset: the answer is a second factor (8.4), with `auth.mfa` and, for the acts that matter, `StepUp`. With a second factor, the password is not the only one. |
| 8.4: multi-factor authentication | `auth.mfa` (an authenticator app; recovery codes); `StepUp(max_age=...)` asks for a code again for an act ([Stepping up](step-up.md)). Requiring it of every user is the project's choice. |
| 10.2.1.4: invalid access attempts are logged | Refused logins, codes, step-ups, transactions, links, and `403`s go to the audit trail with `domainkit.contrib.audit` installed. |

## What changes for users

- **New passwords.** 12 characters at least, letters and digits; none of
  their last four. The refusal lists each rule broken, as
  `InvalidPassword` always does.
- **Passwords set for them.** The first login sends the password staff
  gave and a new one, in one request:

  ```http
  POST /auth/login
  {"username": "alice", "password": "<given>", "new_password": "<chosen>"}
  ```

  Without `new_password`, `403 password_change_required`, recorded in the
  audit trail. With a second factor, the challenge follows the change, as
  at any login.
- **Idle sessions.** After 13 minutes without a request, a login token or
  a browser session ends; a JSON Web Token login ends at its next refresh.

The history and the change at first use keep rows only while their
settings are on; turning them off leaves the rows, which deleting or
erasing a user deletes.
