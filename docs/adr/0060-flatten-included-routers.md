# 0060. Flatten included routers when the application is built

- Status: Accepted; amended 2026-10-26 (at the end)
- Date: 2026-10-21

## Context

FastAPI 0.141.1, the release domainkit pins, keeps each `include_router`
call as one entry of the including router's routes (`_IncludedRouter`),
and works out what the router gives its routes (the full path,
dependencies, tags, responses, response class) when a request arrives.
FastAPI 0.143.1, which the performance branch upgrades to, and 0.158.2,
the newest today, still do. For each level of
nesting, a request is matched twice, once to choose the router and once
to serve it; FastAPI sets and restores keys in `scope["fastapi"]` for each
route it tries; and on 0.141.1 the served route's handler is built again
for every request.

The project template nests every route: `config/urls.py` includes the
health, auth, and admin routers, and `get_asgi_application` includes
`config/urls.py`. A request to a route listed after the auth and admin
routers pays for trying each of their routes through the nesting. The
performance re-audit (PERF-04, N5, FX-18) measured 12 to 18 µs per level
on one route; counted with cProfile, a request with no middleware made
1,638 Python calls in the template's routes, and 434 once flattened.

The options:

- Wait for FastAPI. Its newest release nests, and the re-audit measured
  0.147 slower than 0.141, not faster.
- Build the flat routes again with `add_api_route`, merging each router's
  options ourselves. That copies FastAPI's precedence rules (which response
  class, which unique-id function, how tags, responses, callbacks, and
  `deprecated` combine) into domainkit, where they would drift from
  FastAPI's, release by release.
- Ask FastAPI what it works out for each route (its effective route
  contexts), once, and serve that. FastAPI's own nesting serves the same
  state, so nothing is merged twice. The price is reading two private
  FastAPI names.

## Decision

- `get_asgi_application` flattens the routers it includes from
  `ROOT_ROUTER` with `domainkit.http.routing.flatten_routes`. Each
  included router is replaced, in place and in order, by the routes
  FastAPI works out for it, at every depth:
  - a path operation becomes a copy of the router's route, so it keeps
    its class, with the state FastAPI worked out for it, field for field,
    and a handler built once from that state;
  - any other route (a WebSocket, a Starlette route, a mount, a host) is
    the route FastAPI already builds, and serves, for it.
- Left included as FastAPI built them:
  - a router that serves requests in its own way
    (`domainkit.http.routing.serves_itself`): its class, a class between
    it and `APIRouter`, or the router itself has its own version of any
    method of `APIRouter` or Starlette's `Router` but those that only
    declare routes (`__init__`, `add_api_route`, `get`, `include_router`,
    `mount`, and the like: `DECLARING`). FastAPI asks a nested router's
    `matches` and `handle` for each request, and a project may keep an IP
    gate, a host rule, or a tenant's context there; flattened, it would
    never be asked. The methods are read from FastAPI's and Starlette's
    classes, not listed by hand, so one a later release adds counts too.
    Such a router stays one entry, in its place: FastAPI's own branch for
    it, with the prefixes, rules, and other options of every router above
    it. The routers above and beside it are flattened around it;
  - a router with frontend routes (`APIRouter.frontend`), which FastAPI
    matches after every other route;
  - a router with a route whose served form FastAPI does not give;
  - every router, on a FastAPI without `_IncludedRouter` and its
    `effective_candidates`: there, flattening does nothing.
- `FLATTEN_ROUTERS = False` keeps FastAPI's nesting, for a FastAPI release
  or a project's route class that needs it, without a code change.
- Rejected, for routers that serve themselves: leaving included the whole
  top-level router a project's router sits in. Exact too, but one gated
  router in `config/urls.py` would put every route of the application
  back to matching at each level.
- The tests build every application twice, nested and flattened, on
  FastAPI 0.141.1, 0.143.1, and 0.158.2, and require the same OpenAPI document byte
  for byte (the `init` project, the auth, admin, staff, and identity
  routers, and a router tree with every option), the same responses byte
  for byte (statuses, headers in order, bodies; 404, 405, 422, redirects,
  WebSockets), the same access rules, the same `url_path_for`, and the
  routers' lifespans and startup hooks still run. Routers that have their
  own `handle`, `matches`, `not_found`, `__call__`, or `app`, at the top
  or below an ordinary router, answer and refuse alike over HTTP and
  WebSocket.

## Consequences

- Each route is matched once per request, at the top, and its handler is
  built once. In the template's routes, a request with no middleware makes
  434 Python calls instead of 1,638; with the template's eleven
  middleware, 711 instead of 2,019 (with the other changes of the
  performance branch to the HTTP stack). On FastAPI 0.143.1: 500 instead
  of 1,674, and 777 instead of 2,055.
- What a project can see:
  - `app.routes` lists the routes themselves, with their full paths,
    where it listed the included routers;
  - `request.scope["route"]` is the flattened route, whose `path` is the
    full template; nesting gave the router's route, without the prefixes
    of the routers above it (instrumentation that labels by
    `scope["route"].path` reads full templates again);
  - `MetricsMiddleware` labels a Starlette route or a mount inside an
    included router as it labels one at the top, `unmatched` or the
    mounted application's route; nesting gave them an empty label;
  - a route class whose `get_route_handler` reads `self` sees the copy,
    with the full path.
- Routes added to an included router after the application is built are
  not served; nesting served them. domainkit's routers are complete when
  their modules are imported, and the admin site closes registration once
  its router is built. Routers included in the application after
  `get_asgi_application` stay nested and are served.
- domainkit reads two more private FastAPI names (`_IncludedRouter`,
  `_EffectiveRouteContext`). A FastAPI release that drops them turns
  flattening off rather than serving something else; one that changes
  their shape fails the tests that compare nested and flattened
  applications. The benchmarks in CI would show the cost of nesting
  coming back.
- A router that serves itself costs what nesting costs, for its own
  routes only, and its routes added after the build are still served.
  Below another router, its `matches` is asked once per request, as at
  the top, where nesting asked it once more for each router above it.
- What would change this: FastAPI serving included routes flat itself, or
  giving a public way to read what it works out for them. Then
  `flatten_routes` does nothing, or reads that instead.

## Amendment (2026-10-26, 0.10): try only the routes a request's first segment reaches

Flattened, a route was still matched after every route ahead of it, about
7 calls each: 125 calls of a light request behind the template's 23 routes,
and about 1,000 on the last of 150 (PERF-05 of the 0.9.0 audit).

- **`RouteIndex`** (`domainkit.http.routing`) serves the application's
  router: it indexes the top-level routes by the literal first segment of
  their templates, and a request tries, in order, the routes of its
  segment and those that may match any path (a parameter or converter in
  the first segment, a mount at the root, a host, a router left included,
  a route class with its own `matches`). The routes left out could only
  answer "no match", so the first full match, or else the first partial
  one, is what `APIRouter.app` serves, with FastAPI's telemetry hook; when
  none matches, the router's own `app` answers (redirects, frontend
  routes, 404). Lifespan events go to the router; a route added later is
  indexed on the next request.
- `get_asgi_application` installs it with flattening; `FLATTEN_ROUTERS =
  False` keeps FastAPI's own matching, index included. A router with its
  own `app`, or middleware of its own, is left as it is.
- One GET behind N routes, calls per request (and CPU), every route tried
  to the index: 10 routes, 409 to 329; 100, 1,129 to 329 (133 to 58 µs);
  1,000, 8,329 to 329 (757 to 59 µs).
- The tests compare the route each request selects, every route tried and
  indexed, for every route of eight test projects' routers and for random
  overlapping routes of every kind (10 to 400), over HTTP and WebSocket,
  under a root path too, and serve both over HTTP byte for byte.
- "Mount busy routers first" is no longer advice: order still decides
  between routes of one segment, as it always did.
