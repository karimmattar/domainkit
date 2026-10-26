"""How a resource answers: aggregates as JSON, tagged, linked, as the caller sees them.

Each aggregate is shown through the resource's schema (``present``), without
the fields its ``FieldPolicy`` hides from the caller, with its links if the
resource has them, and with an ``ETag`` of what was sent, so a later
``If-None-Match`` answers ``304``. Writes answer the aggregate (``Location``
for one created), another schema, or nothing.
"""

from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from fastapi import Response, status
from pydantic import BaseModel, Field, create_model
from starlette.requests import HTTPConnection

from domainkit.ddd.model import AggregateRoot
from domainkit.http.responses import BuiltJSONResponse
from domainkit.utils.headers import none_match

from ..batches import BatchResult
from ..conditional import entity_tag
from ..schemas import DomainSchema
from .context import DEFAULT, MODELS, Aggregate, Returns, RouteContext

if TYPE_CHECKING:
    from . import ResourceRouter


async def answer(
    router: "ResourceRouter",
    context: RouteContext,
    result: object,
    returns: Returns,
    status_code: int,
) -> Response:
    """The answer to a write: the aggregate (tagged), another schema, or none.

    Raises:
        RuntimeError: The service returned nothing to answer with.
    """
    if returns is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    if result is None:
        raise RuntimeError("The service returned nothing; set returns=None.")
    if (returns is DEFAULT or returns is router.source_schema) and isinstance(
        result, AggregateRoot
    ):
        headers = {}
        if status_code == status.HTTP_201_CREATED:
            headers["Location"] = url(router, context, router.read_name, result.id)
        return await respond(
            router, context, result, status_code=status_code, headers=headers
        )
    shown = shown_as(router, returns, result)
    return BuiltJSONResponse(shown.model_dump(mode="json"), status_code=status_code)


async def batch_result(
    router: "ResourceRouter",
    context: RouteContext,
    result: object,
    returns: Returns,
    status_code: int,
) -> BatchResult:
    """One item's result: the aggregate (tagged), another schema, or none.

    Raises:
        RuntimeError: The service returned nothing to answer with.
    """
    if returns is None:
        return BatchResult(status=status.HTTP_204_NO_CONTENT)
    if result is None:
        raise RuntimeError("The service returned nothing; set returns=None.")
    if (returns is DEFAULT or returns is router.source_schema) and isinstance(
        result, AggregateRoot
    ):
        document, _, etag = await tagged(router, context, result)
        location = (
            url(router, context, router.read_name, result.id)
            if status_code == status.HTTP_201_CREATED
            else None
        )
        return BatchResult(
            status=status_code, item=document, etag=etag, location=location
        )
    shown = shown_as(router, returns, result)
    return BatchResult(status=status_code, item=shown.model_dump(mode="json"))


def shown_as(router: "ResourceRouter", returns: Returns, result: object) -> BaseModel:
    """``result`` as the schema a write returns (the resource's, by default)."""
    schema = router.source_schema if returns is DEFAULT else returns
    return presenter(schema)(result)  # type: ignore[arg-type] # Not None: 204.


async def tagged(
    router: "ResourceRouter", context: RouteContext, aggregate: Aggregate
) -> tuple[dict[str, Any], bytes, str]:
    """An aggregate's JSON for this caller, its bytes, and its ``ETag``."""
    hidden = await hidden_fields(router, context.request)
    document = dump(router, context, aggregate, hidden)
    body = bytes(BuiltJSONResponse(document).body)
    return document, body, entity_tag(aggregate.version, body)


async def respond(
    router: "ResourceRouter",
    context: RouteContext,
    aggregate: Aggregate,
    *,
    status_code: int = status.HTTP_200_OK,
    headers: Mapping[str, str] | None = None,
    if_none_match: str | None = None,
) -> Response:
    """One aggregate as JSON, tagged; ``304`` if ``If-None-Match`` names the tag."""
    _, body, etag = await tagged(router, context, aggregate)
    if none_match(if_none_match, etag):
        return Response(
            status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag}
        )
    return Response(
        body,
        status_code=status_code,
        media_type="application/json",
        headers={**(headers or {}), "ETag": etag},
    )


def dump(
    router: "ResourceRouter",
    context: RouteContext,
    aggregate: Aggregate,
    hidden: frozenset[str],
    include: set[str] | None = None,
) -> dict[str, Any]:
    """The JSON of one aggregate (``include``'s fields), without the hidden ones."""
    document = router.present(aggregate).model_dump(
        mode="json", include=include, exclude=set(hidden)
    )
    if router.links:
        document["links"] = links(router, context, aggregate)
    return document


def links(
    router: "ResourceRouter", context: RouteContext, aggregate: Aggregate
) -> dict[str, str]:
    """The aggregate's own URL, and its parent's."""
    found = {"self": url(router, context, router.read_name, aggregate.id)}
    if router.parent is not None:
        found["parent"] = url(
            router,
            context,
            router.parent.read_name,
            context.ids[router.parent.lookup],
            parent=True,
        )
    return found


def url(
    router: "ResourceRouter",
    context: RouteContext,
    name: str,
    id_: object,
    *,
    parent: bool = False,
) -> str:
    """The path of the read route ``name`` for ``id_`` (and the parent's id)."""
    owner = router.parent if parent else router
    assert owner is not None  # noqa: S101 - a parent was asked for only with one.
    params = {owner.lookup: str(id_)}
    if owner.parent is not None:
        params[owner.parent.lookup] = str(context.ids[owner.parent.lookup])
    request = context.request
    path = request.app.url_path_for(name, **params)
    # A path for a response's links, not a response (a Flask rule).
    # nosemgrep: directly-returned-format-string  # noqa: ERA001
    return f"{request.scope.get('root_path', '')}{path}"


async def hidden_fields(
    router: "ResourceRouter", request: HTTPConnection
) -> frozenset[str]:
    """The fields the caller may not see."""
    return (
        await router.policy.hidden(request)
        if router.policy is not None
        else frozenset()
    )


def presenter(schema: type[BaseModel]) -> Callable[[Any], BaseModel]:
    """How to show an aggregate as ``schema``: ``from_domain``, or its attributes."""
    if issubclass(schema, DomainSchema):
        return schema.from_domain
    from_domain = getattr(schema, "from_domain", None)
    if callable(from_domain):
        return from_domain  # type: ignore[no-any-return]

    def show(value: object) -> BaseModel:
        if isinstance(value, schema):
            return value
        return schema.model_validate(value, from_attributes=True)

    return show


def documented(router: "ResourceRouter", schema: type[BaseModel]) -> type[BaseModel]:
    """``schema`` as OpenAPI shows it: policies noted, links added."""
    shown = router.policy.document(schema) if router.policy is not None else schema
    if not router.links:
        return shown
    return create_model(
        schema.__name__,
        __module__=MODELS,
        __base__=shown,
        links=(
            dict[str, str],
            Field(description="URLs: self, and parent for a nested resource."),
        ),
    )


def page_schema(router: "ResourceRouter") -> type[BaseModel]:
    """``<Name>Page``: the items, and how to ask for the next page."""
    item = router.schema
    if router.list_fields is not None:
        kept: dict[str, Any] = {
            name: (field.annotation, field)
            for name, field in router.schema.model_fields.items()
            if name in router.list_fields
        }
        item = create_model(
            f"{router.schema.__name__.removesuffix('Out')}Summary",
            __module__=MODELS,
            **kept,
        )
    fields: dict[str, Any] = {"items": (list[item], ...)}
    if router.pagination.offset:
        fields["next_offset"] = (
            int | None,
            Field(default=None, description="Pass back as offset."),
        )
    else:
        fields["next_cursor"] = (
            str | None,
            Field(default=None, description="Pass back as cursor."),
        )
    if router.pagination.count:
        fields["total"] = (
            int | None,
            Field(default=None, description="Every match."),
        )
    name = router.schema.__name__.removesuffix("Out")
    return create_model(f"{name}Page", __module__=MODELS, **fields)


def listed(schema: type[BaseModel], fields: Sequence[str] | None) -> set[str] | None:
    """The fields lists show, if they show only some.

    Raises:
        TypeError: The schema lacks one.
    """
    if fields is None:
        return None
    if unknown := sorted(set(fields) - set(schema.model_fields)):
        raise TypeError(f"{schema.__name__} has no field {', '.join(unknown)}.")
    return set(fields)
