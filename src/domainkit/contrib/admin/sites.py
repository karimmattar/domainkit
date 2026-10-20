"""The admin site: registered aggregates, their resources, and ``/meta``."""

import inspect
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Path, Request, status
from fastapi import Query as QueryParameter
from pydantic import BaseModel, Field, JsonValue
from starlette.requests import HTTPConnection

from domainkit.apps import registry
from domainkit.contrib.auth.access import (
    AllOf,
    HasPermissions,
    IsAdminUser,
    IsSuperuser,
    Require,
    Rule,
    current_user,
)
from domainkit.ddd.messages import Command, Query
from domainkit.http import CURSOR_PATTERN, MAX_CURSOR_LENGTH, error_responses
from domainkit.http.filters import FilterSet
from domainkit.http.pagination import Pagination
from domainkit.resources import FieldPolicy, ModelActions, ResourceRouter
from domainkit.resources.access import VIEW, content_type_of, rules_for
from domainkit.resources.routing import (
    DEFAULT,
    Handler,
    Operation,
    RepositoryOf,
    Scope,
    Service,
    UnitOfWorkFactory,
)
from domainkit.utils.text import pluralize, snake_case

AUDIT: Final = "domainkit.contrib.audit"
HISTORY_PAGE: Final = 50
PARAMETER: Final = re.compile(r"{(\w+)}")
STAFF: Final = IsAdminUser() | IsSuperuser()
"""Who may use the admin at all: staff, or superusers."""
OWN_ADMINS: Final = frozenset({"auth"})
"""Domains whose models have an admin of their own, which keeps their rules."""
VIEW_HISTORY: Final = "audit.view_history"
"""The permission histories need, as ``contrib.audit`` names it."""


@dataclass(frozen=True, slots=True)
class AdminCommand:
    """A command the admin runs on a model, through its application service.

    ``options`` are ``ResourceRouter.command_route``'s.
    """

    command: type[Command]
    options: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class AdminQuery:
    """A query the admin answers, through its handler (``query_route``)."""

    query: type[Query]
    options: Mapping[str, Any]


def command(
    command: type[Command],
    *,
    service: Service,
    body: type[BaseModel] | None = None,
    method: str = "POST",
    path: str = "",
    action: str | None = None,
    rule: Rule | None = None,
    returns: Any = DEFAULT,  # noqa: ANN401 - a schema, DEFAULT, or None.
    name: str | None = None,
    summary: str | None = None,
    provide: Mapping[str, Callable[..., Any]] | None = None,
) -> AdminCommand:
    """An admin action: ``command`` run by ``service`` (see ``command_route``)."""
    return AdminCommand(
        command,
        {
            "service": service,
            "body": body,
            "method": method,
            "path": path,
            "action": action,
            "rule": rule,
            "returns": returns,
            "name": name,
            "summary": summary,
            "provide": provide,
        },
    )


def query(
    query: type[Query],
    *,
    handler: Handler,
    path: str,
    action: str = VIEW,
    rule: Rule | None = None,
    name: str | None = None,
    summary: str | None = None,
) -> AdminQuery:
    """An admin query: ``query`` answered by ``handler`` (see ``query_route``)."""
    return AdminQuery(
        query,
        {
            "handler": handler,
            "path": path,
            "action": action,
            "rule": rule,
            "name": name,
            "summary": summary,
        },
    )


@dataclass
class ModelAdmin:
    """One registered model: how the admin lists, reads, and changes it."""

    model: type
    unit_of_work: UnitOfWorkFactory | type
    repository: RepositoryOf
    schema: type[BaseModel] | None = None
    present: Callable[[Any], BaseModel] | None = None
    list_display: Sequence[str] = ()
    filters: type[FilterSet] | None = None
    pagination: Pagination | None = None
    commands: Sequence[AdminCommand] = ()
    queries: Sequence[AdminQuery] = ()
    policy: FieldPolicy | None = None
    scope: Scope | None = None
    label: str | None = None
    """The content type (``catalog.product``); default: the mapped class's."""
    plural: str | None = None
    history: bool | None = None
    """Whether items have ``/history``; default: if ``contrib.audit`` is installed."""
    history_field: str | None = None
    """The events' field that holds the id (default: ``<name>_id``)."""
    _resource: ResourceRouter | None = field(default=None, repr=False)
    _history: str | None = field(default=None, repr=False)

    @property
    def resource(self) -> ResourceRouter:
        """The model's resource under the admin, built on first use.

        Raises:
            TypeError: The model has an admin of its own, or a declaration
                does not fit it.
        """
        if self._resource is None:
            self._resource = self._build()
        return self._resource

    @property
    def history_route(self) -> str | None:
        """The name of the history route, if items have one."""
        return self._history if self.resource else None

    @property
    def listed(self) -> list[str]:
        """The fields lists show, in order: ``id`` and ``list_display``, or all."""
        if self.list_display:
            return list(dict.fromkeys(["id", *self.list_display]))
        return list(self.resource.schema.model_fields)

    def _build(self) -> ResourceRouter:
        """The resource: lists, reads, commands, queries, and history."""
        domain, name = content_type_of(self.label or self.model)
        if domain in OWN_ADMINS:
            raise TypeError(
                f"{domain}.{name} has an admin of its own (/admin/{domain}), "
                "which keeps its rules: grant ceilings, the last superuser."
            )
        basename = snake_case(self.model.__name__)
        resource = ResourceRouter(
            f"/{domain}/{self.plural or pluralize(basename)}",
            model=self.model,
            unit_of_work=self.unit_of_work,
            repository=self.repository,
            schema=self.schema,
            present=self.present,
            rule=ModelActions(f"{domain}.{name}", STAFF),
            filters=self.filters,
            pagination=self.pagination,
            policy=self.policy,
            scope=self.scope,
            links=True,
            list_fields=self.listed if self.list_display else None,
            name_prefix=f"admin_{domain}_",
        )
        for action in self.commands:
            resource.command_route(action.command, **action.options)
        for question in self.queries:
            resource.query_route(question.query, **question.options)
        installed = registry.is_installed(AUDIT)
        if self.history or (self.history is None and installed):
            if not installed:
                raise TypeError(f"history=True needs {AUDIT} installed.")
            self._history = _add_history(
                resource, self.history_field or resource.lookup
            )
        return resource


class AdminSite:
    """Registered models, served under one prefix with ``GET /meta``."""

    def __init__(self) -> None:
        """Start empty."""
        self.admins: dict[type, ModelAdmin] = {}
        self._router: APIRouter | None = None

    def register(
        self,
        model: type,
        *,
        unit_of_work: UnitOfWorkFactory | type,
        repository: RepositoryOf,
        schema: type[BaseModel] | None = None,
        present: Callable[[Any], BaseModel] | None = None,
        list_display: Sequence[str] = (),
        filters: type[FilterSet] | None = None,
        search: Sequence[str] = (),
        ordering: Sequence[str] = (),
        pagination: Pagination | None = None,
        commands: Iterable[AdminCommand] = (),
        queries: Iterable[AdminQuery] = (),
        policy: FieldPolicy | None = None,
        scope: Scope | None = None,
        label: str | None = None,
        plural: str | None = None,
        history: bool | None = None,
        history_field: str | None = None,
    ) -> ModelAdmin:
        """Serve ``model`` in the admin; see the package for the options.

        ``search`` and ``ordering`` make a ``FilterSet`` when ``filters``
        is not given.

        Raises:
            RuntimeError: The site's router is mounted already.
            TypeError: The model is registered already, or ``search`` or
                ``ordering`` come with a ``FilterSet``.
        """
        if self._router is not None:
            raise RuntimeError(
                "Register models before the admin's router is mounted: from "
                "the modules your routes import, or from DomainConfig.ready()."
            )
        if model in self.admins:
            raise TypeError(f"{model.__name__} is registered in the admin already.")
        if filters is not None and (search or ordering):
            raise TypeError("Declare search and ordering in the FilterSet given.")
        if filters is None and (search or ordering):
            filters = type(
                f"{model.__name__}AdminFilters",
                (FilterSet,),
                {
                    "__module__": model.__module__,
                    "model": model,
                    "search_fields": tuple(search),
                    "ordering_fields": tuple(ordering),
                },
            )
        admin = ModelAdmin(
            model,
            unit_of_work,
            repository,
            schema=schema,
            present=present,
            list_display=list_display,
            filters=filters,
            pagination=pagination,
            commands=tuple(commands),
            queries=tuple(queries),
            policy=policy,
            scope=scope,
            label=label,
            plural=plural,
            history=history,
            history_field=history_field,
        )
        self.admins[model] = admin
        return admin

    @property
    def router(self) -> APIRouter:
        """Every registered model's routes, and ``/meta``: mount it once.

        Registering is closed once it is built.
        """
        if self._router is None:
            router = APIRouter(tags=["admin"])
            for admin in self.admins.values():
                router.include_router(admin.resource)
            router.add_api_route(
                "/meta",
                self.meta,
                methods=["GET"],
                name="admin_meta",
                summary="What the admin serves, as far as the caller may use it",
                response_model=AdminMeta,
                dependencies=[Depends(Require(STAFF))],
                responses=error_responses(
                    status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN
                ),
            )
            self._router = router
        return self._router

    async def meta(self, request: Request) -> "AdminMeta":
        """Each model the caller may view: schemas, filters, and what they may run."""
        resources = []
        for admin in self.admins.values():
            described = await _describe(request, admin)
            if described is not None:
                resources.append(described)
        return AdminMeta(resources=resources)


class OperationOut(BaseModel):
    """A command or query the caller may run."""

    name: str
    method: str
    path: str
    summary: str | None
    detail: bool
    """Whether it acts on one item, whose id the path takes."""
    body: dict[str, JsonValue] | None
    """The JSON Schema of its body, if it takes one."""
    parameters: dict[str, JsonValue] | None
    """The JSON Schema of its query string, if it reads one."""


class FilterOut(BaseModel):
    """A query parameter that filters lists."""

    parameter: str
    field: str
    lookup: str


class ResourceOut(BaseModel):
    """One model as the caller may use it."""

    label: str
    """Its content type: ``catalog.product``."""
    path: str
    item_path: str
    item_schema: dict[str, JsonValue]
    """The JSON Schema of an item, without the fields the caller may not see."""
    list_fields: list[str]
    filters: list[FilterOut]
    ordering: list[str]
    default_ordering: list[str]
    search: bool
    operations: list[OperationOut]
    history_path: str | None


class AdminMeta(BaseModel):
    """What the admin serves, for a generic client."""

    resources: list[ResourceOut] = Field(default_factory=list)


async def _describe(request: Request, admin: ModelAdmin) -> ResourceOut | None:
    """``admin``'s model as the caller may use it; None if they may not view it."""
    resource = admin.resource
    user = current_user(request)

    async def allowed(rules: Sequence[Rule]) -> bool:
        return await AllOf(*rules).has_permission(request, user)

    if not await allowed(rules_for(resource.rule, VIEW)):
        return None
    hidden = await resource.policy.hidden(request) if resource.policy else frozenset()
    schema = resource.schema.model_json_schema()
    schema["properties"] = {
        name: value
        for name, value in schema["properties"].items()
        if name not in hidden
    }
    schema["required"] = [name for name in schema["required"] if name not in hidden]
    filters = admin.filters
    history = admin.history_route
    listing = f"{resource.name_prefix}list_{resource.plural}"
    standard = {listing, resource.read_name, history}
    operations = [
        OperationOut(
            name=each.name,
            method=each.method,
            path=_path(request, each),
            summary=each.summary,
            detail=each.detail,
            body=None if each.body is None else each.body.model_json_schema(),
            parameters=(
                None if each.parameters is None else each.parameters.model_json_schema()
            ),
        )
        for each in resource.operations
        if each.name not in standard and await allowed(each.rules)
    ]
    history_rules = (*rules_for(resource.rule, VIEW), HasPermissions(VIEW_HISTORY))
    return ResourceOut(
        label=".".join(content_type_of(admin.label or admin.model)),
        path=_path(request, _operation(resource, listing)),
        item_path=_path(request, _operation(resource, resource.read_name)),
        item_schema=schema,
        list_fields=[name for name in admin.listed if name not in hidden],
        filters=[]
        if filters is None
        else [
            FilterOut(parameter=parameter, field=each.field or "", lookup=lookup)
            for parameter, (each, lookup, _) in filters.filters.items()
        ],
        ordering=[] if filters is None else list(filters.ordering_fields),
        default_ordering=[] if filters is None else list(filters.default_ordering),
        search=filters is not None and bool(filters.search_fields),
        operations=operations,
        history_path=(
            _path(request, _operation(resource, history))
            if history is not None and await allowed(history_rules)
            else None
        ),
    )


def _operation(resource: ResourceRouter, name: str) -> Operation:
    """The resource's operation ``name``."""
    return next(each for each in resource.operations if each.name == name)


def _path(request: HTTPConnection, operation: Operation) -> str:
    """The operation's path in this application, its parameters as ``{name}``."""
    names = PARAMETER.findall(operation.path)
    path = request.app.url_path_for(
        operation.name, **{name: f"{{{name}}}" for name in names}
    )
    return f"{request.scope.get('root_path', '')}{path}"


def _add_history(resource: ResourceRouter, field: str) -> str:
    """Add ``GET /{id}/history``: the audit trail's entries about one item."""
    from domainkit.contrib.audit.api import HistoryPageOut, read_history
    from domainkit.contrib.audit.history import MAX_LIMIT

    rules = (*rules_for(resource.rule, VIEW), HasPermissions(VIEW_HISTORY))
    name = f"{resource.name_prefix}{resource.basename}_history"

    async def history(**values: Any) -> HistoryPageOut:  # noqa: ANN401 - the parameters.
        request: Request = values["request"]
        identifier = values[resource.lookup]
        await resource.fetch(request, values["uow"], identifier)
        return await read_history(
            current_user(request),
            target=None,
            field=field,
            id=str(identifier),
            limit=values["limit"],
            cursor=values["cursor"],
        )

    keyword = inspect.Parameter.KEYWORD_ONLY
    history.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        [
            inspect.Parameter("request", keyword, annotation=Request),
            inspect.Parameter(
                "uow",
                keyword,
                annotation=Annotated[Any, Depends(resource.unit_of_work)],
            ),
            inspect.Parameter(
                resource.lookup, keyword, annotation=Annotated[resource.id_type, Path()]
            ),
            inspect.Parameter(
                "limit",
                keyword,
                annotation=Annotated[int, QueryParameter(ge=1, le=MAX_LIMIT)],
                default=HISTORY_PAGE,
            ),
            inspect.Parameter(
                "cursor",
                keyword,
                annotation=Annotated[
                    str | None,
                    QueryParameter(
                        max_length=MAX_CURSOR_LENGTH, pattern=CURSOR_PATTERN
                    ),
                ],
                default=None,
            ),
        ]
    )
    path = f"/{{{resource.lookup}}}/history"
    resource.add_api_route(
        path,
        history,
        methods=["GET"],
        name=name,
        summary=f"The history of a {resource.basename.replace('_', ' ')}",
        response_model=HistoryPageOut,
        dependencies=[Depends(Require(*rules))],
        responses=error_responses(
            status.HTTP_401_UNAUTHORIZED,
            status.HTTP_403_FORBIDDEN,
            status.HTTP_404_NOT_FOUND,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
        ),
    )
    resource.operations.append(
        Operation(
            name=name,
            method="GET",
            path=f"{resource.prefix}{path}",
            action=VIEW,
            rules=rules,
            summary=None,
            detail=True,
            body=None,
            parameters=None,
        )
    )
    return name
