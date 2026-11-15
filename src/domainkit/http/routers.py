"""Mount part of a router, with dependencies of your own on some of its routes.

The framework's routers are module-level ``APIRouter``s
(``domainkit.contrib.auth.api.router``), and ``include_router`` mounts
every route of one, as it is declared. ``select_routes`` makes a new
router with copies of the routes a project wants, and adds dependencies
(rules, or checks of its own) to the routes it names. The router it
copies is left as it is (ADR 0145)::

    recent = StepUp(max_age=timedelta(minutes=5))
    router.include_router(
        select_routes(
            devices_router,
            without=["POST /devices/login"],
            dependencies={"POST /devices/enroll/code": [Depends(Require(recent))]},
        ),
        prefix="/auth",
    )

A route is named ``METHOD /path``, as the router declares it: its methods
in alphabetical order, separated by spaces, then its path. The path has
the prefixes of the routers it is included through, not the prefix the
copy is mounted under. A WebSocket route is ``WEBSOCKET /path``.
``route_names`` lists a router's names; ``open_routes`` names routes the
same way, with the prefix they are mounted under. A name that matches no
route is refused.

What is copied:

- FastAPI's routes and WebSocket routes, as the router serves them: with
  the prefix, dependencies, tags, responses, and other options of every
  router they are included through. The copies sit in one plain
  ``APIRouter``, in the router's order, with its lifespan.
- Starlette's own routes and mounts that the router declares itself, as
  they are. They take no dependencies.

A router that serves requests in its own way (a ``matches`` or ``handle``
of its own, such as an IP gate), or that has frontend routes, is not
copied in part: ``select_routes`` refuses it, and a router that includes
one.
"""

import copy
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from fastapi import APIRouter, params
from fastapi.datastructures import Default
from fastapi.routing import (
    APIRoute,
    APIWebSocketRoute,
    RouteContext,
    iter_route_contexts,
)
from starlette.routing import Host, Mount, Route, WebSocketRoute

from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.routing import has_frontend_routes, routers_within, serves_itself

__all__ = ["route_names", "select_routes"]


def route_names(router: APIRouter) -> list[str]:
    """Each route's name, ``METHOD /path``, as ``select_routes`` reads it, in order.

    ``route_names(auth_router)`` gives ``["POST /login", "POST /refresh",
    ...]``.
    """
    return [_name(context) for context in iter_route_contexts(router.routes)]


def select_routes(
    router: APIRouter,
    *,
    only: Iterable[str] | None = None,
    without: Iterable[str] = (),
    dependencies: Mapping[str, Sequence[params.Depends]] | None = None,
) -> APIRouter:
    """A copy of ``router`` with some of its routes, and dependencies on some.

    The routes are copied as the module says. A route named in
    ``dependencies`` runs those after its own, as the last of its
    ``dependencies=[...]``: after those of its routers and of the route
    itself, before its endpoint. OpenAPI shows them as it shows the
    route's other dependencies.

    Args:
        router: The router to copy; it is not changed.
        only: The names of the routes to copy; every route when None.
        without: The names of routes to leave out.
        dependencies: More dependencies of routes, by name:
            ``{"POST /login": [Depends(check)]}``.

    Raises:
        ImproperlyConfigured: A name matches no route of ``router``, a
            route left out or one that takes no dependencies is given
            some, a dependency is not ``Depends(...)``, or ``router``
            cannot be copied in part.
    """
    _refuse_whole_routers(router)
    contexts = list(iter_route_contexts(router.routes))
    names = [_name(context) for context in contexts]
    wanted = None if only is None else _names("only", only)
    unwanted = _names("without", without)
    added = {
        name: _dependencies(name, extra) for name, extra in (dependencies or {}).items()
    }
    if unknown := sorted(({*(wanted or ()), *unwanted, *added}) - set(names)):
        raise ImproperlyConfigured(
            f"select_routes: no route is named {', '.join(map(repr, unknown))}; "
            f"the router's routes are {', '.join(names) or 'none'}."
        )

    def kept(name: str) -> bool:
        """Whether the route ``name`` is copied."""
        return (wanted is None or name in wanted) and name not in unwanted

    if left_out := sorted(name for name in added if not kept(name)):
        raise ImproperlyConfigured(
            f"select_routes: dependencies are given to routes that are left "
            f"out: {', '.join(map(repr, left_out))}."
        )
    own = {id(route) for route in router.routes}
    chosen = [
        (context, name)
        for context, name in zip(contexts, names, strict=True)
        if kept(name)
    ]
    for context, name in chosen:
        _refuse_uncopied(context, name, own, name in added)
    selected = APIRouter(
        route_class=router.route_class,
        dependency_overrides_provider=router.dependency_overrides_provider,
        redirect_slashes=router.redirect_slashes,
        lifespan=router.lifespan_context,
    )
    for context, name in chosen:
        _copy(selected, context, added.get(name, []))
    return selected


def _name(context: RouteContext) -> str:
    """How ``select_routes`` names the route ``context`` serves."""
    route = context.original_route
    if isinstance(route, WebSocketRoute):
        return f"WEBSOCKET {context.path}"
    if isinstance(route, Route):
        return f"{' '.join(sorted(context.methods or ())) or 'ANY'} {context.path}"
    if isinstance(route, Host):
        return f"HOST {route.host}"
    if isinstance(route, Mount):
        return f"MOUNT {context.path}"
    return f"{type(route).__name__} {context.path or ''}".rstrip()


def _names(argument: str, names: Iterable[str]) -> set[str]:
    """``names`` as a set; one string, which reads as its letters, is refused."""
    if isinstance(names, str):
        raise ImproperlyConfigured(
            f"select_routes: {argument} takes a list of names, as in "
            f"{argument}=[{names!r}]."
        )
    return set(names)


def _dependencies(name: str, extra: Sequence[params.Depends]) -> list[params.Depends]:
    """``extra`` as a list, each checked to be ``Depends(...)``."""
    if isinstance(extra, params.Depends | str) or not isinstance(extra, Iterable):
        raise ImproperlyConfigured(
            f"select_routes: give {name!r} a list of dependencies, as in "
            f"{{{name!r}: [Depends(...)]}}."
        )
    found = list(extra)
    for each in found:
        if not isinstance(each, params.Depends):
            raise ImproperlyConfigured(
                f"select_routes: {each!r}, given to {name!r}, is not a "
                "dependency: give Depends(...), as in a route's dependencies, "
                "such as Depends(Require(rule))."
            )
    return found


def _refuse_whole_routers(router: APIRouter) -> None:
    """Refuse ``router`` if it, or a router it includes, cannot be copied in part."""
    for each in routers_within(router):
        what = (
            "serves requests in its own way (a matches or handle of its own)"
            if serves_itself(each)
            else "has frontend routes"
            if has_frontend_routes(each)
            else None
        )
        if what is not None:
            which = (
                "the router"
                if each is router
                else f"a router it includes ({type(each).__qualname__})"
            )
            raise ImproperlyConfigured(
                f"select_routes: {which} {what}, which a copy of its routes "
                "would leave out: include it whole."
            )


def _refuse_uncopied(
    context: RouteContext, name: str, own: set[int], depends: bool
) -> None:
    """Refuse a route ``select_routes`` cannot copy, or give dependencies to."""
    route = context.original_route
    if isinstance(route, APIRoute | APIWebSocketRoute):
        return
    if depends:
        raise ImproperlyConfigured(
            f"select_routes: {name!r} takes no dependencies (a "
            f"{type(route).__name__}): serve it from a FastAPI route."
        )
    if id(route) not in own:
        raise ImproperlyConfigured(
            f"select_routes: {name!r} is a {type(route).__name__} of a router "
            "the router includes, and only FastAPI's routes are copied from "
            "those: leave it out, or include that router whole."
        )


def _copy(
    selected: APIRouter, context: RouteContext, extra: list[params.Depends]
) -> None:
    """Add to ``selected`` a copy of the route ``context`` serves, with ``extra``.

    A FastAPI route is made again by its class, from what FastAPI works
    out for it, so its dependencies, body, and OpenAPI are worked out
    with ``extra`` among them. Any other route is copied as it is.
    """
    route = context.original_route
    served: Any = context  # Answers with the route's state, as it is served.
    if isinstance(route, APIWebSocketRoute):
        selected.add_api_websocket_route(
            served.path,
            served.endpoint,
            name=served.name,
            dependencies=[*served.dependencies, *extra],
        )
    elif isinstance(route, APIRoute):
        # A streamed route works out its item type from the endpoint's
        # return annotation only when no response model is given.
        streams = getattr(context, "stream_item_type", None) is not None
        selected.add_api_route(
            served.path,
            served.endpoint,
            response_model=Default(None) if streams else served.response_model,
            status_code=served.status_code,
            tags=list(served.tags),
            dependencies=[*served.dependencies, *extra],
            summary=served.summary,
            description=served.description,
            response_description=served.response_description,
            responses=dict(served.responses),
            deprecated=served.deprecated,
            methods=set(served.methods),
            operation_id=served.operation_id,
            response_model_include=served.response_model_include,
            response_model_exclude=served.response_model_exclude,
            response_model_by_alias=served.response_model_by_alias,
            response_model_exclude_unset=served.response_model_exclude_unset,
            response_model_exclude_defaults=served.response_model_exclude_defaults,
            response_model_exclude_none=served.response_model_exclude_none,
            include_in_schema=served.include_in_schema,
            response_class=served.response_class,
            name=served.name,
            route_class_override=type(route),
            callbacks=served.callbacks,
            openapi_extra=served.openapi_extra,
            generate_unique_id_function=served.generate_unique_id_function,
            strict_content_type=served.strict_content_type,
        )
    else:
        selected.routes.append(copy.copy(route))
