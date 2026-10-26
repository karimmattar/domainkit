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

``RouteIndex`` then serves the top-level routes by their first path
segment (PERF-05): a request tries only the routes that can match a path
starting with its segment, in their order, instead of every route ahead
of its own. It answers what trying them all answers.
"""

import copy
import dataclasses
from collections.abc import Callable, Iterable, Sequence
from typing import Any, Final, Self, SupportsIndex

from fastapi import APIRouter
from fastapi import routing as fastapi_routing
from fastapi.routing import APIRoute, APIWebSocketRoute
from starlette._utils import get_route_path
from starlette.routing import BaseRoute, Match, Mount, Route, Router, WebSocketRoute
from starlette.types import Receive, Scope, Send

__all__ = [
    "RouteIndex",
    "flatten_routes",
    "index_routes",
    "serves_itself",
]

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


_SELECTED: Final[Callable[..., None] | None] = getattr(
    fastapi_routing, "_route_selected", None
)
"""FastAPI's telemetry hook for the route a request matched (``APIRouter.app``)."""
INDEXED_MATCHING: Final = frozenset(
    {
        Route.matches,
        WebSocketRoute.matches,
        Mount.matches,
        APIRoute.matches,
        APIWebSocketRoute.matches,
    }
)
"""The ``matches`` methods that match a path by its template's regular expression.

A route with one of them, and a template whose first segment is literal,
matches no path that starts with another segment: the index leaves it out
of such a request's candidates. Any other route (a host, a router left
included, a class with its own ``matches``) is a candidate of every
request.
"""


def first_segment(route: BaseRoute) -> str | None:
    """The literal first segment of every path ``route`` matches; None: any.

    ``/items/{id}`` gives ``items``, ``/`` gives ``""``; ``/{name}``,
    ``/items{suffix}``, a mount at the root, a host, or a route whose class
    matches in its own way give None.
    """
    if "matches" in vars(route) or type(route).matches not in INDEXED_MATCHING:
        return None
    path = getattr(route, "path", None)
    if not isinstance(path, str) or not path.startswith("/"):
        return None
    segment = path[1:].partition("/")[0]
    return None if "{" in segment else segment


class Routes(list[BaseRoute]):
    """A router's routes that count their changes (``version``).

    ``RouteIndex`` puts its router's routes in one, so a route added,
    removed, or replaced in place, by any of a list's methods, is noticed
    on the next request at the cost of comparing one number (SEC-10a-6 of
    the 0.10 review: the length alone missed a route replaced in place).
    """

    __slots__ = ("version",)

    def __init__(self, routes: Iterable[BaseRoute] = ()) -> None:
        """Hold ``routes``, at version 0."""
        super().__init__(routes)
        self.version = 0

    def _changed(self) -> None:
        """Count one change."""
        self.version += 1

    def __setitem__(self, index: Any, value: Any) -> None:  # noqa: ANN401 - list's.
        """Replace a route (or a slice of them)."""
        super().__setitem__(index, value)
        self._changed()

    def __delitem__(self, index: Any) -> None:  # noqa: ANN401 - list's.
        """Remove a route (or a slice of them)."""
        super().__delitem__(index)
        self._changed()

    def __iadd__(self, routes: Iterable[BaseRoute]) -> Self:  # type: ignore[override,misc]
        """Add ``routes`` at the end."""
        result = super().__iadd__(routes)
        self._changed()
        return result

    def __imul__(self, times: SupportsIndex) -> Self:
        """Repeat the routes ``times`` times."""
        result = super().__imul__(times)
        self._changed()
        return result

    def append(self, route: BaseRoute) -> None:
        """Add ``route`` at the end."""
        super().append(route)
        self._changed()

    def extend(self, routes: Iterable[BaseRoute]) -> None:
        """Add ``routes`` at the end."""
        super().extend(routes)
        self._changed()

    def insert(self, index: SupportsIndex, route: BaseRoute) -> None:
        """Add ``route`` before ``index``."""
        super().insert(index, route)
        self._changed()

    def pop(self, index: SupportsIndex = -1) -> BaseRoute:
        """Remove and return the route at ``index``."""
        route = super().pop(index)
        self._changed()
        return route

    def remove(self, route: BaseRoute) -> None:
        """Remove the first ``route``."""
        super().remove(route)
        self._changed()

    def clear(self) -> None:
        """Remove every route."""
        super().clear()
        self._changed()

    def sort(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401 - list's.
        """Reorder the routes."""
        super().sort(*args, **kwargs)
        self._changed()

    def reverse(self) -> None:
        """Reverse the routes' order."""
        super().reverse()
        self._changed()


@dataclasses.dataclass(frozen=True, slots=True)
class _Index:
    """The candidates of each first segment, worked out for one list of routes."""

    routes: Routes
    """The router's list, as it was indexed: a change to it is noticed."""
    version: int
    by_segment: dict[str, tuple[BaseRoute, ...]]
    anywhere: tuple[BaseRoute, ...]
    """The candidates of a segment no route names."""


def _index(routes: Routes) -> _Index:
    """Index ``routes``: each segment's candidates, in the routes' order."""
    keys = [first_segment(route) for route in routes]
    by_segment = {
        segment: tuple(
            route
            for route, key in zip(routes, keys, strict=True)
            if key in (None, segment)
        )
        for segment in {key for key in keys if key is not None}
    }
    anywhere = tuple(
        route for route, key in zip(routes, keys, strict=True) if key is None
    )
    return _Index(routes, routes.version, by_segment, anywhere)


class RouteIndex:
    """Serves a router's requests trying only the routes their path can reach.

    The ASGI application in front of a router's own (``index_routes``). A
    request tries, in order, the routes whose template starts with its
    path's first segment and those that may match any path: the routes
    left out could only answer "no match". The first full match is served,
    or else the first partial one (a known path, another method), as
    ``APIRouter.app`` does; when none matches, the router's own ``app``
    answers (redirects, frontend routes, 404). Lifespan events go to the
    router. The router's routes become ``Routes``: one added, removed, or
    replaced later, or a new list given to the router, is indexed on the
    next request.
    """

    __slots__ = ("_index", "router")

    def __init__(self, router: APIRouter) -> None:
        """Index ``router``'s routes as they are."""
        self.router = router
        self._index = _index(self._routes())

    def _routes(self) -> Routes:
        """The router's routes, made ``Routes`` if they are not yet."""
        routes = self.router.routes
        if not isinstance(routes, Routes):
            routes = self.router.routes = Routes(routes)
        return routes

    def candidates(self, scope: Scope) -> Sequence[BaseRoute]:
        """The routes a request with ``scope`` may match, in order."""
        index = self._index
        routes = self.router.routes
        if routes is not index.routes or index.routes.version != index.version:
            index = self._index = _index(self._routes())
        path = get_route_path(scope)
        if not path.startswith("/"):
            return index.anywhere
        return index.by_segment.get(path[1:].partition("/")[0], index.anywhere)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve the first route that matches, or let the router answer."""
        router = self.router
        if scope["type"] == "lifespan":
            await router.app(scope, receive, send)
            return
        if "router" not in scope:
            scope["router"] = router
        partial: tuple[BaseRoute, Scope] | None = None
        for route in self.candidates(scope):
            match, child_scope = route.matches(scope)
            if match == Match.FULL:
                await _serve(route, child_scope, scope, receive, send)
                return
            if match == Match.PARTIAL and partial is None:
                partial = (route, child_scope)
        if partial is not None:
            await _serve(*partial, scope, receive, send)
        else:
            await router.app(scope, receive, send)


async def _serve(
    route: BaseRoute, child_scope: Scope, scope: Scope, receive: Receive, send: Send
) -> None:
    """Serve ``route``, as ``APIRouter.app`` serves the route it matched."""
    scope.update(child_scope)
    if _SELECTED is not None:
        _SELECTED(
            scope=scope,
            path=getattr(route, "path_format", None),
            mount=isinstance(route, Mount),
        )
    await route.handle(scope, receive, send)


def index_routes(router: APIRouter) -> bool:
    """Serve ``router``'s requests through a ``RouteIndex``; whether it does.

    Only a router that serves requests with ``APIRouter.app`` itself, with
    no middleware of its own and FastAPI's telemetry hook to call: any
    other is left as it is.
    """
    if (
        type(router).app is not APIRouter.app
        or "app" in vars(router)
        or router.middleware_stack != router.app
        or _SELECTED is None
    ):
        return False
    router.middleware_stack = RouteIndex(router)
    return True
