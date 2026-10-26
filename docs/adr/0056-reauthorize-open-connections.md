# 0056. Re-authorize open connections on a timer, and close them when that fails

- Status: Accepted; amended in place 2026-10-21; amended by [0058](0058-cache-principals-and-stamp-revocations.md)
- Date: 2026-10-20
- Amends: [0016](0016-authenticating-requests.md), [0017](0017-deny-by-default-and-admin-api.md)

## Context

`AuthenticationMiddleware` checks a WebSocket's credentials at the
handshake, and `Require` its route's rules. Both hold for the life of the
connection: a token revoked by logging out, an access token past its
expiry, a user deactivated, or a permission taken away keeps every socket
it opened, for hours. An event stream is the same.

Options considered:

- **Check on every message.** Exact, but a lookup (a token, a user with its
  groups and permissions) per message multiplies the database's work by the
  message rate, and a stream that only sends has no messages to check on.
- **Check on a timer.** One lookup per connection per interval, whatever
  the traffic. A revocation takes effect within the interval.
- **Push revocations.** The code that revokes tells the open connections.
  Exact and cheap, but every way a principal can change (a logout, an
  expiry, a group's permissions, a project's own backend) must remember to
  push, and across replicas it needs a broker.
- **Short lifetimes only.** Close every connection after a few minutes, so
  the handshake runs again. Simple, but it costs every client a reconnect,
  and still leaves the window open as long as the lifetime.
- **What to re-run.** Re-run the authentication classes against the
  handshake's headers, and the rules the route checked, or ask a backend
  only whether the user is still active. The latter misses revoked tokens
  and removed permissions.

## Decision

- Every `LIVE_REAUTH_INTERVAL` seconds (60 by default; between three
  quarters of it and all of it, at random, so connections opened together
  are not checked together), an open WebSocket or event stream is
  re-authorized:
  - `AuthenticationMiddleware` leaves a re-authentication in the scope. It
    runs the authentication classes again on the handshake's headers. The
    credentials must hold and name the same user; a connection that was
    anonymous stays anonymous. The user is then loaded afresh, with its
    groups and permissions.
  - `Require` records the rules it passed, and they run again, with that
    user.
- When either refuses, the connection is closed with 1008, "Not
  authorized.", whatever the reason. Why is logged as a security event,
  never told to the client. An event stream ends with that comment, and a
  browser that reconnects gets its 401 or 403 at the handshake.
- A check that raises, or takes longer than ten seconds, confirms nothing:
  the connection is closed with 1013, "Authorization not confirmed.", so a
  client tries again later. Authorization fails closed.
- A check is not cut off when its connection ends meanwhile: it is shielded
  and bounded by the same ten seconds, so no query is cancelled halfway.
  The bound cancels what the check awaits; blocking work an authenticator
  runs on a thread with `anyio.to_thread.run_sync` does not heed it, and
  its answer counts when it ends (`docs/live.md`). (Amended 2026-10-21.)
- `None` turns the checks off; `check` warns about that in staging and
  production.
- The interval, the setting's or a route's own (`LiveLimits`), is at most
  an hour, so no setting leaves a revoked token its connection for longer.
  (Amended 2026-10-21.)
- Live events (ADR 0057) are filtered with the user of the last check.

## Consequences

- A revoked token, an expired one, a deactivated user, or a removed
  permission closes the connection within the interval.
- The cost is one authentication per connection per interval, never per
  message: 1,000 connections at the default interval make about 17 checks
  a second, each costing what authenticating a request costs.
- JSON Web Tokens expire after `AUTH.JWT.ACCESS_TOKEN_TTL` (five minutes by
  default), and their connections close then: the client reconnects with a
  fresh token.
- A database outage closes live connections at their next check (1013), as
  it refuses new handshakes; clients back off and reconnect.
- The route's rules see the request's scope again later: a rule must not
  depend on anything but the connection and the user.
- Only `Require`'s rules run again. A check a handler makes itself at the
  start (`check_object_permissions` on an object it loaded) holds for the
  life of the connection; conditions that must keep holding belong in the
  route's rules.
