"""Declare a route of a resource on FastAPI: its parameters, answers, and endpoint.

A route kind describes a route (``RouteSpec``) and how it answers
(``handle``); ``add_route`` gives FastAPI an endpoint whose signature names
what the route reads (the unit of work, the ids in the path, the page, the
body, the conditional headers, the dependencies of ``provide``), documents
the errors it may answer with, guards it with the resource's rules, and
hands each request's values to ``handle`` as a ``RouteContext``.
"""

import inspect
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Annotated, Any, Final, cast

from fastapi import Body, Depends, Header, Path, Request, status
from fastapi import Query as QueryParameters
from pydantic import BaseModel

from domainkit.ddd.uow import UnitOfWork
from domainkit.http.access import Require, Rule
from domainkit.http.errors import error_responses
from domainkit.http.pagination import PageRequest

from .context import (
    DEFAULT,
    MERGE_PATCH,
    PROVIDED,
    Handle,
    Operation,
    RouteContext,
    RouteSpec,
    UnitOfWorkFactory,
)
from .reach import check_parent, rules_of

if TYPE_CHECKING:
    from . import ResourceRouter

IF_NONE_MATCH: Final = "The ETag you hold: 304 if it is current."
OPTIONAL: Final = (
    "parent_uow",
    "body",
    "parameters",
    "page",
    "filters",
    "if_match",
    "if_none_match",
)
"""Parameters some routes take, which the context names alike."""


def add_route(router: "ResourceRouter", spec: RouteSpec, handle: Handle) -> None:
    """Add the route ``spec`` describes to ``router``, answered by ``handle``."""
    name = f"{router.name_prefix}{spec.name}"
    rules = rules_of(router, spec.action, spec.rule)
    router.add_api_route(
        spec.path,
        _endpoint(router, spec, handle, name),
        methods=[spec.method],
        name=name,
        summary=spec.summary,
        status_code=spec.status_code,
        response_model=(
            None if spec.returns is None or spec.returns is DEFAULT else spec.returns
        ),
        responses=_responses(router, spec, rules),
        dependencies=[Depends(Require(*rules))] if rules else None,
        openapi_extra=_extra(spec),
    )
    router.operations.append(
        Operation(
            name=name,
            method=spec.method,
            path=f"{router.prefix}{spec.path}",
            action=spec.action,
            rules=rules,
            summary=spec.summary,
            detail=spec.detail,
            body=spec.body,
            parameters=spec.asked,
        )
    )


def _endpoint(
    router: "ResourceRouter", spec: RouteSpec, handle: Handle, name: str
) -> Callable[..., Awaitable[object]]:
    """The endpoint FastAPI calls: it takes the parameters and answers by ``handle``.

    Its signature is made here, so FastAPI reads what to resolve from it,
    as from a function written by hand.
    """
    parent = router.parent
    parameters = _parameters(router, spec)
    taken = {each.name for each in parameters}
    ids = tuple(
        each
        for each in (router.lookup, parent.lookup if parent is not None else None)
        if each in taken
    )
    optional = tuple(each for each in OPTIONAL if each in taken)
    provided = tuple((f"{PROVIDED}{field}", field) for field in spec.provided)

    async def endpoint(**values: Any) -> object:  # noqa: ANN401 - the route's parameters.
        context = RouteContext(
            request=values["request"],
            uow=values["uow"],
            ids={each: values[each] for each in ids},
            provided={field: values[key] for key, field in provided},
            **{each: values[each] for each in optional},
        )
        if parent is not None:
            await check_parent(router, context)
        return await handle(context)

    endpoint.__name__ = endpoint.__qualname__ = name
    endpoint.__doc__ = spec.summary
    endpoint.__signature__ = inspect.Signature(parameters)  # type: ignore[attr-defined]
    return endpoint


def _parameters(router: "ResourceRouter", spec: RouteSpec) -> list[inspect.Parameter]:
    """What the endpoint takes, in order: what FastAPI resolves for the route."""
    parameters = [
        parameter("request", Request),
        parameter("uow", Annotated[object, Depends(router.unit_of_work)]),
    ]
    if router.parent is not None:
        parameters += [
            parameter(router.parent.lookup, Annotated[router.parent.id_type, Path()]),
            parameter(
                "parent_uow", Annotated[object, Depends(router.parent.unit_of_work)]
            ),
        ]
    if spec.detail:
        parameters.append(parameter(router.lookup, Annotated[router.id_type, Path()]))
    if spec.listing:
        parameters.append(
            parameter("page", Annotated[PageRequest, Depends(router.pagination)])
        )
        if router.filters is not None:
            parameters.append(
                parameter(
                    "filters", Annotated[object, Depends(router.filters_dependency)]
                )
            )
    if spec.reads:
        parameters.append(_header("if_none_match", "If-None-Match", IF_NONE_MATCH))
    if spec.detail and spec.writes:
        parameters.append(
            _header(
                "if_match",
                "If-Match",
                "Required: the ETag of the version you change; 412 if it changed since."
                if router.require_if_match
                else "The ETag of the version you change; 412 if it changed since.",
            )
        )
    if spec.body is not None:
        parameters.append(parameter("body", Annotated[spec.body, Body()]))
    if spec.asked is not None:
        parameters.append(
            parameter("parameters", Annotated[spec.asked, QueryParameters()])
        )
    parameters.extend(
        parameter(f"{PROVIDED}{field}", Annotated[object, Depends(dependency)])
        for field, dependency in spec.provided.items()
    )
    return parameters


def _header(name: str, alias: str, description: str) -> inspect.Parameter:
    """An optional header parameter of at most 4,096 characters."""
    return parameter(
        name,
        Annotated[
            str | None,
            Header(alias=alias, max_length=4096, description=description),
        ],
        None,
    )


def _responses(
    router: "ResourceRouter", spec: RouteSpec, rules: tuple[Rule, ...]
) -> dict[int | str, dict[str, Any]]:
    """The answers OpenAPI documents for the route, errors first."""
    codes = [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN] if rules else []
    if spec.detail:
        codes.append(status.HTTP_404_NOT_FOUND)
    if spec.writes:
        codes += [status.HTTP_409_CONFLICT, status.HTTP_422_UNPROCESSABLE_CONTENT]
    if spec.writes and spec.detail:
        codes.append(status.HTTP_412_PRECONDITION_FAILED)
        if router.require_if_match:
            codes.append(status.HTTP_428_PRECONDITION_REQUIRED)
    exporting = spec.listing and len(router.media_types) > 1
    if spec.listing or spec.asked is not None:
        codes.append(status.HTTP_422_UNPROCESSABLE_CONTENT)
    if exporting:
        codes.append(status.HTTP_406_NOT_ACCEPTABLE)
    responses: dict[int | str, dict[str, Any]] = dict(error_responses(*codes))
    if exporting:
        exports = router.media_types[1:]
        responses[status.HTTP_200_OK] = {
            "content": {media: {"schema": {"type": "string"}} for media in exports},
            "description": "A page as JSON; every match as "
            + " or ".join(exports)
            + f", by Accept, up to {router.max_export}.",
        }
    if spec.reads:
        responses[status.HTTP_304_NOT_MODIFIED] = {
            "description": "The ETag sent is still current."
        }
    return responses


def _extra(spec: RouteSpec) -> dict[str, Any] | None:
    """What OpenAPI says beyond FastAPI: ``PATCH`` takes JSON Merge Patch too."""
    if spec.body is None or spec.method != "PATCH":
        return None
    return {"requestBody": {"content": {MERGE_PATCH: {"schema": inline(spec.body)}}}}


def parameter(
    name: str, annotation: object, default: object = inspect.Parameter.empty
) -> inspect.Parameter:
    """A keyword-only parameter of a generated endpoint."""
    return inspect.Parameter(
        name, inspect.Parameter.KEYWORD_ONLY, annotation=annotation, default=default
    )


def inline(body: type[BaseModel]) -> dict[str, Any]:
    """``body``'s JSON schema, inline: a reference by name could miss a renamed one.

    FastAPI names a schema after its class, or after its module too when two
    classes share a name; nested schemas are referred to as components.
    """
    schema = body.model_json_schema(ref_template="#/components/schemas/{model}")
    schema.pop("$defs", None)
    return schema


def unit_of_work_dependency(
    factory: UnitOfWorkFactory | type[UnitOfWork],
) -> UnitOfWorkFactory:
    """A unit of work dependency; a class is called without arguments.

    FastAPI would read a class's ``__init__`` parameters from the query
    string, and call it on its thread pool, so it is wrapped in a coroutine.
    """
    if not isinstance(factory, type):
        return factory

    async def unit_of_work() -> UnitOfWork:
        made: UnitOfWork = factory()
        return made

    return unit_of_work


def awaited[T](dependency: T) -> T:
    """A class dependency (a ``FilterSet``) FastAPI awaits, built on the loop.

    FastAPI calls a class on its thread pool; this coroutine takes the
    class's parameters and builds it where the request runs.
    """
    if not isinstance(dependency, type):
        return dependency
    cls: type = dependency

    async def build(**values: Any) -> object:  # noqa: ANN401 - the class's own parameters.
        return cls(**values)

    build.__signature__ = inspect.signature(cls)  # type: ignore[attr-defined]
    return cast("T", build)
