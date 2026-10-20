"""Resources: an aggregate over REST, read through routes and changed by commands.

A ``ResourceRouter`` is an ``APIRouter`` that lists and reads one kind of
aggregate, and runs commands on it through the application's services::

    products = ResourceRouter(
        "/products",
        model=Product,
        unit_of_work=get_unit_of_work,          # a dependency; tests override it
        repository=lambda uow: uow.products,
        filters=ProductFilters,                 # a FilterSet
        rule=ModelActions(Product),             # view, add, change, delete
    )
    products.command_route(
        CreateProduct, service=services.create_product, body=ProductIn
    )
    products.command_route(
        RenameProduct, service=services.rename_product, body=RenameIn, path="/rename"
    )
    products.command_route(
        DeleteProduct, service=services.delete_product, method="DELETE"
    )

That gives:

- ``GET /products``: a page, filtered, ordered, and searched by the
  ``FilterSet``, paged by ``Pagination`` (``?limit=&cursor=``).
- ``GET /products/{product_id}``: one, with an ``ETag``; ``If-None-Match``
  answers ``304``.
- ``POST /products``: ``CreateProduct`` built from the body, run by the
  service in its unit of work; ``201`` with ``Location`` and ``ETag``.
- ``POST /products/{product_id}/rename``: a command on one product; the id
  comes from the path. ``If-Match`` makes it conditional (``412`` if the
  product changed meanwhile); ``require_if_match=True`` makes it
  mandatory (``428`` without).
- ``DELETE /products/{product_id}``: ``204``.

``query_route`` answers a ``Query`` message through its handler:
``GET /products/by-owner?owner_id=...``, from the query string.

Task-based commands are the norm: each request is one use case. For
reference data, ``crud=True`` adds a generic ``POST``, ``PATCH`` (a
``PatchModel``; JSON or ``application/merge-patch+json``), and ``DELETE``
that set fields directly and record ``RecordCreated``, ``RecordChanged``,
and ``RecordDeleted``.

Every route needs the resource's ``rule``: a ``Rule`` for all of them, or
``ModelActions`` for the model permission of each route's action. A
resource without one declares no rule, so ``check`` lists its routes and
staging and production refuse to start (deny by default); add
``dependencies=[Public]`` for a resource anyone may call. ``scope``
narrows what a caller sees to criteria of its own (``VisibleTo``), and
what is out of scope is ``404``, in lists and items alike. ``policy``
(a ``FieldPolicy``) hides fields, or keeps them from being changed, by
permission. ``parent`` nests a resource: ``/orders/{order_id}/invoices``.
"""

import dataclasses
import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Annotated, Any, Final, Literal, get_origin, get_type_hints
from uuid import UUID, uuid4

from fastapi import APIRouter, Body, Depends, Header, Path, Request, Response, status
from fastapi import Query as QueryParameters
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, create_model
from starlette.requests import HTTPConnection
from starlette.routing import BaseRoute

from domainkit.contrib.auth.access import (
    AllOf,
    AnyOf,
    BasePermission,
    Not,
    Require,
    Rule,
    check_object_permissions,
)
from domainkit.ddd.fields import FieldInfo, fields_of, wrapped_field
from domainkit.ddd.messages import Command
from domainkit.ddd.messages import Query as QueryMessage
from domainkit.ddd.model import AggregateRoot, ValueObject
from domainkit.ddd.preconditions import expect_version
from domainkit.ddd.query import CriteriaLike, F, Query, as_criteria, as_query
from domainkit.ddd.repository import Repository
from domainkit.ddd.uow import UnitOfWork
from domainkit.http.errors import error_responses
from domainkit.http.filters import FilterSet
from domainkit.http.pagination import PageRequest, Pagination
from domainkit.utils.headers import none_match
from domainkit.utils.text import pluralize, snake_case

from .access import ADD, CHANGE, DELETE, VIEW, Actions, rules_for
from .conditional import PreconditionRequired, entity_tag, expected_versions
from .events import RecordChanged, RecordCreated, RecordDeleted
from .policies import FieldPolicy
from .schemas import GENERATED, DomainSchema, PatchModel, schema_for

MERGE_PATCH: Final = "application/merge-patch+json"
"""RFC 7396's media type; ``PATCH`` routes take it as well as JSON."""
RESERVED: Final = frozenset(
    {
        "request",
        "uow",
        "parent_uow",
        "page",
        "filters",
        "body",
        "if_match",
        "if_none_match",
        "parameters",
    }
)
"""Parameter names the generated endpoints use themselves."""
PROVIDED: Final = "provided__"
"""Starts the parameters of a command's ``provide`` dependencies."""

type UnitOfWorkFactory = Callable[[], UnitOfWork]
type RepositoryOf = Callable[[Any], Repository[Any, Any]]
type Scope = Callable[[HTTPConnection], Awaitable[CriteriaLike | None]]
type Service = Callable[[Any, Any], Awaitable[Any]]
type Handler = Callable[[Any, Any], Awaitable[Any]]
type Values = dict[str, Any]
type Aggregate = AggregateRoot[Any]


class _Default:
    """The resource's own schema, as a command's ``returns``."""


DEFAULT: Final = _Default()


class ResourceRouter(APIRouter):
    """The routes of one aggregate type (see the module)."""

    def __init__(
        self,
        prefix: str,
        *,
        model: type | Callable[[], type],
        unit_of_work: UnitOfWorkFactory | type[UnitOfWork],
        repository: RepositoryOf,
        schema: type[BaseModel] | None = None,
        present: Callable[[Any], BaseModel] | None = None,
        rule: Rule | Actions | None = None,
        filters: type[FilterSet] | Callable[..., Any] | None = None,
        pagination: Pagination | None = None,
        page_schema: type[BaseModel] | None = None,
        lookup: str | None = None,
        id_type: type | None = None,
        basename: str | None = None,
        plural: str | None = None,
        parent: "ResourceRouter | None" = None,
        parent_field: str | None = None,
        scope: Scope | None = None,
        policy: FieldPolicy | None = None,
        require_if_match: bool = False,
        links: bool = False,
        crud: bool = False,
        create: type[BaseModel] | None = None,
        update: type[PatchModel] | None = None,
        new_id: Callable[[], object] = uuid4,
        not_found: Callable[[Any], Exception] | None = None,
        **options: Any,  # noqa: ANN401 - APIRouter's options (tags, dependencies).
    ) -> None:
        """Declare the resource and add its list and read routes.

        ``model`` is the aggregate class, or a function returning it (for a
        swappable model); then ``schema``, ``basename``, and ``id_type``
        must be given. ``unit_of_work`` is a FastAPI dependency (a class is
        called without arguments); ``repository`` picks the repository from
        a unit of work. ``present`` turns an aggregate into ``schema``
        (default: its ``from_domain``). ``filters`` is a ``FilterSet``, or
        a dependency returning a ``Query`` or criteria. ``page_schema``
        replaces the page's schema; ``lookup`` names the id in the path
        (``<basename>_id``). ``not_found(id)`` is the error for an id that
        names nothing (default: the repository's ``not_found``).

        Raises:
            TypeError: A declaration does not fit the model.
        """
        if not prefix.startswith("/") or prefix.endswith("/"):
            raise TypeError(
                f"A resource's prefix starts with / and does not end with one: "
                f"{prefix!r}."
            )
        lazy = not isinstance(model, type)
        if lazy and (schema is None or basename is None):
            raise TypeError(
                "A resource whose model is resolved later needs a schema and a "
                "basename."
            )
        self._model = model
        self.basename = basename or snake_case(model.__name__)
        self.plural = plural or pluralize(self.basename)
        self.lookup = lookup or f"{self.basename}_id"
        if self.lookup in RESERVED or not self.lookup.isidentifier():
            raise TypeError(f"{self.lookup!r} cannot name a resource's id.")
        self.id_type = id_type or _id_type(model)
        self.parent = parent
        self.parent_field = parent_field
        if (parent is None) != (parent_field is None):
            raise TypeError("A nested resource needs both parent and parent_field.")
        if parent is not None:
            if parent.lookup == self.lookup:
                raise TypeError(
                    f"The parent and the resource both use {self.lookup!r}."
                )
            prefix = f"{parent.prefix}/{{{parent.lookup}}}{prefix}"
        super().__init__(prefix=prefix, **options)
        self.unit_of_work = _dependency(unit_of_work)
        self.repository = repository
        self.missing = not_found
        """The error for an id that names nothing; None: the repository's."""
        self.rule = rule
        self.scope = scope
        self.policy = policy
        self.require_if_match = require_if_match
        self.links = links
        self.pagination = pagination or Pagination()
        self.filters = filters
        given = schema or schema_for(model)  # type: ignore[arg-type]
        if policy is not None:
            policy.check(given.model_fields, given.__name__)
            _check_filters(filters, policy)
        self.source_schema = given
        self.schema = self._documented(given)
        self.present = present or _presenter(given)
        self.page_schema = page_schema or self._page_schema()
        self.read_name = f"read_{self.basename}"
        self._checks_objects: dict[str, bool] = {}
        """Per action, whether its rules look at the aggregate itself."""
        self._add_list()
        self._add_read()
        if crud:
            self._add_crud(create, update, new_id)

    @property
    def model(self) -> type:
        """The aggregate class (resolved now, if it is resolved per request)."""
        model = self._model
        return model if isinstance(model, type) else model()

    # Declaring routes -----------------------------------------------------------

    def command_route(
        self,
        command: type[Command],
        *,
        service: Service,
        body: type[BaseModel] | None = None,
        returns: type[BaseModel] | _Default | None = DEFAULT,
        method: Literal["POST", "PUT", "PATCH", "DELETE"] = "POST",
        path: str = "",
        action: str | None = None,
        rule: Rule | None = None,
        status_code: int | None = None,
        name: str | None = None,
        summary: str | None = None,
        provide: Mapping[str, Callable[..., Any]] | None = None,
    ) -> None:
        """Add a route that runs ``command`` through ``service(command, uow)``.

        A command with the resource's ``lookup`` field (``product_id``)
        changes one aggregate: its route is ``/{product_id}{path}``, and
        the id comes from the path. Others act on the collection (``path``
        after the prefix). The body's fields, the path's ids (and the
        parent's, by ``parent_field``) build the command; a ``PatchModel``
        body gives only the fields the request set. ``provide`` maps other
        fields to FastAPI dependencies that give them, such as the caller's
        id for an ``owner_id``: the path wins over them, and they over the
        body.

        ``returns`` is the answer's schema: by default the resource's, for
        the aggregate the service returns (``201`` with ``Location`` for a
        ``POST`` on the collection, ``ETag`` always); None answers ``204``,
        as a ``DELETE`` does by default. ``action`` names the permission
        ``ModelActions`` asks for (default: ``add`` to create, ``delete``,
        or ``change``); ``rule`` adds a rule of the route's own.

        Raises:
            TypeError: The body has fields the command lacks, or the
                command needs fields nothing gives.
        """
        names = {field.name for field in dataclasses.fields(command)}
        detail = self.lookup in names
        from_path = {self.lookup} if detail else set()
        if self.parent_field is not None and self.parent_field in names:
            from_path.add(self.parent_field)
        provided = dict(provide or {})
        given = set(body.model_fields) if body is not None else set()
        if extra := sorted((given | set(provided)) - names):
            raise TypeError(f"{command.__name__} has no field {', '.join(extra)}.")
        required = {
            field.name
            for field in dataclasses.fields(command)
            if field.default is dataclasses.MISSING
            and field.default_factory is dataclasses.MISSING
        }
        if missing := sorted(required - given - set(provided) - from_path):
            raise TypeError(f"Nothing gives {command.__name__}'s {', '.join(missing)}.")
        if returns is DEFAULT and method == "DELETE":
            returns = None
        creates = method == "POST" and not detail
        code = status_code or (
            status.HTTP_204_NO_CONTENT
            if returns is None
            else status.HTTP_201_CREATED
            if creates
            else status.HTTP_200_OK
        )
        verb = action or (DELETE if method == "DELETE" else ADD if creates else CHANGE)
        partial = body is not None and issubclass(body, PatchModel)
        if partial and self.policy is not None:
            body = self.policy.describe_changes(body)  # type: ignore[arg-type]

        async def run(values: Values) -> Response:
            request: Request = values["request"]
            payload: BaseModel | None = values.get("body")
            changes = _changes(payload)
            if partial and self.policy is not None:
                await self.policy.check_changes(request, changes)
            arguments = {
                **changes,
                **{name: values[f"{PROVIDED}{name}"] for name in provided},
                **{name: values[self._path_name(name)] for name in from_path},
            }
            built = command(**arguments)
            uow = values["uow"]
            if not detail:
                result = await service(built, uow)
            else:
                await self._precheck(uow, request, values, verb)
                result = await self._conditionally(values, lambda: service(built, uow))
            return await self._answer(request, values, result, returns, code)

        self._route(
            f"/{{{self.lookup}}}{path}" if detail else path,
            method,
            run,
            name=name or snake_case(command.__name__),
            summary=summary or _first_line(command.__doc__),
            action=verb,
            rule=rule,
            detail=detail,
            body=body,
            returns=returns,
            status_code=code,
            writes=True,
            provided=provided,
        )

    def query_route(
        self,
        query: type[QueryMessage],
        *,
        handler: Handler,
        path: str,
        action: str = VIEW,
        rule: Rule | None = None,
        name: str | None = None,
        summary: str | None = None,
    ) -> None:
        """Add a ``GET`` route that answers ``query`` with ``handler(query, uow)``.

        The query's fields come from the query string, as
        ``schema_for(query, write=True)`` describes them (bounded; unknown
        parameters are refused), and the resource's ids from the path: a
        query with the ``lookup`` field asks about one aggregate, at
        ``/{product_id}{path}``. The handler returns an aggregate, answered
        as a read is, with its ``ETag``; or, as its return annotation
        says, a sequence of them, answered as ``{"items": [...]}``. Either
        way, what the caller may not reach (``scope``, ``parent``) or see
        (``policy``) is left out. ``action`` names the permission
        ``ModelActions`` asks for (``view``); ``rule`` adds one.

        Raises:
            TypeError: The path is empty, or does not start with ``/``; the
                handler does not annotate what it returns.
        """
        if not path.startswith("/"):
            raise TypeError(f"A query route's path starts with /, not {path!r}.")
        many = _returns_many(handler)
        fields = fields_of(query)
        detail = self.lookup in fields
        from_path = {self.lookup} if detail else set()
        if self.parent_field is not None and self.parent_field in fields:
            from_path.add(self.parent_field)
        asked = (
            schema_for(
                query,
                write=True,
                exclude=sorted(from_path),
                name=f"{query.__name__}Parameters",
            )
            if set(fields) - from_path
            else None
        )

        async def run(values: Values) -> Response:
            request: Request = values["request"]
            given: BaseModel | None = values.get("parameters")
            arguments = {
                name: to_domain(fields[name], value)
                for name, value in (given.model_dump() if given else {}).items()
            }
            arguments |= {name: values[self._path_name(name)] for name in from_path}
            uow = values["uow"]
            if detail:
                await self._precheck(uow, request, values, action)
            result = await handler(query(**arguments), uow)
            criteria = [
                as_criteria(each) for each in await self._criteria(request, values)
            ]
            if many:
                hidden = await self._hidden(request)
                items = [
                    self._dump(request, values, item, hidden)
                    for item in result
                    if all(each.matches(item) for each in criteria)
                ]
                return JSONResponse({"items": items})
            if not isinstance(result, AggregateRoot):
                raise RuntimeError(
                    f"{_label_of(handler)} answered {type(result).__name__}, not "
                    f"a {self.model.__name__}."
                )
            if not all(each.matches(result) for each in criteria):
                missing = self.missing or self.repository(uow).not_found
                raise missing(result.id)
            await self._check_object(request, result, action)
            return await self._respond(
                request, values, result, if_none_match=values.get("if_none_match")
            )

        self._route(
            f"/{{{self.lookup}}}{path}" if detail else path,
            "GET",
            run,
            name=name or snake_case(query.__name__),
            summary=summary or _first_line(query.__doc__),
            action=action,
            rule=rule,
            detail=detail,
            reads=not many,
            asked=asked,
            returns=(
                create_model(f"{query.__name__}Answer", items=(list[self.schema], ...))  # type: ignore[name-defined]
                if many
                else self.schema
            ),
        )
        if not detail:
            self._before_items(self.routes.pop())

    def _before_items(self, route: BaseRoute) -> None:
        """Put ``route`` before the routes of one item, so its path matches first.

        ``/products/by-owner`` would otherwise be read as the id of
        ``/products/{product_id}``.
        """
        item = f"{self.prefix}/{{{self.lookup}}}"
        first = next(
            (
                index
                for index, each in enumerate(self.routes)
                if getattr(each, "path", "").startswith(item)
            ),
            len(self.routes),
        )
        self.routes.insert(first, route)

    def _add_list(self) -> None:
        """``GET`` the collection: one page."""

        async def run(values: Values) -> Response:
            request: Request = values["request"]
            page: PageRequest = values["page"]
            query = await self._list_query(request, values)
            async with values["uow"] as uow:
                found = await page.fetch(self.repository(uow), query)
            hidden = await self._hidden(request)
            items = [self._dump(request, values, item, hidden) for item in found.items]
            out = page.respond(found, items, query)
            body = {"items": items}
            for name in self.page_schema.model_fields:
                if name != "items":
                    body[name] = getattr(out, name)
            return JSONResponse(body)

        self._route(
            "",
            "GET",
            run,
            name=f"list_{self.plural}",
            summary=f"List {self.plural.replace('_', ' ')}",
            action=VIEW,
            listing=True,
            returns=self.page_schema,
        )

    def _add_read(self) -> None:
        """``GET`` one aggregate, with its ``ETag``."""

        async def run(values: Values) -> Response:
            request: Request = values["request"]
            async with values["uow"] as uow:
                aggregate = await self._load(uow, request, values)
            await self._check_object(request, aggregate, VIEW)
            return await self._respond(
                request, values, aggregate, if_none_match=values["if_none_match"]
            )

        self._route(
            f"/{{{self.lookup}}}",
            "GET",
            run,
            name=self.read_name,
            summary=f"Read a {self.basename.replace('_', ' ')}",
            action=VIEW,
            detail=True,
            reads=True,
            returns=self.schema,
        )

    def _add_crud(
        self,
        create: type[BaseModel] | None,
        update: type[PatchModel] | None,
        new_id: Callable[[], object],
    ) -> None:
        """Generic ``POST``, ``PATCH``, and ``DELETE`` that set fields directly."""
        model = self.model
        fields = fields_of(model)
        read_only = self.policy.read_only if self.policy is not None else frozenset()
        relations = [name for name, field in fields.items() if field.entity is not None]
        create = create or schema_for(model, write=True, exclude=relations)
        update = update or schema_for(  # type: ignore[assignment]
            model,
            partial=True,
            exclude=[*relations, *(name for name in read_only if name in fields)],
        )
        assert update is not None  # noqa: S101 - for the type checker.
        for schema in (create, update):
            if bad := sorted(
                name
                for name in schema.model_fields
                if name not in fields
                or name in GENERATED
                or fields[name].entity is not None
            ):
                raise TypeError(
                    f"crud=True sets plain fields of {model.__name__}, not "
                    f"{', '.join(bad)}."
                )
        needed = {
            field.name
            for field in dataclasses.fields(model)
            if field.default is dataclasses.MISSING
            and field.default_factory is dataclasses.MISSING
            and field.name != "id"
        }
        if missing := sorted(needed - set(create.model_fields)):
            raise TypeError(
                f"The create schema lacks {model.__name__}'s {', '.join(missing)}."
            )
        label = _label(model)

        async def created(values: Values) -> Response:
            payload: BaseModel = values["body"]
            given = payload.model_dump()
            aggregate = model(
                id=new_id(),
                **{
                    name: to_domain(fields[name], value)
                    for name, value in given.items()
                },
            )
            aggregate.record(
                RecordCreated(
                    model=label, record_id=str(aggregate.id), fields=tuple(given)
                )
            )
            async with values["uow"] as uow:
                await self.repository(uow).add(aggregate)
                await uow.commit()
            return await self._answer(
                values["request"], values, aggregate, DEFAULT, 201
            )

        async def changed(values: Values) -> Response:
            request: Request = values["request"]
            changes = _changes(values["body"])
            if self.policy is not None:
                await self.policy.check_changes(request, changes)

            async def apply() -> AggregateRoot[Any]:
                async with values["uow"] as uow:
                    aggregate = await self._load(uow, request, values)
                    await self._check_object(request, aggregate, CHANGE)
                    names = []
                    for name, value in changes.items():
                        new = to_domain(fields[name], value)
                        if getattr(aggregate, name) != new:
                            setattr(aggregate, name, new)
                            names.append(name)
                    if names:
                        aggregate.record(
                            RecordChanged(
                                model=label,
                                record_id=str(aggregate.id),
                                fields=tuple(names),
                            )
                        )
                    await uow.commit()
                return aggregate

            aggregate = await self._conditionally(values, apply)
            return await self._answer(request, values, aggregate, DEFAULT, 200)

        async def deleted(values: Values) -> Response:
            request: Request = values["request"]

            async def apply() -> None:
                async with values["uow"] as uow:
                    aggregate = await self._load(uow, request, values)
                    await self._check_object(request, aggregate, DELETE)
                    aggregate.record(
                        RecordDeleted(model=label, record_id=str(aggregate.id))
                    )
                    await self.repository(uow).remove(aggregate)
                    await uow.commit()

            await self._conditionally(values, apply)
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        body = (
            self.policy.describe_changes(update) if self.policy is not None else update
        )
        self._route(
            "",
            "POST",
            created,
            name=f"create_{self.basename}",
            summary=f"Create a {self.basename.replace('_', ' ')}",
            action=ADD,
            body=create,
            returns=self.schema,
            status_code=status.HTTP_201_CREATED,
            writes=True,
        )
        self._route(
            f"/{{{self.lookup}}}",
            "PATCH",
            changed,
            name=f"update_{self.basename}",
            summary=f"Change fields of a {self.basename.replace('_', ' ')}",
            action=CHANGE,
            detail=True,
            body=body,
            returns=self.schema,
            writes=True,
        )
        self._route(
            f"/{{{self.lookup}}}",
            "DELETE",
            deleted,
            name=f"delete_{self.basename}",
            summary=f"Delete a {self.basename.replace('_', ' ')}",
            action=DELETE,
            detail=True,
            returns=None,
            status_code=status.HTTP_204_NO_CONTENT,
            writes=True,
        )

    def _route(
        self,
        path: str,
        method: str,
        run: Callable[[Values], Awaitable[Any]],
        *,
        name: str,
        summary: str | None,
        action: str,
        rule: Rule | None = None,
        detail: bool = False,
        body: type[BaseModel] | None = None,
        asked: type[BaseModel] | None = None,
        returns: type[BaseModel] | _Default | None = None,
        status_code: int = status.HTTP_200_OK,
        listing: bool = False,
        reads: bool = False,
        writes: bool = False,
        provided: Mapping[str, Callable[..., Any]] | None = None,
    ) -> None:
        """Add one route, its parameters and OpenAPI answers declared."""
        parameters = [_parameter("request", Request)]
        parameters.append(_parameter("uow", Annotated[Any, Depends(self.unit_of_work)]))
        if self.parent is not None:
            parameters.append(
                _parameter(self.parent.lookup, Annotated[self.parent.id_type, Path()])
            )
            parameters.append(
                _parameter(
                    "parent_uow", Annotated[Any, Depends(self.parent.unit_of_work)]
                )
            )
        if detail:
            parameters.append(_parameter(self.lookup, Annotated[self.id_type, Path()]))
        if listing:
            parameters.append(
                _parameter("page", Annotated[PageRequest, Depends(self.pagination)])
            )
            if self.filters is not None:
                parameters.append(
                    _parameter("filters", Annotated[Any, Depends(self.filters)])
                )
        if reads:
            parameters.append(
                _parameter(
                    "if_none_match",
                    Annotated[
                        str | None,
                        Header(
                            alias="If-None-Match",
                            max_length=4096,
                            description="The ETag you hold: 304 if it is current.",
                        ),
                    ],
                    None,
                )
            )
        if detail and writes:
            parameters.append(
                _parameter(
                    "if_match",
                    Annotated[
                        str | None,
                        Header(
                            alias="If-Match",
                            max_length=4096,
                            description=(
                                "Required: the ETag of the version you change; "
                                "412 if it changed since."
                                if self.require_if_match
                                else "The ETag of the version you change; 412 if "
                                "it changed since."
                            ),
                        ),
                    ],
                    None,
                )
            )
        if body is not None:
            parameters.append(_parameter("body", Annotated[body, Body()]))
        if asked is not None:
            parameters.append(
                _parameter("parameters", Annotated[asked, QueryParameters()])
            )
        parameters.extend(
            _parameter(f"{PROVIDED}{name}", Annotated[Any, Depends(dependency)])
            for name, dependency in (provided or {}).items()
        )
        rules = (*rules_for(self.rule, action), *([rule] if rule is not None else []))
        codes = (
            [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN] if rules else []
        )
        if detail:
            codes.append(status.HTTP_404_NOT_FOUND)
        if writes:
            codes += [status.HTTP_409_CONFLICT, status.HTTP_422_UNPROCESSABLE_CONTENT]
        if writes and detail:
            codes.append(status.HTTP_412_PRECONDITION_FAILED)
            if self.require_if_match:
                codes.append(status.HTTP_428_PRECONDITION_REQUIRED)
        if listing or asked is not None:
            codes.append(status.HTTP_422_UNPROCESSABLE_CONTENT)
        responses: dict[int | str, dict[str, Any]] = dict(error_responses(*codes))
        if reads:
            responses[status.HTTP_304_NOT_MODIFIED] = {
                "description": "The ETag sent is still current."
            }
        extra: dict[str, Any] | None = None
        if body is not None and method == "PATCH":
            schema = _inline(body)
            extra = {"requestBody": {"content": {MERGE_PATCH: {"schema": schema}}}}

        async def endpoint(**values: Any) -> Any:  # noqa: ANN401 - the route's answer.
            if self.parent is not None:
                await self._check_parent(values)
            return await run(values)

        endpoint.__name__ = endpoint.__qualname__ = name
        endpoint.__doc__ = summary
        endpoint.__signature__ = inspect.Signature(parameters)  # type: ignore[attr-defined]
        self.add_api_route(
            path,
            endpoint,
            methods=[method],
            name=name,
            summary=summary,
            status_code=status_code,
            response_model=None if returns is None or returns is DEFAULT else returns,
            responses=responses,
            dependencies=[Depends(Require(*rules))] if rules else None,
            openapi_extra=extra,
        )
        self._checks_objects[action] = any(_checks_objects(each) for each in rules)

    # Answering ------------------------------------------------------------------

    async def _list_query(self, request: Request, values: Values) -> Query:
        """The listing's query: the filters, the parent, and the caller's scope."""
        given = values.get("filters")
        query = given.query() if isinstance(given, FilterSet) else as_query(given)
        return query.where(*await self._criteria(request, values))

    async def _check_parent(self, values: Values) -> None:
        """Refuse a parent the caller may not reach, as its own read would.

        Raises:
            NotFound: There is no such parent, or it is out of reach.
        """
        parent = self.parent
        assert parent is not None  # noqa: S101 - called for nested resources only.
        request: Request = values["request"]
        async with values["parent_uow"] as uow:
            aggregate = await parent._load(uow, request, values)
        await parent._check_object(request, aggregate, VIEW)

    async def _criteria(
        self, request: HTTPConnection, values: Values
    ) -> list[CriteriaLike]:
        """What a caller may reach here: the parent's children, within its scope."""
        criteria: list[CriteriaLike] = []
        if self.parent is not None and self.parent_field is not None:
            criteria.append(F(self.parent_field) == values[self.parent.lookup])
        if self.scope is not None:
            scoped = await self.scope(request)
            if scoped is not None:
                criteria.append(scoped)
        return criteria

    async def _load(
        self, uow: UnitOfWork, request: HTTPConnection, values: Values
    ) -> Aggregate:
        """The aggregate the path names, if the caller may reach it.

        Raises:
            NotFound: There is none, or it is out of the caller's reach
                (the repository's ``not_found``).
        """
        repository: Repository[Aggregate, Any] = self.repository(uow)
        id_ = values[self.lookup]
        criteria = await self._criteria(request, values)
        if not criteria:
            found = await repository.find(id_)
        else:
            found = await repository.first(Query(F("id") == id_).where(*criteria))
        if found is None:
            missing = self.missing or repository.not_found
            raise missing(id_)
        return found

    async def _precheck(
        self, uow: UnitOfWork, request: HTTPConnection, values: Values, action: str
    ) -> None:
        """Before a command: refuse an aggregate the caller may not reach or act on."""
        if not (self.parent or self.scope or self._checks_objects.get(action)):
            return
        async with uow:
            aggregate = await self._load(uow, request, values)
        await self._check_object(request, aggregate, action)

    async def _check_object(
        self, request: HTTPConnection, aggregate: object, action: str
    ) -> None:
        """Ask the rules about the aggregate itself, where a rule checks objects."""
        if self._checks_objects.get(action):
            rules = rules_for(self.rule, action)
            await check_object_permissions(request, aggregate, *rules)

    async def _conditionally[T](
        self, values: Values, change: Callable[[], Awaitable[T]]
    ) -> T:
        """Run ``change``, holding the aggregate to the request's ``If-Match``.

        Raises:
            PreconditionRequired: The resource requires ``If-Match``.
            PreconditionFailed: The aggregate is not at a version it names.
            RuntimeError: The service never loaded the aggregate, so
                ``If-Match`` could not be checked.
        """
        header = values.get("if_match")
        if header is None:
            if self.require_if_match:
                raise PreconditionRequired
            return await change()
        id_ = values[self.lookup]
        with expect_version(self.model, id_, expected_versions(header)) as expected:
            result = await change()
        if not expected.checked:
            raise RuntimeError(
                f"The service never loaded {self.model.__name__} {id_} through its "
                "repository, so If-Match could not be checked."
            )
        return result

    async def _answer(
        self,
        request: Request,
        values: Values,
        result: object,
        returns: type[BaseModel] | _Default | None,
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
        if (returns is DEFAULT or returns is self.source_schema) and isinstance(
            result, AggregateRoot
        ):
            headers = {}
            if status_code == status.HTTP_201_CREATED:
                headers["Location"] = self._url(
                    request, values, self.read_name, result.id
                )
            return await self._respond(
                request, values, result, status_code=status_code, headers=headers
            )
        schema = self.source_schema if returns is DEFAULT else returns
        shown = _presenter(schema)(result)  # type: ignore[arg-type]
        return JSONResponse(shown.model_dump(mode="json"), status_code=status_code)

    async def _respond(
        self,
        request: Request,
        values: Values,
        aggregate: Aggregate,
        *,
        status_code: int = status.HTTP_200_OK,
        headers: Mapping[str, str] | None = None,
        if_none_match: str | None = None,
    ) -> Response:
        """One aggregate as JSON, tagged; ``304`` if ``If-None-Match`` names the tag."""
        hidden = await self._hidden(request)
        document = self._dump(request, values, aggregate, hidden)
        body = bytes(JSONResponse(document).body)
        etag = entity_tag(aggregate.version, body)
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

    def _dump(
        self,
        request: Request,
        values: Values,
        aggregate: Aggregate,
        hidden: frozenset[str],
    ) -> dict[str, Any]:
        """The JSON of one aggregate, without what the caller may not see."""
        document = self.present(aggregate).model_dump(mode="json", exclude=set(hidden))
        if self.links:
            document["links"] = self._links(request, values, aggregate)
        return document

    def _links(
        self, request: Request, values: Values, aggregate: Aggregate
    ) -> dict[str, str]:
        """The aggregate's own URL, and its parent's."""
        links = {"self": self._url(request, values, self.read_name, aggregate.id)}
        if self.parent is not None:
            links["parent"] = self._url(
                request,
                values,
                self.parent.read_name,
                values[self.parent.lookup],
                parent=True,
            )
        return links

    def _url(
        self,
        request: Request,
        values: Values,
        name: str,
        id_: object,
        *,
        parent: bool = False,
    ) -> str:
        """The path of the read route ``name`` for ``id_`` (and the parent's id)."""
        owner = self.parent if parent else self
        assert owner is not None  # noqa: S101 - a parent was asked for only with one.
        params = {owner.lookup: str(id_)}
        if owner.parent is not None:
            params[owner.parent.lookup] = str(values[owner.parent.lookup])
        path = request.app.url_path_for(name, **params)
        return f"{request.scope.get('root_path', '')}{path}"

    async def _hidden(self, request: HTTPConnection) -> frozenset[str]:
        """The fields the caller may not see."""
        return (
            await self.policy.hidden(request)
            if self.policy is not None
            else frozenset()
        )

    def _path_name(self, name: str) -> str:
        """The path parameter that gives the command field ``name``."""
        if name == self.parent_field and self.parent is not None:
            return self.parent.lookup
        return name

    def _documented(self, schema: type[BaseModel]) -> type[BaseModel]:
        """``schema`` as OpenAPI shows it: policies noted, links added."""
        documented = self.policy.document(schema) if self.policy is not None else schema
        if not self.links:
            return documented
        return create_model(
            schema.__name__,
            __base__=documented,
            links=(
                dict[str, str],
                Field(description="URLs: self, and parent for a nested resource."),
            ),
        )

    def _page_schema(self) -> type[BaseModel]:
        """``<Name>Page``: the items, and how to ask for the next page."""
        fields: dict[str, Any] = {"items": (list[self.schema], ...)}  # type: ignore[name-defined]
        if self.pagination.offset:
            fields["next_offset"] = (
                int | None,
                Field(default=None, description="Pass back as offset."),
            )
        else:
            fields["next_cursor"] = (
                str | None,
                Field(default=None, description="Pass back as cursor."),
            )
        if self.pagination.count:
            fields["total"] = (
                int | None,
                Field(default=None, description="Every match."),
            )
        name = self.schema.__name__.removesuffix("Out")
        return create_model(f"{name}Page", **fields)


def to_domain(field: FieldInfo, value: object) -> object:
    """A request's ``value`` as ``field`` holds it: value objects built, enums found.

    Raises:
        InvalidQuery: The value does not fit the field.
    """
    if value is None:
        return None
    kind = field.kind
    if (
        isinstance(kind, type)
        and issubclass(kind, ValueObject)
        and wrapped_field(kind) is None
        and isinstance(value, Mapping)
    ):
        inner = fields_of(kind)
        return kind(
            **{name: to_domain(inner[name], item) for name, item in value.items()}
        )
    return field.coerce(value)


def _changes(payload: BaseModel | None) -> dict[str, Any]:
    """A body's fields as a command's keywords; a ``PatchModel``'s set ones only.

    Nested models become dicts, as ``model_dump`` gives them.
    """
    if payload is None:
        return {}
    if isinstance(payload, PatchModel):
        return {name: _plain(value) for name, value in payload.changes().items()}
    return payload.model_dump()


def _plain(value: object) -> object:
    """A nested model (or a list of them) as dicts; other values as they are."""
    if isinstance(value, BaseModel):
        return value.model_dump()
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _presenter(schema: type[BaseModel]) -> Callable[[Any], BaseModel]:
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


def _dependency(factory: UnitOfWorkFactory | type[UnitOfWork]) -> UnitOfWorkFactory:
    """A unit of work dependency; a class is called without arguments.

    FastAPI would read a class's ``__init__`` parameters from the query
    string, so it is wrapped.
    """
    if not isinstance(factory, type):
        return factory

    def unit_of_work() -> UnitOfWork:
        made: UnitOfWork = factory()
        return made

    return unit_of_work


def _id_type(model: type | Callable[[], type]) -> type:
    """The type of the model's ids in paths: ``UUID`` unless the hint says."""
    if not isinstance(model, type):
        return UUID
    kind = fields_of(model)["id"].kind
    return kind if kind in {UUID, int, str} else UUID


def _parameter(
    name: str, annotation: object, default: object = inspect.Parameter.empty
) -> inspect.Parameter:
    """A keyword-only parameter of a generated endpoint."""
    return inspect.Parameter(
        name, inspect.Parameter.KEYWORD_ONLY, annotation=annotation, default=default
    )


def _inline(body: type[BaseModel]) -> dict[str, Any]:
    """``body``'s JSON schema, inline: a reference by name could miss a renamed one.

    FastAPI names a schema after its class, or after its module too when two
    classes share a name; nested schemas are referred to as components.
    """
    schema = body.model_json_schema(ref_template="#/components/schemas/{model}")
    schema.pop("$defs", None)
    return schema


def _check_filters(filters: object, policy: FieldPolicy) -> None:
    """Refuse filters on fields the policy hides: values leak through them.

    Filtering, ordering (and the cursors it makes), and searching on a
    field tell its values to a caller who may not see it.

    Raises:
        TypeError: The ``FilterSet`` filters, orders, or searches a hidden field.
    """
    if not (isinstance(filters, type) and issubclass(filters, FilterSet)):
        return
    used = {
        *(each.field or name for name, (each, _, _) in filters.filters.items()),
        *filters.ordering_fields,
        *(item.removeprefix("-") for item in filters.default_ordering),
        *filters.search_fields,
    }
    if hidden := sorted(
        {name.partition(".")[0] for name in used if name} & set(policy.visible)
    ):
        raise TypeError(
            f"{filters.__name__} filters, orders, or searches fields some callers "
            f"may not see: {', '.join(hidden)}."
        )


def _checks_objects(rule: Rule) -> bool:
    """Whether ``rule`` (or a rule inside it) looks at objects."""
    if isinstance(rule, type):
        rule = rule()
    if isinstance(rule, AllOf | AnyOf):
        return any(_checks_objects(each) for each in rule.rules)
    if isinstance(rule, Not):
        return _checks_objects(rule.rule)
    return type(rule).has_object_permission is not BasePermission.has_object_permission


def _returns_many(handler: Handler) -> bool:
    """Whether ``handler`` returns a sequence (of aggregates), as annotated.

    Raises:
        TypeError: It does not annotate what it returns.
    """
    returned = get_type_hints(handler).get("return")
    if returned is None:
        raise TypeError(
            f"Annotate what {_label_of(handler)} returns: an aggregate, or a list."
        )
    origin = get_origin(returned)
    return isinstance(origin, type) and issubclass(origin, Sequence)


def _label_of(function: Callable[..., object]) -> str:
    """A function's name, for messages."""
    return getattr(function, "__qualname__", repr(function))


def _first_line(text: str | None) -> str | None:
    """The first line of a docstring, as a route's summary."""
    return text.strip().splitlines()[0] if text and text.strip() else None


def _label(model: type) -> str:
    """The content type of ``model`` (``catalog.category``), or its class name."""
    try:
        from domainkit.contrib.contenttypes import content_type_key

        return ".".join(content_type_key(model))
    except LookupError:
        return model.__qualname__
