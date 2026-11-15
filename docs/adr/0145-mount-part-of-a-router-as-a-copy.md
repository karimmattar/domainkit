# 0145. Mount part of a router as a copy

- Status: Accepted
- Date: 2026-11-15

## Context

A project that runs an identity service on domainkit reported (A7) that
it could not mount the framework's routers selectively, nor give one of
their routes a rule of its own:

- The routers are module-level `APIRouter`s (`auth.api.router`,
  `devices.api.router`, ...). `include_router` mounts every route of one,
  with the dependencies it declares, and nothing in `http.routing`
  selects, leaves out, or adds to routes.
- So the project's urls module took the routers apart by path, with
  helpers of its own, and added a rule to a framework route with
  dependencies that read the JSON body.

Constraints:

- A module-level router is shared by every project and test that imports
  it: changing it in place changes it for all of them.
- FastAPI 0.143 keeps included routers nested and works out each route's
  full path and dependencies itself. A dependency added to a route after
  it is made is not served: FastAPI builds the route's dependencies, body
  and OpenAPI from `dependencies` when the route is made.
- A typo in a route's name must not mount every route, nor leave a rule
  out.

Options considered:

- **Name each framework route** (`auth.login`, `devices.enroll_code`) and
  mount by name: a second name for every route, to keep stable, and none
  for a project's own routers.
- **Methods on a router class of our own** (`router.select(...)`): every
  framework router would have to be that class, and a project's own
  `APIRouter`s would not have them.
- **A function that copies**, naming routes as `open_routes` already
  prints them.

## Decision

- `domainkit.http.routers.select_routes(router, only=None, without=(),
  dependencies=None)` returns a new `APIRouter` with copies of the routes
  of `router` that `only` names (all without it), but those `without`
  names. `dependencies` maps a route's name to `Depends(...)`s that the
  copy runs after its own, as the last of its `dependencies=[...]`.
  `route_names(router)` lists the names.
- A route is named `METHOD /path` as its router declares it: methods in
  alphabetical order, the path with the prefixes of the routers it is
  included through, without the prefix it is mounted under.
  `WEBSOCKET /path` for a WebSocket route. `open_routes` names routes
  the same way.
- A name that matches no route, dependencies on a route left out, and a
  dependency that is not `Depends(...)` raise `ImproperlyConfigured`.
- Each FastAPI route is made again by its own class, from the state
  FastAPI works out for it as served (`iter_route_contexts`), with the
  added dependencies. So its dependencies, body and OpenAPI are worked
  out as for any route, and the router copied is not changed. The copies
  sit in one plain `APIRouter`, with the router's lifespan.
- A router that serves requests in its own way, or has frontend routes,
  or includes such a router, is refused: a copy of its routes would leave
  that behaviour out. Starlette's own routes are copied as they are when
  the router declares them itself, and take no dependencies.

## Consequences

- A project mounts some routes of a framework router, and adds a rule or
  a check to one of them, in its urls module, without helpers of its own.
- The same works for a project's own routers and for `account_router()`.
- Names follow paths: a framework release that moves a route changes its
  name, and the project's call is refused at startup rather than mounting
  the wrong set.
- Copies are made when the urls module is imported, about a millisecond
  a route; requests pay nothing.
- A project that mounts a router and a copy of it has each route twice,
  and `check` refuses the clashing operation ids (`openapi.E001`).
