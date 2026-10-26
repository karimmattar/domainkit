# 0087. Count proven devices apart, and bound scoped credentials by route

- Status: Accepted; amended 2026-10-26 (at the end)
- Date: 2026-10-26
- Amends: [0017](0017-deny-by-default-and-admin-api.md), [0068](0068-count-failed-attempts-and-hold-back-only-guesses.md)

## Context

Two promises of the auth app did not hold, and neither can change again
without breaking projects.

**Wrong passwords keep users out.** `LOGIN_RATE` counts wrong passwords per
account, from any address, and per address, for any account, before the
password is checked ([0068](0068-count-failed-attempts-and-hold-back-only-guesses.md)).
That bounds guessing one account from many addresses at `LOGIN_RATE`, and
it lets anyone keep a known username out, and one person behind an
office's address keep the office out (SEC-r1-2). WP-9 of the 0.9.0 audit
asked for a per-account lockout; 0068's amendment asked that the user get
in "by a way a stranger's failures cannot use up, without checking more
guesses per account". Four designs were measured against both attacks,
counting with domainkit's `MemoryBackend` on a simulated clock for an hour, `LOGIN_RATE = "10/minute"`; the attacker sends one wrong
password a second from each of 200 addresses; the victim logs in with the
right password once a minute from their own address; in the office attack,
one person at the victim's address sends one wrong password a second for
other usernames.

| Design | Guesses checked on the account, an hour (a day) | Victim's logins that work, under attack | Under the office attack |
| --- | --- | --- | --- |
| 0.9: per address and per account | 609 (14,616) | 0 of 60 | 0 of 60 |
| A: per (account, address), the account's count only slows | 121,800 (2,923,200) | 60 of 60 | 0 of 60 |
| B: a stored lockout, 10 failures lock for 15 minutes, a mailed unlock | 40 (960) | 0 of 60 | 0 of 60 |
| C: device proofs, counted per device | 609 (14,616) | 60 of 60 | 60 of 60 |

A slowing account count (A) lets every address guess at its own rate: two
hundred addresses check two hundred times as many passwords. A stored
lockout (B) checks fewest guesses, and keeps the user out for as long as
the attacker cares: ten wrong passwords lock the account again within a
second of every unlock, and the unlock costs the user a mail each time.
Device proofs (C) keep 0.9's bound on guessing and let the user in from
any client that logged in before, office attack included.

**Scoped credentials pass authentication-only routes.** A token limited to
scopes was bounded only by rules that check permissions; `Require()`,
`IsAuthenticated`, and `AuthenticatedUser` let it do anything its user may
(AUTH-01: a token scoped to `shop.view_product` wrote `POST /notes`). Two
places could check it: each authentication-only rule, or the route. Per
rule breaks the common shape of a router guarded by `Require()` whose
routes add `Require(HasPermissions(...))`, and of routes that take
`AuthenticatedUser` beside a permission rule: the authentication-only part
runs first and would refuse a token the permission rule allows.

## Decision

- **Device proofs.** A login that works, its second factor included
  (`/login`, `/session`, `/mfa/login`, `/session/mfa`), sets a cookie,
  `__Host-dk_device`, HttpOnly, `SameSite=Lax`, holding a random device id
  signed with `SECRET_KEY` under a salt that names the account (the
  digest of its normalized username). The device is stored as one of the
  user's tokens (kind `device`, by the id's SHA-256), for
  `AUTH.DEVICE_TTL` (30 days; an hour to 400; `None` turns proofs off), 20
  at most per user, the oldest ending first: whatever ends every token of
  the user (a password change or reset, logging out everywhere, an admin's
  change, deletion) ends its devices, and a proof of a device that ended
  counts as none. No migration: the tokens table holds them.
- **Counts of proven attempts.** A login that sends a live proof counts
  against `LOGIN_RATE` under the device's key, and against a ceiling of
  the account's proven attempts, twice `LOGIN_RATE`; not the account's
  own count nor the address's. Strangers cannot reach either. A first
  design counted the device alone: a security review of the merged 0.10
  showed that proofs minted while a password was known (six of them)
  checked 12 guesses after the password changed, at a `LOGIN_RATE` of 2.
  The ceiling was chosen by the same simulation, with an attacker holding
  D valid proofs (copied cookies) beside the 200 addresses:

  | Ceiling | D = 0: guesses an hour, victim's logins | D = 1 | D = 6 |
  | --- | --- | --- | --- |
  | none | 609, 60 of 60 | 1,218, 60 of 60 | 4,263, 60 of 60 |
  | 1x | 609, 60 of 60 | 1,218, 0 of 60 | 1,218, 0 of 60 |
  | 2x | 609, 60 of 60 | 1,218, 60 of 60 | 1,828, 0 of 60 |
  | 5x | 609, 60 of 60 | 1,218, 60 of 60 | 3,658, 0 of 60 |

  Twice `LOGIN_RATE` is the least that one copied proof cannot fill alone
  to keep the user's devices out. Holding two or more live proofs needs
  the password and the second factor, or the user's cookies, and a
  password change ends them all.
- **No stored lockout.** The account's count already bounds guessing, and
  a lock would hand attackers the user's account.
- **Scopes by route.** A route reached with credentials that carry scopes
  must declare a rule that checks scopes, at any depth of its dependencies
  or its routers' (`BasePermission.bounds_scopes`: `HasScope`, the
  permission rules, `IsUnscoped`, `IsAdminUser`, `IsSuperuser`, and the
  new `AllowScoped`; `&` checks if either side does, `|` if both do, `~`
  never). It is decided once a request, wherever the principal is
  first read: `Require`, `CurrentUser`, `AuthenticatedUser`, and
  `current_user`, which every provider's dependency calls, `Public` routes
  included (`check_scopes_bounded`; the review of the merged 0.10 found
  `CurrentUser` routes skipped). The route is the one FastAPI serves,
  from its effective route context, so routers' rules count, nested or
  flattened. `HasObjectPermissions` checks the scopes on the request too,
  and `ModelPermissions` asks scoped credentials for `view` on `OPTIONS`.
- **One minor of notice.** `SCOPED_CREDENTIALS = "warn"` (0.10's default)
  serves such requests as before, with a `DomainkitDeprecationWarning`
  for tests and a throttled security log line for deployments; `"refuse"`
  answers 403. 0.11 makes `"refuse"` the default.

## Consequences

- A user is kept out by strangers only on a client that never logged in
  (a new browser, a script without a cookie jar): as in 0.9, and a password
  reset, which needs their mail, ends it. Guessing an account stays
  bounded: `LOGIN_RATE` without proofs, plus twice that with them, however
  many proofs one holds.
- A successful login writes or renews one device row; the user's tokens
  table holds up to 20 more rows per user, which `GET /auth/tokens` does
  not list.
- Every login response sets one more cookie. HTTP clients without a cookie
  jar get 0.9's behaviour.
- Projects with routes that serve scoped tokens on authentication alone
  see the warning in their tests, and must name a scope, a permission, or
  `AllowScoped()` before 0.11; a route that reads `CurrentUser` and
  decides for itself is held too. A principal read straight from
  `scope["user"]` is not.
- A request with scoped credentials walks its route's dependencies once;
  requests without scopes pay nothing.

## Amendment (2026-10-26, 0.11): refused by default

`SCOPED_CREDENTIALS` defaults to `"refuse"`, as planned (AUTH-01): a
route reached with credentials limited to scopes, and declaring no rule
that checks scopes, answers 403. `"warn"` stays, as the way to keep 0.10's
answer while a project adds the routes' rules: it serves such requests
with the throttled line on the security logger, and no longer warns with
`DomainkitDeprecationWarning`. That warning announced a removal; `"warn"`
is a choice a project makes, not one this release plans to take away, and
a deprecation warning without a removal behind it would teach projects to
ignore the category. Nothing else changes: the rules that check scopes,
and where the decision is made, are as above.
