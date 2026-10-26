# 0087. Count proven devices apart, and bound scoped credentials by route

- Status: Accepted
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

- **Device proofs.** A login that works (`/login`, `/session`) sets a
  cookie, `__Host-dk_device`, HttpOnly, `SameSite=Lax`, holding a random
  device id signed with `SECRET_KEY` under a salt that names the account
  (the digest of its normalized username), for `AUTH.DEVICE_TTL` (30 days;
  an hour to 400; `None` turns proofs off). A login to that account that
  sends a valid proof counts against `LOGIN_RATE` under the device's key
  alone, not the account's nor the address's. Each login that works
  renews it. Nothing is stored: the counts stay in `RATE_LIMIT_BACKEND`,
  and no table or migration is added. No stored lockout is added: the
  account's count already bounds guessing, and a lock would hand
  attackers the user's account.
- **Scopes by route.** A route reached with credentials that carry scopes
  must declare a rule that checks scopes, at any depth of its dependencies
  or its routers' (`BasePermission.bounds_scopes`: `HasScope`, the
  permission rules, `IsUnscoped`, `IsAdminUser`, `IsSuperuser`, and the
  new `AllowScoped`; `&` checks if either side does, `|` if both do, `~`
  never). `Require` checks it once its rules allow, and so does
  `AuthenticatedUser` (`check_scopes_bounded`). The route is the one
  FastAPI serves, from its effective route context, so routers' rules
  count, nested or flattened.
- **One minor of notice.** `SCOPED_CREDENTIALS = "warn"` (0.10's default)
  serves such requests as before, with a `DomainkitDeprecationWarning`
  for tests and a throttled security log line for deployments; `"refuse"`
  answers 403. 0.11 makes `"refuse"` the default.

## Consequences

- A user is kept out by strangers only on a client that never logged in
  (a new browser, a script without a cookie jar): as in 0.9, and a password
  reset, which needs their mail, ends it. Guessing stays bounded by the
  account's count; a stolen proof adds one device's count for one account.
- Every login response sets one more cookie. HTTP clients without a cookie
  jar get 0.9's behaviour.
- Projects with routes that serve scoped tokens on authentication alone
  see the warning in their tests, and must name a scope, a permission, or
  `AllowScoped()` before 0.11. A route that reads `CurrentUser` and decides
  for itself is not checked.
- A request with scoped credentials walks its route's dependencies once
  per `Require`; requests without scopes pay nothing.
