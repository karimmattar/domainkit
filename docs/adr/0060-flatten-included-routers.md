# 0060. Flatten included routers when the application is built

- Status: Accepted
- Date: 2026-10-21

## Context

FastAPI 0.141.1, the release domainkit pins, keeps each `include_router`
call as one entry of the including router's routes (`_IncludedRouter`),
and works out what the router gives its routes (the full path,
dependencies, tags, responses, response class) when a request arrives.
FastAPI 0.158.2, the newest today, still does. For each level of
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
  - a router with frontend routes (`APIRouter.frontend`), which FastAPI
    matches after every other route;
  - a router with a route whose served form FastAPI does not give;
  - every router, on a FastAPI without `_IncludedRouter` and its
    `effective_route_contexts`: there, flattening does nothing.
- `FLATTEN_ROUTERS = False` keeps FastAPI's nesting, for a FastAPI release
  or a project's route class that needs it, without a code change.
- The tests build every application twice, nested and flattened, on
  FastAPI 0.141.1 and 0.158.2, and require the same OpenAPI document byte
  for byte (the `init` project, the auth, admin, staff, and identity
  routers, and a router tree with every option), the same responses byte
  for byte (statuses, headers in order, bodies; 404, 405, 422, redirects,
  WebSockets), the same access rules, the same `url_path_for`, and the
  routers' lifespans and startup hooks still run.

## Consequences

- Each route is matched once per request, at the top, and its handler is
  built once. In the template's routes, a request with no middleware makes
  434 Python calls instead of 1,638; with the template's eleven
  middleware, 711 instead of 2,019 (with the other changes of the
  performance branch to the HTTP stack).
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
- On FastAPI 0.158, `iter_route_contexts` readers that ask a nested
  WebSocket's context for its `starlette_route` get nothing, so domainkit's
  checks see the router's own route: its path without prefixes, and
  without the rules of the `include_router` calls. Flattened routes do not
  have that problem; routers left nested still do, until those checks read
  the context itself.
- What would change this: FastAPI serving included routes flat itself, or
  giving a public way to read what it works out for them. Then
  `flatten_routes` does nothing, or reads that instead.
