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
from starlette.routing import BaseRoute

_INCLUDED: Final[type | None] = getattr(fastapi_routing, "_IncludedRouter", None)
"""What ``include_router`` adds to a router's routes, on a FastAPI that nests."""


def nests_routers() -> bool:
    """Whether this FastAPI keeps included routers nested, as ``flatten_routes`` reads.

    False on a FastAPI that copies routes when they are included (as
    FastAPI did before 0.130) or that keeps them some other way: then
    ``flatten_routes`` leaves every route as it is.
    """
    return _INCLUDED is not None and all(
        callable(getattr(_INCLUDED, name, None))
        for name in ("effective_route_contexts", "effective_low_priority_routes")
    )


def flatten_routes(router: APIRouter) -> int:
    """Put the routes of each router included in ``router`` in its place.

    Routes keep their order, so a request matches the route it matched
    before. Returns how many included routers were replaced.
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
    """The routes ``included`` serves, in order; None to leave it included."""
    if included.effective_low_priority_routes():
        return None
    routes = []
    for context in included.effective_route_contexts():
        route = _route(context)
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
