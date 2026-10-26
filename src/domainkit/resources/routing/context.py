"""What the routes of a resource share: a request's context, and their declaration.

A route is declared once (``RouteSpec``) and answers each request from a
``RouteContext``: what FastAPI resolved for it (the request, the unit of
work, the ids in the path, the body, the headers that make it conditional),
typed and frozen, so a route kind reads ``context.if_match``, not a string
key of a bag. Batches give each item a context of its own with
``dataclasses.replace``.
"""

import dataclasses
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Final

from fastapi import Request, Response, status
from pydantic import BaseModel
from starlette.requests import HTTPConnection

from domainkit.ddd.model import AggregateRoot
from domainkit.ddd.query import CriteriaLike, Selection
from domainkit.ddd.repository import Repository
from domainkit.ddd.uow import UnitOfWork
from domainkit.http.access import Rule
from domainkit.http.filters import FilterSet
from domainkit.http.pagination import PageRequest

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
MODELS: Final = "domainkit.resources.routing"
"""The module of the schemas routes derive, which OpenAPI names them after
when two share a name: the same since the routes had one module."""

CREATE: Final = "create"
UPDATE: Final = "update"
COMMAND: Final = "command"
BATCH: Final = "batch"
BODY_KINDS: Final = (CREATE, UPDATE, COMMAND, BATCH)
"""The kinds of route that take a body, as ``ResourceRouter(body_limits=)`` names
them: ``crud=True``'s create and change, ``command_route``, ``batch_route``."""
ALL_FIELDS: Final = "__all__"
"""``ResourceRouter(writable=ALL_FIELDS)``: ``crud=True`` writes every plain field."""

type UnitOfWorkFactory = Callable[[], UnitOfWork | Awaitable[UnitOfWork]]
type RepositoryOf = Callable[[Any], Repository[Any, Any]]
type Scope = Callable[[HTTPConnection], Awaitable[CriteriaLike | None]]
type Service = Callable[[Any, Any], Awaitable[Any]]
type Handler = Callable[[Any, Any], Awaitable[Any]]
type Aggregate = AggregateRoot[Any]


class _Default:
    """The resource's own schema, as a command's ``returns``."""


DEFAULT: Final = _Default()

type Returns = type[BaseModel] | _Default | None
"""What a write answers with: the resource's schema, another, or nothing (204)."""


@dataclasses.dataclass(frozen=True, slots=True)
class Operation:
    """One route of a resource, as its rules and a generic client see it."""

    name: str
    """The route's name (its operation id)."""
    method: str
    path: str
    """The path inside the resource's router: ``/products/{product_id}/rename``."""
    action: str
    """The action its rules check: ``view``, ``add``, ``change``, ``delete``, ..."""
    rules: tuple[Rule, ...]
    summary: str | None
    detail: bool
    """Whether it acts on one aggregate, named in the path."""
    body: type[BaseModel] | None
    parameters: type[BaseModel] | None
    """The query string a query route reads."""


@dataclasses.dataclass(frozen=True, slots=True)
class RouteSpec:
    """How one route is declared: its path, its parameters, and its answers."""

    path: str
    """After the resource's prefix: ``""``, ``/{product_id}/rename``."""
    method: str
    name: str
    """Its name, before the resource's ``name_prefix``."""
    summary: str | None
    action: str
    """What its rules check (``ModelActions`` asks for that permission)."""
    rule: Rule | None = None
    """A rule of the route's own, after the resource's."""
    detail: bool = False
    """It acts on one aggregate, whose id is in the path."""
    body: type[BaseModel] | None = None
    asked: type[BaseModel] | None = None
    """The query string it reads into one model (a query route's)."""
    returns: Returns = None
    status_code: int = status.HTTP_200_OK
    listing: bool = False
    """It lists the collection: pages and filters."""
    reads: bool = False
    """It answers one aggregate with its ``ETag`` (``If-None-Match``)."""
    writes: bool = False
    """It changes data (``If-Match`` on one aggregate; 409 and 422)."""
    provided: Mapping[str, Callable[..., object]] = dataclasses.field(
        default_factory=dict
    )
    """Command fields that FastAPI dependencies give, by field."""
    body_limit: int | None = None
    """Its body limit in bytes (``body_limit``); None: ``MAX_REQUEST_BODY_SIZE``."""


@dataclasses.dataclass(frozen=True, slots=True)
class RouteContext:
    """What FastAPI resolved for one request to a route of a resource.

    Built by the endpoint from its parameters, and handed to the route
    kind's ``handle``. Only what the route declared is set.
    """

    request: Request
    uow: UnitOfWork
    """A unit of work from the resource's dependency, not yet entered."""
    ids: Mapping[str, object]
    """The ids in the path, by name: the resource's ``lookup`` on a detail
    route, and the parent's on a nested resource."""
    parent_uow: UnitOfWork | None = None
    """The parent resource's unit of work, for a nested resource."""
    body: BaseModel | None = None
    parameters: BaseModel | None = None
    """A query route's query string."""
    page: PageRequest | None = None
    """The page a listing asks for."""
    filters: "FilterSet | Selection | CriteriaLike | None" = None
    """What the resource's ``filters`` dependency gave: a ``FilterSet``, a
    ``Selection``, or criteria."""
    if_match: str | None = None
    if_none_match: str | None = None
    provided: Mapping[str, object] = dataclasses.field(default_factory=dict)
    """The values of a command's ``provide`` dependencies, by field."""


type Handle = Callable[[RouteContext], Awaitable[Response]]
"""How a route kind answers a request."""
