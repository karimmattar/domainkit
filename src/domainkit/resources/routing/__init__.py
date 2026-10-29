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

The router is a facade over one module per kind of route (``listing``,
``reading``, ``commands``, ``batching``, ``queries``, ``crud``), each a
class that declares its route (``declaring.add_route``) and answers each
request from a typed ``RouteContext``; what they share is in ``reach``
(what a caller may reach) and ``presenting`` (how aggregates are shown).
A new kind of route is a module of that shape and a method here (ADR 0025).
"""

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter
from fastapi.routing import APIRoute
from pydantic import BaseModel
from starlette.requests import HTTPConnection
from starlette.routing import BaseRoute

from domainkit.ddd.fields import fields_of
from domainkit.ddd.messages import Command
from domainkit.ddd.messages import Query as QueryMessage
from domainkit.ddd.uow import Retry, UnitOfWork
from domainkit.http.access import Rule, check_object_permissions
from domainkit.http.filters import FilterSet
from domainkit.http.middleware import body_limit
from domainkit.http.pagination import Pagination
from domainkit.utils.text import pluralize, snake_case

from ..access import VIEW, Actions
from ..batches import MAX_ITEMS
from ..policies import FieldPolicy
from ..renderers import FORMATS, JSON
from ..schemas import PatchModel, schema_for
from . import presenting, reach
from .batching import BatchRoute
from .commands import CommandRoute
from .context import ALL_FIELDS as ALL_FIELDS
from .context import BODY_KINDS
from .context import DEFAULT as DEFAULT
from .context import MERGE_PATCH as MERGE_PATCH
from .context import PROVIDED as PROVIDED
from .context import RESERVED as RESERVED
from .context import Aggregate as Aggregate
from .context import Handler as Handler
from .context import Operation as Operation
from .context import RepositoryOf as RepositoryOf
from .context import Returns as Returns
from .context import RouteContext as RouteContext
from .context import Scope as Scope
from .context import Service as Service
from .context import UnitOfWorkFactory as UnitOfWorkFactory
from .crud import CrudRoutes
from .declaring import awaited, unit_of_work_dependency
from .listing import EXPORT_CHUNK as EXPORT_CHUNK
from .listing import ListRoute
from .queries import QueryRoute
from .reading import ReadRoute
from .values import to_domain as to_domain


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
        filters: type[FilterSet] | Callable[..., object] | None = None,
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
        writable: Sequence[str] | str | None = None,
        new_id: Callable[[], object] = uuid4,
        not_found: Callable[[Any], Exception] | None = None,
        list_fields: Sequence[str] | None = None,
        name_prefix: str = "",
        exports: Sequence[str] = (),
        max_export: int = 10_000,
        body_limits: Mapping[str, int] | None = None,
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
        ``list_fields`` keeps only those fields of the schema in lists.
        ``name_prefix`` starts every route's name, hence its operation id,
        so two resources of one model can live in one application.
        ``exports`` (``"csv"``, ``"ndjson"``) lets the listing answer in
        those media types, by ``Accept``: every match, up to ``max_export``
        of them, streamed.

        ``crud=True`` writes the fields ``writable`` names (or every plain
        one, with ``ALL_FIELDS``), or takes the ``create`` and ``update``
        schemas given; without either it is refused. ``body_limits`` gives
        each kind of route that takes a body (``create``, ``update``,
        ``command``, ``batch``) a body limit in bytes of its own
        (``domainkit.http.middleware.body_limit``); ``command_route`` and
        ``batch_route`` take ``body_limit=`` per route.

        Raises:
            TypeError: A declaration does not fit the model.
            ValueError: A body limit is out of bounds.
        """
        if not prefix.startswith("/") or prefix.endswith("/"):
            raise TypeError(
                f"A resource's prefix starts with / and does not end with one: "
                f"{prefix!r}."
            )
        if not isinstance(model, type) and (schema is None or basename is None):
            raise TypeError(
                "A resource whose model is resolved later needs a schema and a "
                "basename."
            )
        if unknown := sorted(set(body_limits or {}) - set(BODY_KINDS)):
            raise TypeError(
                f"body_limits= names kinds of route ({', '.join(BODY_KINDS)}), "
                f"not {', '.join(unknown)}."
            )
        for size in (body_limits or {}).values():
            body_limit(size)  # Checks the bound now, not at the first route.
        self.body_limits = dict(body_limits or {})
        """Body limits by kind of route; a kind left out has the global one."""
        if writable is not None and not crud:
            raise TypeError("writable= names what crud=True writes; crud is off.")
        self._model = model
        self.basename = basename or snake_case(model.__name__)
        self.plural = plural or pluralize(self.basename)
        self.lookup = lookup or f"{self.basename}_id"
        if self.lookup in RESERVED or not self.lookup.isidentifier():
            raise TypeError(f"{self.lookup!r} cannot name a resource's id.")
        self.id_type = id_type or _id_type(model)
        self.parent = parent
        self.parent_field = parent_field
        super().__init__(prefix=self._nested(prefix), **options)
        self.unit_of_work = unit_of_work_dependency(unit_of_work)
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
        self.filters_dependency = awaited(filters)
        """``filters`` as FastAPI awaits it: a ``FilterSet`` built on the loop."""
        given = schema or schema_for(model)  # type: ignore[arg-type]
        if policy is not None:
            policy.check(given.model_fields, given.__name__)
            _check_filters(filters, policy)
        self.source_schema = given
        self.schema = presenting.documented(self, given)
        self.present = present or presenting.presenter(given)
        self.list_fields = presenting.listed(self.schema, list_fields)
        """The fields lists show; None: all of them."""
        if unknown := sorted(set(exports) - set(FORMATS)):
            raise TypeError(
                f"Resources export {', '.join(sorted(FORMATS))}, not "
                f"{', '.join(unknown)}."
            )
        self.media_types = (JSON, *(FORMATS[name] for name in exports))
        """What the listing answers in, by ``Accept``; JSON first."""
        self.max_export = max_export
        self.page_schema = page_schema or presenting.page_schema(self)
        self.name_prefix = name_prefix
        self.read_name = f"{name_prefix}read_{self.basename}"
        self.operations: list[Operation] = []
        """Every route, in the order declared."""
        ListRoute(self).declare()
        ReadRoute(self).declare()
        if crud:
            CrudRoutes(self, create, update, new_id, writable).declare()

    def body_limit_of(self, kind: str, own: int | None = None) -> int | None:
        """The body limit of a route of ``kind``: its ``own``, else the kind's."""
        return own if own is not None else self.body_limits.get(kind)

    def _nested(self, prefix: str) -> str:
        """The router's prefix: under the parent's item, for a nested resource.

        Raises:
            TypeError: The parent and its field are not given together, or
                the parent names its id as this resource does.
        """
        parent = self.parent
        if (parent is None) != (self.parent_field is None):
            raise TypeError("A nested resource needs both parent and parent_field.")
        if parent is None:
            return prefix
        if parent.lookup == self.lookup:
            raise TypeError(f"The parent and the resource both use {self.lookup!r}.")
        return f"{parent.prefix}/{{{parent.lookup}}}{prefix}"

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
        returns: Returns = DEFAULT,
        method: Literal["POST", "PUT", "PATCH", "DELETE"] = "POST",
        path: str = "",
        action: str | None = None,
        rule: Rule | None = None,
        status_code: int | None = None,
        name: str | None = None,
        summary: str | None = None,
        provide: Mapping[str, Callable[..., object]] | None = None,
        body_limit: int | None = None,
        retry: Retry | None = None,
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
        ``body_limit`` holds its bodies to that many bytes (default: the
        resource's for commands, else ``MAX_REQUEST_BODY_SIZE``).

        ``retry`` runs the service again, whole, when its transaction loses
        a race (``UnitOfWork.run``): the check that the caller may reach the
        aggregate and ``If-Match`` are made again with it, in its new
        transaction. Declare it for services that change nothing outside
        their unit of work, on a unit of work whose isolation can fail
        (``Isolation.SERIALIZABLE``) or whose locks can deadlock.

        Raises:
            TypeError: The body has fields the command lacks, or the
                command needs fields nothing gives.
        """
        CommandRoute(
            self,
            command,
            service=service,
            body=body,
            returns=returns,
            method=method,
            path=path,
            action=action,
            rule=rule,
            status_code=status_code,
            name=name,
            summary=summary,
            provide=provide,
            body_limit=body_limit,
            retry=retry,
        ).declare()

    def batch_route(
        self,
        command: type[Command],
        *,
        service: Service,
        body: type[BaseModel] | None = None,
        path: str = "/batch",
        action: str | None = None,
        rule: Rule | None = None,
        returns: Returns = DEFAULT,
        max_items: int = MAX_ITEMS,
        name: str | None = None,
        summary: str | None = None,
        provide: Mapping[str, Callable[..., object]] | None = None,
        body_limit: int | None = None,
        retry: Retry | None = None,
    ) -> None:
        """Add ``POST <path>``: ``command`` for each of many items, ``207``.

        Each item is ``body``'s fields, and, for a command with the
        resource's ``lookup`` field, the id of the aggregate it acts on
        (and an ``if_match`` of its own). The request holds 1 to
        ``max_items`` items and ``atomic`` (default false); see
        ``domainkit.resources.batches`` for what each answers. ``action``
        defaults to ``change`` for commands on one aggregate and ``add``
        for the others; pass ``action="delete"`` for deletions, which
        answer ``204`` per item unless ``returns`` says otherwise.
        ``body_limit`` holds its bodies to that many bytes (default: the
        resource's for batches, else ``MAX_REQUEST_BODY_SIZE``).

        ``retry`` runs work that lost a race again, as a command route's
        does (``UnitOfWork.run``): without ``atomic``, the item that lost,
        its reach check and ``If-Match`` made again; with ``atomic``, the
        whole batch, in a new transaction, since the race ended the one it
        ran in. Once the runs are spent, the item answers as without
        ``retry`` (``503 database_busy``). Declare it as for a command
        route: for services that change nothing outside their unit of
        work, on one whose isolation can fail or whose locks can deadlock.

        Raises:
            TypeError: The path, the bound, or the body does not fit the command.
        """
        BatchRoute(
            self,
            command,
            service=service,
            body=body,
            path=path,
            action=action,
            rule=rule,
            returns=returns,
            max_items=max_items,
            name=name,
            summary=summary,
            provide=provide,
            body_limit=body_limit,
            retry=retry,
        ).declare()

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
        QueryRoute(
            self,
            query,
            handler=handler,
            path=path,
            action=action,
            rule=rule,
            name=name,
            summary=summary,
        ).declare()

    def before_items(self, route: BaseRoute) -> None:
        """Put ``route`` before the routes of one item, so its path matches first.

        ``/products/by-owner`` would otherwise be read as the id of
        ``/products/{product_id}``.
        """
        item = f"{self.prefix}/{{{self.lookup}}}"
        first = next(
            index
            for index, each in enumerate(self.routes)
            if isinstance(each, APIRoute) and each.path.startswith(item)
        )
        self.routes.insert(first, route)

    # Reaching aggregates ----------------------------------------------------------

    async def fetch(
        self, request: HTTPConnection, uow: UnitOfWork, id_: object, action: str = VIEW
    ) -> Aggregate:
        """The aggregate ``id_``, as a route of ``action`` would reach it.

        For routes of your own on the resource: what is out of the caller's
        scope is not found, and rules that look at objects are asked.

        Raises:
            TypeError: The resource is nested; its routes take the parent too.
            NotFound: There is none, or it is out of the caller's reach.
            PermissionDenied: A rule refuses the caller this aggregate.
        """
        if self.parent is not None:
            raise TypeError(
                "fetch() reaches aggregates of resources that are not nested."
            )
        async with uow:
            aggregate = await reach.load(self, uow, request, {self.lookup: id_})
        rules = reach.rules_of(self, action)
        if reach.asks_objects(rules):
            await check_object_permissions(request, aggregate, *rules)
        return aggregate


def _id_type(model: type | Callable[[], type]) -> type:
    """The type of the model's ids in paths: ``UUID`` unless the hint says."""
    if not isinstance(model, type):
        return UUID
    kind = fields_of(model)["id"].kind
    return kind if kind in {UUID, int, str} else UUID


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
