# 0146. Show gates how a login was made, and let a login keep shorter limits

- Status: Accepted; amended by [0164](0164-let-the-asker-withdraw-a-phone-request-count-sessions-per-user-and-word-each-kind.md)
- Date: 2026-11-16

## Context

A project that runs an identity service on the framework has a staff
dashboard next to its customers' app. It reported two gaps (user report
A11):

1. **A login gate cannot see how the login is made.** A gate is
   `(request, user)` ([ADR 0128](0128-approve-sign-ins-from-the-users-mobile-app.md)).
   The project's dashboard gate guessed the method from the path and the
   `Origin` header. `Credentials` (`request.auth`) has no method, no age,
   and no device either, so a route cannot ask "was this login made with
   a second factor, and how long ago?".
2. **Every login has the same limits.** `AUTH.IDLE_TIMEOUT`,
   `COOKIE.TTL`, `TOKEN_TTL` and `JWT.REFRESH_TOKEN_TTL` are one value
   each, for every user. The project could not end its staff's sessions
   sooner without forking auth.

### What exists

- **The token row records how its login signed in**
  ([ADR 0130](0130-show-where-each-login-came-from-and-manage-logins-from-the-phone.md)):
  `method` (`password`, `passkey`, `device`, `push`, `qr`, `provider`,
  `impersonation`), `agent`, `ip`, `device_id`, and `created_at`. A
  refreshed JSON Web Token login copies them. But `method` says
  "password" whether a second factor followed or not, and nothing reads
  the row back into `Credentials`.
- **The route knows the method; the gate runs before it says.** Every
  login route calls `pass_login_gates(request, user)`, then
  `issue_login(user, method=...)`. The method reaches the token store
  through a context the gates never see.
- **The device is known once checked.** The devices app writes the bound
  device that signed a request into its scope (`note_login_device`) when
  `signing_device` checks the signature. With `CIBA.APPROVE_LOGINS`,
  ciba's gate checks it; elsewhere nothing does.
- **ciba's gate keeps its own marks** (`mark_approved`), and decides by
  the signature: it needs nothing new.
- **Step-ups already speak RFC 8176**: `pwd`, `otp`, `hwk` for a bound
  device's key, and `hwk` with `mfa` for a passkey verified with its user
  (`StepUp`, `STANDS_FOR`). A JSON Web Token's `auth_time` and `amr`
  claims, and `Credentials.auth_time` and `amr`, are the step-up's.

### What the standards say

- **NIST SP 800-63B-4** [1] sets two limits per assurance level: an
  overall timeout from the authentication (30 days at AAL1, 24 hours at
  AAL2, 12 hours at AAL3) and an inactivity timeout (none required at
  AAL1, 1 hour at AAL2, 15 minutes at AAL3). Section 5.1: "A session
  SHOULD inherit the AAL properties of the authentication event that
  triggered its creation." The limits follow how the user authenticated.
- **OWASP's Session Management Cheat Sheet** [2] asks for both an idle and
  an absolute timeout on every session, enforced on the server, shorter
  for high-value use (idle 2 to 5 minutes, against 15 to 30 for low risk).
- **OWASP ASVS 5.0** [3], V7.1.1, V7.3.1 and V7.3.2: the inactivity
  timeout and the absolute lifetime are documented, and set "according
  to risk analysis".
- **OpenID Connect** [4] reports how a user authenticated in the ID
  token: `auth_time`, and `amr`, whose values RFC 8176 [5] registers.

### What others do

- **Django** keeps the backend that authenticated the user in the session
  [6], and `request.session.set_expiry()` gives one session its own
  inactivity timeout, typically when the user logs in [7]. django-otp
  marks the request's user with the device that verified them
  (`user.otp_device`, `user.is_verified()`) [8].
- **Okta** gives its Admin Console sessions a 12-hour lifetime and a
  15-minute idle timeout by default, apart from users' sessions [9].
  **Microsoft Entra** sets a sign-in frequency per policy, for "high
  impact users" among others [10].

Each fixes the limits when the session is made, from who logged in and
how, and keeps them with it.

### Options

| Option | For the gates | For the limits |
| --- | --- | --- |
| Gates of three arguments, `(request, user, origin)` | Every gate, ciba's and the projects', changes; or `HookPoint` takes two counts | |
| **The route names the method to `pass_login_gates`; a gate reads `login_origin(request)`** | **Existing gates unchanged** | |
| Settings per role: `AUTH.STAFF_SESSIONS = {...}` | | One rule ("staff"), new keys, no way to decide by how the login was made |
| A hook asked on every request | | Project code on the hot path, in the authentication middleware; limits that move under a live login; the proof (`amr`) must be stored for it anyway |
| **A hook asked when the login is made; the login keeps the outcome** | | **One call per login, async allowed; the per-request path reads the row; as NIST and Django do** |

## Decision

### The route says how the login is made

`pass_login_gates(request, user, *, method=None, amr=(), device_id=None)`
records a `LoginOrigin` in the request's scope before it runs the gates:

- `method`: a `LoginMethod`, as `issue_login` takes.
- `amr`: what the login proved, in RFC 8176's names, as step-ups use them:
  `pwd` (`/login`, `/session`), `pwd` and `otp` (`/mfa/login`,
  `/session/mfa`; a recovery code stands in for the app, so it counts as
  `otp`), `hwk` and `mfa` (a passkey), `hwk` (a bound device's login or
  enrollment, and a sign-in the phone approved). An identity provider's
  login proves what the provider checked, which domainkit does not know:
  none.
- `at`: now.
- `device_id`: the route's, or the bound device that signed the request
  once the devices app checked it. A device's signature adds `hwk`.

A gate stays `(request, user)` and reads `login_origin(request)`. Every
login route of auth, devices, ciba, and identity names its method and
proof. `issue_login` and `start_browser_session` take `amr=` beside
`method=` and `device_id=`.

### The login keeps it, and its credentials say it

The tokens table gains a nullable `amr` column. The token store fills it
as it fills `method` and `device_id`, and a refreshed login copies it.
`Credentials` gains `origin: LoginOrigin | None`: for a login token or a
browser session, from its row (`created_at` is when it was made); for a
JSON Web Token, from its login's first refresh token, read with the
login in the same statement. A personal token, a certificate, or a class
of the project's has none. `Credentials.auth_time` and `amr` stay the
step-up's.

### A hook gives some logins shorter limits

`on_session_limits(limiter)` registers an async function of the user and
the `LoginOrigin` that returns `SessionLimits(idle_timeout=...,
lifetime=...)`, or None. `issue_login` and the browser session ask every
hook when the login is issued, past the gates:

- The shortest of each limit holds, and none outlasts its setting. A hook
  cannot lengthen a session.
- `lifetime` shortens the login's expiry: the login token's (`TOKEN_TTL`),
  the browser session's and its cookies' `Max-Age` (`COOKIE.TTL`), the
  JSON Web Token login's refresh tokens (`REFRESH_TOKEN_TTL`). The login
  token's `expires_in` says so.
- `idle_timeout` is kept in a new nullable column of the login's rows,
  `idle_timeout`, in seconds, and copied on refresh. Each request takes
  the shorter of it and `AUTH.IDLE_TIMEOUT`; last uses are written a
  tenth as often, as for the setting.
- Bounds as the setting's: an idle timeout from 5 minutes to 30 days; a
  positive lifetime.

A login keeps the limits it was made with. A user made staff later keeps
their logins' limits until they end: end them (the admin API's
`logout`) for the new ones to hold at once.

## Consequences

- **A gate decides by facts.** "Staff log in only with a second factor,
  and never from the app" reads `amr` and `device_id`, not the path. A
  route asks `request.auth.origin` the same, and how old the login is.
- **Staff sessions end sooner**, by a hook of a few lines; everyone else
  keeps the settings, and no hook keeps today's behaviour.
- **Existing gates and hooks work unchanged.** A login route of the
  project's that does not name its method gives its gates an origin with
  no method or proof, and records none, as before.
- **Migrations**: the tokens table gains two nullable columns, `amr` and
  `idle_timeout` (`makemigrations`, then `migrate`). Logins made before
  show no proof, and keep the settings' limits.
- **New names** in `auth.gates`: `LoginOrigin`, `SessionLimits`,
  `SessionLimiter`, `login_origin`, `on_session_limits`; `amr=` on
  `pass_login_gates`, `issue_login` and `start_browser_session`;
  `Credentials.origin`.
- **What it does not say**: an identity provider's proof (its own `amr`
  is not read), and the first step of a provider login finished by a
  code, which is recorded as `password` with `pwd` and `otp`, as its
  method already was. A gate before ciba's in `INSTALLED_DOMAINS` order
  sees a signing device only if something checked the signature first.
- **To watch**: a hook that raises fails the login it was asked about; a
  hook is asked once per login, so its cost is the login's, never a
  request's.

## References

1. NIST SP 800-63B-4, *Authentication and Authenticator Management*,
   section 2 (reauthentication at each AAL) and section 5 (session
   management): <https://pages.nist.gov/800-63-4/sp800-63b.html>
2. OWASP Cheat Sheet Series, *Session Management*, "Session Expiration":
   <https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html>
3. OWASP ASVS 5.0, V7 *Session Management*:
   <https://github.com/OWASP/ASVS/blob/master/5.0/en/0x16-V7-Session-Management.md>
4. OpenID Connect Core 1.0, the ID token's `auth_time` and `amr`:
   <https://openid.net/specs/openid-connect-core-1_0.html#IDToken>
5. RFC 8176, *Authentication Method Reference Values*:
   <https://www.rfc-editor.org/rfc/rfc8176.html>
6. Django, *Using the Django authentication system*, `login()`:
   <https://docs.djangoproject.com/en/5.2/topics/auth/default/#how-to-log-a-user-in>
7. Django, *How to use sessions*, `set_expiry()`:
   <https://docs.djangoproject.com/en/5.2/topics/http/sessions/>
8. django-otp, *Overview*, `OTPMiddleware`:
   <https://django-otp-official.readthedocs.io/en/stable/overview.html>
9. Okta, *Admin session lifetime and idle timeout security enhancements*:
   <https://support.okta.com/help/s/article/admin-session-lifetime-idle-timeout-security-enhancements>
10. Microsoft Entra, *Conditional Access adaptive session lifetime
    policies*:
    <https://learn.microsoft.com/en-us/entra/identity/conditional-access/concept-session-lifetime>
