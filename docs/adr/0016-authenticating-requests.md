# 0016. Authenticating requests with tokens, and route rules

- Status: Accepted
- Date: 2026-10-15

## Context

[0015](0015-auth-and-content-types.md) gave projects users, groups, and
permissions, but nothing told the framework who sent a request. Rate
limits, idempotency keys, and the audit trail were meant to be per actor,
yet no one set the actor: they all fell back to the client address, so
users behind one address shared a rate limit, and one user could be
replayed another's stored response (the audit's M3). FastAPI's usual
login dependencies run inside the route, after those middlewares, so they
could not fix it.

Clients are API consumers (mobile and single-page apps, scripts, other
services), not browsers with sessions. Some projects want tokens they can
revoke at once; others want signed tokens that other services can check
without the database.

## Decision

- `AuthenticationMiddleware` authenticates every request before the rate
  limiter and idempotency, sets `request.user` and `request.auth`, and
  makes the user's id the request's actor. Startup refuses an order that
  would let them read the actor first.
- Authentication classes, as in Django REST framework
  (`AUTH.AUTHENTICATION_CLASSES`), are tried in order. A class returns
  None for credentials that are not its own and raises for its own that
  do not hold: those get a 401 even on public routes, so a bad token is
  never mistaken for anonymity. The first class also issues login tokens.
- Opaque tokens are the default (`TokenAuthentication`): a prefix and 256
  random bits, stored as a SHA-256 digest and looked up on every request,
  so logging out and password changes take effect at once.
- JSON Web Tokens are optional (`JWTAuthentication`, the `jwt` extra):
  short-lived access tokens checked with one configured algorithm, and
  opaque refresh tokens used once each. A refresh token used twice ends
  its whole login. Each access token names its login, and a request
  checks the login still exists, because the user is loaded anyway: logout
  is immediate here too, while other services can still verify access
  tokens with the public key alone.
- The token table has no foreign key to the user table, so it stays the
  same whatever user model a project swaps in.
- Routes say who may call them with rules modelled on DRF's permission
  classes (`IsAuthenticated`, `HasPermissions`, `ModelPermissions`, and
  their `&`, `|`, `~`), through a FastAPI dependency, `Require`. Anonymous
  callers who are refused get 401; users get 403.
- The login endpoint is throttled per client address and per username,
  and refused logins are audited without the username.
- Without an actor, idempotency keys are also scoped by a hash of the
  request's credentials, for projects that authenticate on their own.

## Consequences

Rate limits, idempotency, logs, and the audit trail are per user once the
middleware is installed. Every authenticated request costs a lookup of the
token (or of the JWT's login) and of the user with its grants. Access
tokens cannot be scoped to some permissions yet. Generated routes stay
open until projects add rules; making them deny by default, and an admin
API, follow next.
