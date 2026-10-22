"""Serve the routes of included routers from the top of the application.

FastAPI keeps each ``include_router`` call as one entry of the including
router, and works out a route's full path, dependencies, tags, and
responses when a request arrives: a route two routers deep is matched at
each level, and its handler is built again for every request. That costs
about 12 to 18 µs a request per level of nesting.

``flatten_routes`` puts the routes FastAPI works out in place of the
routers included in a router, once, so each route is matched once, at
the top, and its handler is built once. ``get_asgi_application`` flattens
``ROOT_ROUTER`` when it builds the application (ADR 0060).

A flattened route is served exactly as before: with its full path, the
dependencies (access rules among them), tags, responses, response class,
callbacks, and OpenAPI settings of every router it is included through,
its own route class, and ``app.dependency_overrides``. Mounted
applications stay mounts. Lifespans and startup hooks of included routers
were already merged into the including router by ``include_router``.

What is left included, as FastAPI built it:

- a router that serves requests in its own way (``serves_itself``): its
  class, or the router itself, has its own ``matches``, ``handle``, or any
  other method of ``APIRouter`` or Starlette's ``Router`` but those that
  declare routes. FastAPI asks a nested router to match and handle each
  request, so a router that gates by IP address or host, or sets a
  tenant's context, there does it; once flattened, it would never be
  asked. It stays one entry, with the prefixes and options of the routers
  above it, where its routes were; the routers around it, above it or
  beside it, are still flattened;
- a router with frontend routes (``APIRouter.frontend``), which FastAPI
  matches after every other route;
- every router, on a FastAPI that no longer includes routers this way.

``app.routes`` then lists the routes themselves, with their full paths,
where it listed the included routers; ``request.scope["route"]`` is the
flattened route, whose ``path`` is the full path. Routes added to an
included router after the application is built are not served: finish
a router before it is included.
"""

import copy
import dataclasses
from typing import Any, Final

from fastapi import APIRouter
from fastapi import routing as fastapi_routing
from fastapi.routing import APIRoute
from starlette.routing import BaseRoute, Router

_INCLUDED: Final[type[BaseRoute] | None] = getattr(
    fastapi_routing, "_IncludedRouter", None
)
"""What ``include_router`` adds to a router's routes, on a FastAPI that nests."""
DECLARING: Final = frozenset(
    {
        "__init__",
        "add_api_route",
        "add_api_websocket_route",
        "add_event_handler",
        "add_route",
        "add_websocket_route",
        "api_route",
        "delete",
        "frontend",
        "get",
        "head",
        "host",
        "include_router",
        "mount",
        "on_event",
        "options",
        "patch",
        "post",
        "put",
        "route",
        "trace",
        "websocket",
        "websocket_route",
    }
)
"""Methods of ``APIRouter`` and ``Router`` that only declare routes and hooks.

They run before the application is built, and what they declare is what
flattening reads: a router may have its own. Any other method may run
while a request is served.
"""


def nests_routers() -> bool:
    """Whether this FastAPI keeps included routers nested, as ``flatten_routes`` reads.

    False on a FastAPI that copies routes when they are included (as
    FastAPI did before 0.130) or that keeps them some other way: then
    ``flatten_routes`` leaves every route as it is.
    """
    return _INCLUDED is not None and all(
        callable(getattr(_INCLUDED, name, None))
        for name in ("effective_candidates", "effective_low_priority_routes")
    )


def serving_methods() -> frozenset[str]:
    """The methods of ``APIRouter`` and ``Router`` that may run for a request.

    Every method (and property) the two classes have, but ``DECLARING``: read
    from the FastAPI and Starlette installed, so a method a later release
    adds is one of them without a change here.
    """
    names = {
        name
        for base in (APIRouter, Router)
        for name, value in vars(base).items()
        if callable(value) or isinstance(value, property | classmethod)
    }
    return frozenset(names - DECLARING)


def serves_itself(router: APIRouter) -> bool:
    """Whether ``router`` has its own version of a method that serves requests.

    True when its class, a class between it and ``APIRouter``, or the
    router itself defines one of ``serving_methods()``. FastAPI calls a
    nested router's ``matches`` and ``handle`` for each request (an IP
    gate, a host rule, or a tenant's context may live there), and could
    call the others; a flattened router is never asked. A router that only
    has its own ``__init__`` or ways of declaring routes does not count.
    """
    serving = serving_methods()
    own = set(vars(router))
    for base in type(router).__mro__:
        if base is APIRouter:
            break
        own.update(vars(base))
    return not own.isdisjoint(serving)


def flatten_routes(router: APIRouter) -> int:
    """Put the routes of each router included in ``router`` in its place.

    Routes keep their order, so a request matches the route it matched
    before. A router that serves itself (``serves_itself``) stays
    included, where its routes were. Returns how many of the routers
    ``router`` includes were replaced.
    """
    if not nests_routers():
        return 0
    flattened = 0
    routes: list[BaseRoute] = []
    for route in router.routes:
        served = _served(route) if isinstance(route, _INCLUDED) else None  # type: ignore[arg-type]
        if served is None:
            routes.append(route)
        else:
            routes.extend(served)
            flattened += 1
    router.routes[:] = routes
    return flattened


def _served(included: Any) -> list[BaseRoute] | None:  # noqa: ANN401 - FastAPI's private type.
    """The routes ``included`` serves, in order; None to leave it included.

    A router included below it that is left included too stays one entry,
    in its place, with the prefixes and options of every router above it
    (FastAPI's own branch for it, as nesting serves it).
    """
    if (
        serves_itself(included.original_router)
        or included.effective_low_priority_routes()
    ):
        return None
    routes: list[BaseRoute] = []
    for candidate in included.effective_candidates():
        if isinstance(candidate, _INCLUDED):  # type: ignore[arg-type]
            served = _served(candidate)
            routes.extend([candidate] if served is None else served)
            continue
        route = _route(candidate)
        if route is None:
            return None
        routes.append(route)
    return routes


def _route(context: Any) -> BaseRoute | None:  # noqa: ANN401 - FastAPI's private type.
    """The route FastAPI serves for ``context``; None if it cannot tell.

    A path operation is a copy of the router's route, so it keeps its
    class, with the state FastAPI worked out for it (full path,
    dependencies, tags, responses, ...) and a handler built from that
    state, as FastAPI builds it for each request. Any other route (a
    WebSocket, a Starlette route, a mount) is the route FastAPI already
    made for it and serves.
    """
    original = context.original_route
    if isinstance(original, APIRoute) and dataclasses.is_dataclass(context):
        route = copy.copy(original)
        state = vars(route)
        for field in dataclasses.fields(context):
            if field.name in state:
                state[field.name] = getattr(context, field.name)
        route.app = fastapi_routing.request_response(route.get_route_handler())
        return route
    served = getattr(context, "starlette_route", None)
    return served if isinstance(served, BaseRoute) else None
