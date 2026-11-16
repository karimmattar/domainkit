# 0148. Check new passwords against breaches, and let password rules be async

- Status: Accepted
- Date: 2026-11-16

## Context

A project reported that domainkit has no breached-password validator, and
that validators are synchronous, so a rule cannot call a range API (user
report A11).

### What exists

- **`AUTH.PASSWORD_VALIDATORS`** (`contrib/auth/validators.py`) lists
  classes with `validate(password, user)` and `help_text()`, as Django's
  `AUTH_PASSWORD_VALIDATORS` does. `validate_password` calls each one and
  collects their `PasswordValidationError`s into one `InvalidPassword`
  (422). It is sync.
- **`CommonPasswordValidator`** refuses Django's 20,000 common passwords,
  a list drawn from breach corpora, held in the package.
- **Every caller is async but two.** `/auth/password`, resets and signups
  by code, the admin API, a change at the first login (with or without a
  second factor), and a phone's enrollment call it from async code. The
  commands `changepassword` and `createsuperuser` call it from a sync
  `handle`, which reaches the database with `run_async`.
- A rule with `async def validate` is accepted by every check, and never
  runs: `validate` returns a coroutine, nothing awaits it, and the
  password is stored. Python warns that the coroutine was never awaited;
  the request succeeds.

### What the standards ask

- **NIST SP 800-63B** [1] (rev. 3, 5.1.1.2; rev. 4, 3.1.1.2): when a
  password is set or changed, verifiers SHALL compare it with a list of
  commonly used, expected, or compromised values ("passwords obtained
  from previous breach corpuses"), SHALL tell the subscriber the reason,
  and SHALL require another one. The whole password is compared, not its
  parts.
- **OWASP ASVS 5.0** [2]: 6.2.4 (level 1) asks for at least the top 3,000
  passwords; 6.2.12 (level 2) asks that passwords set at registration or
  change are checked against a set of breached passwords.
  `CommonPasswordValidator` meets the first; nothing meets the second.

### Have I Been Pwned's Pwned Passwords

The range API [3] holds the hashes of the passwords seen in breaches,
hundreds of millions of them:

- `GET https://api.pwnedpasswords.com/range/{first 5 hex of SHA-1}`
  answers every hash under that prefix as `SUFFIX:COUNT` lines (35 hex
  characters, then the times it was seen), about 800 of them. Every one
  of the 1,048,576 prefixes answers 200. The caller compares the rest of
  its hash: the password and its full hash never leave (k-anonymity, as
  Cloudflare designed it [4]).
- **`Add-Padding: true`** [5] pads every answer to 800 to 1,000 lines with
  random suffixes that count 0, so the size of an answer, seen on the
  wire through TLS, does not tell the prefix. Padding lines are dropped
  by reading counts of 0 as "not found".
- **No key, no rate limit** for Pwned Passwords, HTTPS only; a
  `User-Agent` is recommended, not required. It is served through
  Cloudflare; no availability is promised. The hashes can be downloaded
  and served from a mirror on the same paths.

### How others do it

- **Django** has no breached-password validator and no async validation:
  `validate_password` is sync, and `contrib.auth`'s async work stops at
  `acheck_password` and the like [6].
- **pwned-passwords-django** [7] lists `PwnedPasswordsValidator` in
  `AUTH_PASSWORD_VALIDATORS`, with `PWNED_PASSWORDS = {"ADD_PADDING": True,
  "API_TIMEOUT": 1.0}`. On a timeout or any error it logs and falls back
  to `CommonPasswordValidator`: it fails open. It offers
  `check_password_async`, but Django cannot await a validator.
- **django-pwned-passwords** has `PWNED_VALIDATOR_FAIL_SAFE`: while it is
  true, anything but a password the API names passes, timeouts included.

### Options

Async rules:

| Option | What it costs |
| --- | --- |
| Make `validate_password` itself async | Breaks every project that calls it: an unawaited call returns a coroutine and checks nothing, silently, the very bug reported. |
| Run async rules from the sync function, on a loop of their own | Cannot be done from a running loop, where every route is; a thread per call to hide it. |
| **Add `avalidate_password`, which awaits `async` rules; `validate_password` refuses to run one** | A second function, as `amake_password` and `acheck_password` are; the framework's callers move to it. A project that calls the sync one with an async rule gets an error, not an unchecked password. |

Turning the check on and setting it:

| Option | What it costs |
| --- | --- |
| A class listed in `PASSWORD_VALIDATORS`, set by its `OPTIONS` | Options are kwargs, unvalidated by the settings; turning it on means writing out the four default rules again, or the preset's. |
| **A settings group, `AUTH.BREACHED_PASSWORDS`, validated like the others, whose `ENABLED` runs the class after `PASSWORD_VALIDATORS`** | One key turns it on, with the defaults or a preset's rules kept; bounds are checked at startup; the rules run in two lists. |

When the API cannot answer:

| Option | What it costs |
| --- | --- |
| Fail closed: refuse | A third party's outage stops signups, resets (people locked out of their accounts), and changes. |
| Fail open, silently | An outage, or a proxy that answers with a page, turns the check off unseen. |
| **Fail open, logged, by default; fail closed on request** | During an outage only the local rules hold: the 20,000 common passwords, length, likeness to the user. |

## Decision

### Rules may be async

- A rule's `validate` may be `async def`, or return an awaitable.
  **`avalidate_password(password, user, validators=None)`** runs every
  rule in order, awaits those that need it, and raises `InvalidPassword`
  with every refusal, as `validate_password` does. An exception other
  than `PasswordValidationError` goes up.
- **`validate_password` stays sync** and refuses a rule whose `validate`
  returns an awaitable: it closes the coroutine and raises
  `ImproperlyConfigured` naming the class and `avalidate_password`. It
  never takes a password unchecked.
- **Every framework caller awaits `avalidate_password`**: `/auth/password`,
  `/password-reset/code`, `/signup/code`, the admin API's new user and
  new password, the change at the first login (`/login`, `/session`,
  `/mfa/login`, `/session/mfa`) and at a phone's enrollment. The commands
  run it with `run_async`, and stop with the error's detail when a rule
  raises an `UpstreamError`.
- Messages, and help texts, are translated (`gettext`) as they are
  collected, so a refusal is in the request's language even when several
  rules refuse.

### The breached-password check

- **`AUTH["BREACHED_PASSWORDS"]`** (`BreachedPasswordSettings`):
  - `ENABLED` (`False`): run `BreachedPasswordValidator` after
    `PASSWORD_VALIDATORS`. Off by default: turning it on sends a hash
    prefix of every new password to a third party, which the project
    decides.
  - `URL` (`https://api.pwnedpasswords.com/range/`): `http` or `https`,
    ending in `/`, for a mirror too.
  - `TIMEOUT` (2 seconds, at most 10), retries included.
  - `MIN_COUNT` (1, up to 1,000,000): times seen before a password is
    refused. At least 1, so padding never refuses.
  - `FAIL_CLOSED` (`False`).
- **The call** goes through `domainkit.http.client` (the `httpclient`
  extra; without it, startup refuses the setting). One client per
  settings, made at startup: its circuit is shared, so after
  `BREAKER_FAILURES` failures in a row checks fail at once, and nobody
  waits `TIMEOUT` while the API is down. It sends the five-character
  prefix, upper case, and `Add-Padding: true`; no request id, no trace
  context. The request's deadline still bounds it, and cuts it as it
  cuts any work.
- **The prefix stays out of logs.** A plain client logs each call's path,
  and `CallTimeout`'s message names it. A prefix next to a request id
  narrows the password's hash by 2^20. `HttpClient` gets
  `log_paths=False` (default: unless guarded), which logs, and names in
  errors, the origin alone. A guarded client would do the same, but
  ignores the environment's proxy and refuses private addresses, which
  a mirror may have.
- **Reading the answer**: every line must be `<35 hex>:<count>`, and
  there must be one; anything else is an answer from something else (a
  proxy's page) and counts as no answer. The suffix is compared in upper
  case.
- **No answer** (a network error, a timeout, an open circuit, a status
  that is not 2xx, an answer that does not read) logs a warning naming
  the error's type, and lets the password through, the other rules
  having run. With `FAIL_CLOSED` it raises `BreachedPasswordsUnavailable`
  (an `UpstreamError`, `503 breached_passwords_unavailable`) instead.
  Fail open is the default because the local list keeps NIST's
  requirement met during an outage, because a reset refused for a third
  party's outage locks people out, and because both libraries that do
  this for Django let passwords through.
- **The refusal** is "This password has appeared in a data breach:
  choose another one.", in English and Arabic: the reason NIST asks for.

## Consequences

- A project turns the check on with one key, and keeps its rules or its
  preset's. Its own rules may be async: one that reads a table or asks a
  service now runs, where before it was skipped.
- `validate_password` raises in a project that calls it with an async
  rule configured, the breached-password check included. That is the
  point: the call would have checked less than it says. The guide's
  common mistakes name it.
- A route that validates inside its transaction (a reset, a signup's
  code, a change at the first login, the admin's new password) holds its
  connection during the call: `TIMEOUT` at most, nothing while the
  circuit is open. Moving the check out would mean spending a code in
  one transaction and setting the password in another; not done.
- Signups and changes take one more round trip, about tens of
  milliseconds through Cloudflare. Tests never reach the API: the check
  is off unless a test turns it on, with `stub` as the API.
- Have I Been Pwned learns, per new password, one prefix among
  1,048,576, and the service's address. A mirror avoids even that.
- `HttpClient(log_paths=False)` is new public surface of the client.

## References

1. NIST SP 800-63B, *Digital Identity Guidelines: Authentication and
   Lifecycle Management*: rev. 3, 5.1.1.2
   <https://pages.nist.gov/800-63-3/sp800-63b.html>; rev. 4, 3.1.1.2
   <https://pages.nist.gov/800-63-4/sp800-63b.html>
2. OWASP Application Security Verification Standard 5.0.0, V6.2:
   <https://github.com/OWASP/ASVS/blob/v5.0.0/5.0/en/0x15-V6-Authentication.md>
3. Have I Been Pwned, API v3, *Pwned Passwords*:
   <https://haveibeenpwned.com/API/v3#PwnedPasswords>
4. Cloudflare, *Validating Leaked Passwords with k-Anonymity*:
   <https://blog.cloudflare.com/validating-leaked-passwords-with-k-anonymity/>
5. Troy Hunt, *Enhancing Pwned Passwords Privacy with Padding*:
   <https://www.troyhunt.com/enhancing-pwned-passwords-privacy-with-padding/>
6. Django ticket #34565, async `check_password`:
   <https://code.djangoproject.com/ticket/34565>
7. pwned-passwords-django, the validator and its settings:
   <https://pwned-passwords-django.readthedocs.io/en/stable/validator.html>,
   <https://pwned-passwords-django.readthedocs.io/en/stable/settings.html>
