# 0130. Show where each login came from, and manage logins from the phone

- Status: Accepted
- Date: 2026-11-09

## Context

A user whose phone is their identity ([ADR 0128](0128-approve-sign-ins-from-the-users-mobile-app.md),
[ADR 0129](0129-complete-the-phone-as-identity.md)) wants to see the web
sessions they are signed in to from the phone, and to end one, several,
or all of them.

`GET /auth/tokens` and `DELETE /auth/tokens/{id}` already list and end a
user's logins, and `POST /auth/logout` with `everywhere` ends all of them.
Three gaps remained.

1. **The user cannot tell one login from another.** Every login is named
   `login`, and its row keeps only its kind and dates. Nothing says which
   browser or app made it, from where, or how it signed in.
2. **No way to end every login but this one.** `everywhere` ends the
   phone's own login too.
3. **Any login may manage the others.** In the phone-as-identity modes, a
   stolen web session could list the user's logins and end the phone's.

Constraints:

- **Boundaries:** auth must not import devices or ciba.
- **The project owner's decisions:** only the bound phone manages logins
  in the phone-as-identity modes; each login records its user agent, its
  IP address, and how it signed in.

## Decision

### Each login records where it came from

A login's token row gains four columns, all optional, so rows made before
this release and personal tokens stay as they are:

- `method`: how it signed in. One of `password`, `passkey`, `device`,
  `push`, `qr`, `provider`, or `impersonation`.
- `agent`: the request's `User-Agent`, at most 200 characters.
- `ip`: the client's address, as `TRUSTED_PROXIES` resolves it.
- `device_id`: the bound device that logged in, for the phone's own login.

The token store fills them when it writes a login's row: the address and
the agent from the request in progress, and the method and the device
from the code that issues the login (`issue_login` and
`start_browser_session` take `method=` and `device_id=`). A refreshed JSON
Web Token login copies them from the refresh token it replaces, so the
login keeps its origin. The values are personal data: they live as long
as the login, and end with it, with the user, and with their erasure.

### The list shows them, and "others" ends the rest

- `GET /auth/tokens` answers each token's `method`, `agent`, `ip`, and
  `device_id` (null where not recorded).
- `POST /auth/logout` with `{"others": true}` ends every other login of
  the user, and keeps the one making the request. Personal tokens and
  certificates are not logins, and stay.

### A hook decides who manages logins

Auth gains a hook point, `on_manage_logins`. Its checks run before:

- listing the user's tokens;
- ending a token that is not the request's own login;
- logging out `everywhere` or `others`.

A check raises to refuse. With no check registered, nothing changes.
Logging out of the request's own login is never checked: a user can
always sign out of the browser in front of them.

### In the phone-as-identity modes, only the phone manages logins

With `CIBA.APPROVE_LOGINS` set (`True` or `"always"`), the ciba app
registers a check: the request must be signed by a bound device of the
user (the devices app's request signature, as the phone's other routes
are), else `403 phone_required`.

The phone calls the same routes as any client, with its signature
headers. No second set of routes is needed.

## Consequences

- **The user sees "Chrome on Windows, from 41.x.x.x, by QR, last used two
  minutes ago",** and ends it from the phone: one, all but the phone, or
  all.
- **A stolen web session cannot hide.** In the phone modes it cannot end
  the phone's login, and the phone sees and ends it.
- **Projects must migrate:** the tokens table gains four nullable
  columns (`makemigrations`, then `migrate`).
- **More personal data is stored:** an address and an agent per login,
  for the login's life. The privacy export and erasure cover them with
  the user's tokens.
- **A web page in the phone modes cannot list logins,** its personal
  tokens included: the phone shows them. Creating a personal token is
  unchanged.
