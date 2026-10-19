# 0021. Check mounted applications and Starlette routes too

- Status: Accepted
- Date: 2026-10-19

## Context

[0017](0017-deny-by-default-and-admin-api.md) made routes deny by default:
each must declare a rule, or `Public`, as a FastAPI dependency. Its walk
skipped what takes no dependencies: Starlette's own `Route` and
`WebSocketRoute`, and mounted applications. 0017 accepted that as a
consequence, but nothing said so to the project. A FastAPI application
mounted under the main one served all of its routes to anyone, rules or
not, and neither `check` nor startup listed them. A mounted `StaticFiles`
was public without anyone saying it should be.

The options were to keep skipping them and warn in the documentation; to
list public paths in a setting, which drifts away from the code that
mounts them and must repeat every prefix; to refuse mounts altogether; or
to mark what is mounted, where it is mounted, as `Public` marks a route.

## Decision

- Applications mounted with routes of their own (a `FastAPI`, a
  `Starlette`, a `Mount` of routes, a `Host`) are walked route by route,
  with the mount's path as a prefix. Their routes declare rules as any
  other: the main application's `AuthenticationMiddleware` has already
  set the user when they run.
- What takes no dependencies (an ASGI application without routes, such as
  `StaticFiles`, and Starlette's `Route` and `WebSocketRoute`) is listed,
  as `MOUNT /static` or `GET HEAD /raw`, with its own advice: serve it
  from a FastAPI route with `Require(...)`, or wrap it in `mark_public`.
- `mark_public(served)` sets an attribute on the application or endpoint
  and returns it unchanged, so the declaration sits where the mount is
  made, and nothing about how it is served changes.
- FastAPI's documentation routes (`/docs`, `/redoc`, `/openapi.json`) are
  not checked, as the framework's own routes are not: `API_DOCS` turns
  them on, and the deployment checks warn about it.

This replaces the consequence of 0017 that routes mounted outside
FastAPI's routers are not checked; the rest of 0017 stands.

## Consequences

Every way of serving a path now declares who may call it, and a forgotten
one fails `check` and deployment. Applications that mount files or other
ASGI applications must mark or guard them before deploying the release
that brings this in; it ships as a security fix, since nothing can tell
which mounts were meant to be public. A mounted FastAPI application
enforces its rules, but answers refusals with its own exception handlers,
not domainkit's error body. Objects that take no attributes (a bound
method, a class with `__slots__`) cannot be marked; wrap them in a
function first.
