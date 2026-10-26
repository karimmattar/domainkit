"""Resources: aggregates listed, read, and changed over REST by ``ResourceRouter``."""

import asyncio
import dataclasses
import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Annotated, Any, Final
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, Header, Query, Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Receive, Scope, Send

from domainkit.contrib.auth import AnonymousUser, AnyUser
from domainkit.ddd import (
    UNSET,
    AggregateRoot,
    BusinessRuleViolation,
    Command,
    DomainEvent,
    EventBus,
    InMemoryRepository,
    NotFound,
    Reference,
    UnitOfWork,
    Unset,
)
from domainkit.ddd import Query as QueryMessage
from domainkit.ddd.fields import fields_of
from domainkit.ddd.query import F
from domainkit.ddd.query import Query as Listing
from domainkit.deprecation import DomainkitDeprecationWarning
from domainkit.http import Filter, FilterSet, Pagination
from domainkit.http.access import (
    AllOf,
    AnyOf,
    BasePermission,
    HasObjectPermissions,
    IsAuthenticated,
    Not,
    Public,
    open_routes,
)
from domainkit.http.errors import install_exception_handlers
from domainkit.http.middleware import RequestBodyLimitMiddleware, route_body_limit
from domainkit.resources import (
    ALL_FIELDS,
    MERGE_PATCH,
    FieldPolicy,
    ModelActions,
    PatchModel,
    RecordChanged,
    RecordCreated,
    RecordDeleted,
    ResourceRouter,
    schema_for,
    to_domain,
)
from domainkit.resources.access import HasModelPermission, content_type_of, rules_for
from domainkit.resources.routing.reach import checks_objects
from domainkit_tests.conftest import UseAuth
from domainkit_tests.gadgets import (
    FakeGadgetsUnitOfWork,
    Gadget,
    GadgetId,
    InMemoryGadgets,
    Label,
    make_gadget,
)
from domainkit_tests.test_auth_access import user
from domainkit_tests.test_resource_schemas import Money, Order

GADGETS: Final = "shop.gadget"
LOCATION: Final = "/gadgets/"


class GadgetFilters(FilterSet):
    """Filters, ordering, and search for the gadget listing."""

    model = Gadget
    color = Filter("exact", "in")
    ordering_fields = ("name", "price")
    default_ordering = ("name",)
    search_fields = ("name",)


class MakeGadget(Command):
    """Make a gadget."""

    name: str
    price: Decimal


class RenameGadget(Command):
    """Rename a gadget."""

    gadget_id: UUID
    name: str


class UpdateGadget(Command):
    """Change some fields of a gadget."""

    gadget_id: UUID
    name: str | Unset = UNSET
    stock: int | Unset | None = UNSET


class DropGadget(Command):
    """Delete a gadget."""

    gadget_id: UUID


class TouchGadget(Command):
    """A command whose service never loads the gadget."""

    gadget_id: UUID


class MakeIn(BaseModel):
    """A new gadget's fields."""

    name: str = Field(max_length=30)
    price: Decimal


class RenameIn(BaseModel):
    """A new name."""

    name: str = Field(max_length=30)


class GadgetChanges(PatchModel):
    """What may change about a gadget."""

    name: str = Field(max_length=30)
    stock: int | None


class Summary(BaseModel):
    """Another answer than the resource's schema."""

    name: str


async def make(command: MakeGadget, uow: FakeGadgetsUnitOfWork) -> Gadget:
    """Make and store a gadget."""
    gadget = make_gadget(command.name, price=str(command.price))
    async with uow:
        await uow.gadgets.add(gadget)
        await uow.commit()
    return gadget


async def rename(command: RenameGadget, uow: FakeGadgetsUnitOfWork) -> Gadget:
    """Rename a gadget; the version moves on, as the database's would."""
    async with uow:
        gadget = await uow.gadgets.get(GadgetId(command.gadget_id))
        gadget.rename(command.name)
        gadget.version += 1
        await uow.commit()
    return gadget


async def update(command: UpdateGadget, uow: FakeGadgetsUnitOfWork) -> Gadget:
    """Set the fields the command sets."""
    async with uow:
        gadget = await uow.gadgets.get(GadgetId(command.gadget_id))
        if not isinstance(command.name, Unset):
            gadget.name = command.name
        if not isinstance(command.stock, Unset):
            gadget.stock = command.stock
        await uow.commit()
    return gadget


async def drop(command: DropGadget, uow: FakeGadgetsUnitOfWork) -> None:
    """Delete a gadget."""
    async with uow:
        await uow.gadgets.remove(await uow.gadgets.get(GadgetId(command.gadget_id)))
        await uow.commit()


async def touch(command: TouchGadget, uow: FakeGadgetsUnitOfWork) -> Gadget:
    """Answer with a gadget it never loads through the repository."""
    return make_gadget("untouched")


async def summarize(command: RenameGadget, uow: FakeGadgetsUnitOfWork) -> Summary:
    """Answer with another schema."""
    return Summary(name=command.name)


async def nothing(command: RenameGadget, uow: FakeGadgetsUnitOfWork) -> None:
    """Answer with nothing at all."""


class Users:
    """Middleware: the user ``X-User`` names (anonymous without one), and scopes."""

    def __init__(self, app: ASGIApp, users: dict[str, AnyUser]) -> None:
        """Wrap ``app``."""
        self.app = app
        self.users = users

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Set ``scope["user"]``, and credentials limited to ``X-Scopes`` if sent."""
        if scope["type"] == "http":
            names = dict(scope["headers"])
            name = names.get(b"x-user", b"").decode()
            scope["user"] = self.users.get(name, AnonymousUser())
            if b"x-scopes" in names:
                limited = frozenset(names[b"x-scopes"].decode().split())
                scope["auth"] = SimpleNamespace(scopes=limited)
        await self.app(scope, receive, send)


def everyone() -> dict[str, AnyUser]:
    """Users granted the gadget permissions, by name."""
    return {
        "viewer": user("shop.view_gadget"),
        "changer": user("shop.view_gadget", "shop.change_gadget"),
        "adder": user("shop.add_gadget"),
        "deleter": user("shop.delete_gadget"),
        "costs": user("shop.view_gadget", "shop.view_cost"),
        "stocker": user("shop.view_gadget", "shop.change_gadget", "shop.change_stock"),
        "root": user(is_superuser=True),
    }


type Client = Callable[..., TestClient]


@pytest.fixture
def store() -> InMemoryGadgets:
    """Three gadgets: a, b, c."""
    return InMemoryGadgets(
        [
            make_gadget("b", price="2.00"),
            make_gadget("a", price="3.00"),
            make_gadget("c"),
        ]
    )


def gadgets(
    store: InMemoryGadgets,
    published: list[DomainEvent] | None = None,
    **options: Any,  # noqa: ANN401 - ResourceRouter's options.
) -> ResourceRouter:
    """The gadget resource over ``store``; events go to ``published``."""
    events = EventBus()

    async def collect(event: DomainEvent) -> None:
        if published is not None:
            published.append(event)

    events.subscribe(DomainEvent, collect)
    options.setdefault("rule", ModelActions(GADGETS))
    return ResourceRouter(
        "/gadgets",
        model=Gadget,
        unit_of_work=lambda: FakeGadgetsUnitOfWork(store, events=events),
        repository=lambda uow: uow.gadgets,
        **options,
    )


def client_of(*routers: ResourceRouter) -> TestClient:
    """A client of an application serving ``routers``."""
    app = FastAPI()
    install_exception_handlers(app)
    for router in routers:
        app.include_router(router)
    app.add_middleware(Users, users=everyone())
    return TestClient(app)


def as_(name: str, **headers: str) -> dict[str, str]:
    """Headers that send a request as ``name``."""
    return {"X-User": name, **headers}


def test_lists_page_filter_order_and_search(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """The ``FilterSet`` and ``Pagination`` drive the listing."""
    use_auth()
    client = client_of(
        gadgets(
            store,
            filters=GadgetFilters,
            pagination=Pagination(default_limit=2, max_limit=2),
        )
    )

    first = client.get("/gadgets", params={"limit": 2}, headers=as_("viewer"))
    second = client.get(
        "/gadgets",
        params={"limit": 2, "cursor": first.json()["next_cursor"]},
        headers=as_("viewer"),
    )
    pricey = client.get(
        "/gadgets", params={"ordering": "-price"}, headers=as_("viewer")
    )
    found = client.get("/gadgets", params={"search": "B"}, headers=as_("viewer"))

    assert first.status_code == 200
    assert set(first.json()) == {"items", "next_cursor"}
    assert [item["name"] for item in first.json()["items"]] == ["a", "b"]
    assert [item["name"] for item in second.json()["items"]] == ["c"]
    assert second.json()["next_cursor"] is None
    assert [item["name"] for item in pricey.json()["items"]] == ["a", "b"]
    assert [item["name"] for item in found.json()["items"]] == ["b"]
    assert first.json()["items"][0]["label"] == "A"
    for params in ({"limit": 3}, {"cursor": "!"}, {"color__regex": "x"}):
        refused = client.get("/gadgets", params=params, headers=as_("viewer"))
        assert refused.status_code == 422
    assert (
        client.get("/gadgets", params={"cursor": "e30"}, headers=as_("viewer")).json()[
            "code"
        ]
        == "invalid_cursor"
    )


def test_lists_take_offsets_counts_and_query_dependencies(
    store: InMemoryGadgets,
) -> None:
    """Offset pages, counts, and a plain dependency that returns a query."""

    def cheap(under: Annotated[str | None, Query()] = None) -> Listing | None:
        return (
            None
            if under is None
            else Listing(F("price") < Decimal(under), ordering=["name"])
        )

    resource = gadgets(
        store,
        rule=None,
        dependencies=[Public],
        filters=cheap,
        pagination=Pagination(offset=True, count=True, default_limit=1),
    )
    client = client_of(resource)

    page = client.get("/gadgets", params={"under": "2.50"}).json()
    rest = client.get("/gadgets", params={"under": "2.50", "offset": 1}).json()
    everything = client.get("/gadgets", params={"limit": 5}).json()

    assert set(page) == {"items", "next_offset", "total"}
    assert (
        [item["name"] for item in page["items"]],
        page["next_offset"],
        page["total"],
    ) == (
        ["b"],
        1,
        2,
    )
    assert (rest["next_offset"], len(everything["items"])) == (None, 3)
    assert resource.page_schema.__name__ == "GadgetPage"


def test_items_are_tagged_and_answer_304_while_current(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """An ``ETag`` of the version and representation; ``If-None-Match`` → 304."""
    use_auth()
    client = client_of(gadgets(store))
    gadget = next(iter(store.items.values()))
    gadget.version = 4
    url = f"{LOCATION}{gadget.id}"

    read = client.get(url, headers=as_("viewer"))
    etag = read.headers["etag"]
    unchanged = client.get(url, headers=as_("viewer", **{"If-None-Match": etag}))
    starred = client.get(url, headers=as_("viewer", **{"If-None-Match": "*"}))
    other = client.get(url, headers=as_("viewer", **{"If-None-Match": '"4-0"'}))

    assert read.status_code == 200
    assert read.json()["id"] == str(gadget.id)
    assert read.json()["version"] == 4
    assert etag.startswith('W/"4-')
    assert (unchanged.status_code, unchanged.content, unchanged.headers["etag"]) == (
        304,
        b"",
        etag,
    )
    assert starred.status_code == 304
    assert (other.status_code, other.headers["etag"]) == (200, etag)
    gadget.stock = 7
    assert (
        client.get(url, headers=as_("viewer", **{"If-None-Match": etag})).status_code
        == 200
    )


def test_missing_items_are_not_found(store: InMemoryGadgets, use_auth: UseAuth) -> None:
    """404 with the repository's error, or the resource's own."""
    use_auth()

    def missing(id_: object) -> NotFound:
        return GadgetNotFound("No such gadget.")

    plain = client_of(gadgets(store))
    custom = client_of(gadgets(store, not_found=missing))

    answer = plain.get(f"{LOCATION}{uuid4()}", headers=as_("viewer"))
    mine = custom.get(f"{LOCATION}{uuid4()}", headers=as_("viewer"))
    malformed = plain.get(f"{LOCATION}not-a-uuid", headers=as_("viewer"))

    assert (answer.status_code, answer.json()["code"]) == (404, "not_found")
    assert mine.json() == {"detail": "No such gadget.", "code": "gadget_not_found"}
    assert malformed.status_code == 422


class GadgetNotFound(NotFound):
    """No gadget has that id."""


def test_commands_create_with_location_and_etag(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """A command on the collection: 201, ``Location`` of the new item, its tag."""
    use_auth()
    resource = gadgets(store)
    resource.command_route(MakeGadget, service=make, body=MakeIn)
    client = client_of(resource)

    created = client.post(
        "/gadgets", json={"name": "new", "price": "9.50"}, headers=as_("adder")
    )
    location = created.headers["location"]

    assert created.status_code == 201
    assert created.json()["price"] == "9.50"
    assert location == f"/gadgets/{created.json()['id']}"
    assert created.headers["etag"].startswith('W/"0-')
    assert client.get(location, headers=as_("viewer")).json() == created.json()
    refused = client.post(
        "/gadgets", json={"name": "x", "price": "1"}, headers=as_("viewer")
    )
    assert refused.status_code == 403
    invalid = client.post("/gadgets", json={"name": "x"}, headers=as_("adder"))
    assert (invalid.status_code, invalid.json()["code"]) == (422, "validation_error")


def test_commands_on_one_item_honour_if_match(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """A stale ``If-Match`` is 412; a current one, ``*``, or none go through."""
    use_auth()
    resource = gadgets(store)
    resource.command_route(RenameGadget, service=rename, body=RenameIn, path="/rename")
    client = client_of(resource)
    gadget = next(iter(store.items.values()))
    url = f"{LOCATION}{gadget.id}/rename"
    etag = client.get(f"{LOCATION}{gadget.id}", headers=as_("viewer")).headers["etag"]

    current = client.post(
        url, json={"name": "one"}, headers=as_("changer", **{"If-Match": etag})
    )
    stale = client.post(
        url, json={"name": "two"}, headers=as_("changer", **{"If-Match": etag})
    )
    by_version = client.post(
        url, json={"name": "three"}, headers=as_("changer", **{"If-Match": '"1"'})
    )
    starred = client.post(
        url, json={"name": "four"}, headers=as_("changer", **{"If-Match": "*"})
    )
    unconditional = client.post(url, json={"name": "five"}, headers=as_("changer"))
    garbage = client.post(
        url, json={"name": "six"}, headers=as_("changer", **{"If-Match": "nope"})
    )

    assert (current.status_code, current.json()["name"]) == (200, "one")
    assert current.headers["etag"].startswith('W/"1-')
    assert stale.status_code == 412
    assert stale.json() == {
        "detail": "This was changed since you read it; read it again before "
        "changing it.",
        "code": "precondition_failed",
    }
    assert (by_version.status_code, starred.status_code) == (200, 200)
    assert (unconditional.status_code, unconditional.json()["version"]) == (200, 4)
    assert garbage.status_code == 412
    assert gadget.name == "five"
    missing = client.post(
        f"{LOCATION}{uuid4()}/rename",
        json={"name": "x"},
        headers=as_("changer", **{"If-Match": "*"}),
    )
    assert missing.status_code == 404


def test_resources_may_require_if_match(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """Without ``If-Match``, a write is 428; OpenAPI says so."""
    use_auth()
    resource = gadgets(store, require_if_match=True, crud=True, writable=ALL_FIELDS)
    resource.command_route(RenameGadget, service=rename, body=RenameIn, path="/rename")
    client = client_of(resource)
    gadget = next(iter(store.items.values()))

    refused = client.post(
        f"{LOCATION}{gadget.id}/rename", json={"name": "x"}, headers=as_("changer")
    )
    deleted = client.delete(f"{LOCATION}{gadget.id}", headers=as_("deleter"))
    allowed = client.post(
        f"{LOCATION}{gadget.id}/rename",
        json={"name": "x"},
        headers=as_("changer", **{"If-Match": '"0"'}),
    )
    operation = client.get("/openapi.json").json()["paths"][
        "/gadgets/{gadget_id}/rename"
    ]["post"]

    assert refused.status_code == 428
    assert refused.json() == {
        "detail": "Send If-Match with the ETag of the version you are changing.",
        "code": "precondition_required",
    }
    assert deleted.status_code == 428
    assert allowed.status_code == 200
    assert "428" in operation["responses"]
    header = next(
        item for item in operation["parameters"] if item["name"] == "If-Match"
    )
    assert header["description"].startswith("Required:")


def test_services_that_never_load_the_item_cannot_take_if_match(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """If-Match unchecked is a bug in the service, not a silent success."""
    use_auth()
    resource = gadgets(store)
    resource.command_route(TouchGadget, service=touch, path="/touch")
    client = client_of(resource)
    gadget = next(iter(store.items.values()))

    with pytest.raises(RuntimeError, match="never loaded Gadget"):
        client.post(
            f"{LOCATION}{gadget.id}/touch", headers=as_("changer", **{"If-Match": "*"})
        )
    assert (
        client.post(f"{LOCATION}{gadget.id}/touch", headers=as_("changer")).status_code
        == 200
    )


def test_command_answers(store: InMemoryGadgets, use_auth: UseAuth) -> None:
    """Another schema, none (204), and a ``DELETE`` command."""
    use_auth()
    resource = gadgets(store)
    resource.command_route(
        RenameGadget, service=summarize, body=RenameIn, path="/summary", returns=Summary
    )
    resource.command_route(
        RenameGadget,
        service=nothing,
        body=RenameIn,
        path="/quiet",
        returns=None,
        name="quiet",
    )
    resource.command_route(
        RenameGadget, service=nothing, body=RenameIn, path="/broken", name="broken"
    )
    resource.command_route(DropGadget, service=drop, method="DELETE")
    resource.command_route(
        UpdateGadget,
        service=update,
        body=GadgetChanges,
        method="PATCH",
        returns=resource.source_schema,
    )
    client = client_of(resource)
    gadget = next(iter(store.items.values()))
    item = f"{LOCATION}{gadget.id}"
    name = gadget.name

    summary = client.post(f"{item}/summary", json={"name": "s"}, headers=as_("changer"))
    quiet = client.post(f"{item}/quiet", json={"name": "q"}, headers=as_("changer"))
    patched = client.patch(
        item,
        content=json.dumps({"stock": None}),
        headers=as_("changer", **{"Content-Type": MERGE_PATCH}),
    )
    renamed = client.patch(item, json={"name": "patched"}, headers=as_("changer"))

    assert (summary.status_code, summary.json()) == (200, {"name": "s"})
    assert "etag" not in summary.headers
    assert (quiet.status_code, quiet.content) == (204, b"")
    assert (patched.status_code, patched.json()["stock"]) == (200, None)
    assert patched.json()["name"] == name
    assert renamed.json()["name"] == "patched"
    assert (
        client.patch(item, json={"other": 1}, headers=as_("changer")).status_code == 422
    )
    with pytest.raises(RuntimeError, match="returned nothing"):
        client.post(f"{item}/broken", json={"name": "b"}, headers=as_("changer"))
    assert client.delete(item, headers=as_("changer")).status_code == 403
    assert client.delete(item, headers=as_("deleter")).status_code == 204
    assert gadget.is_deleted


def test_command_declarations_are_checked(store: InMemoryGadgets) -> None:
    """Bodies must fit their commands, and commands must be fully given."""
    resource = gadgets(store)

    with pytest.raises(TypeError, match="RenameGadget has no field price"):
        resource.command_route(RenameGadget, service=rename, body=MakeIn)
    with pytest.raises(TypeError, match="Nothing gives RenameGadget's name"):
        resource.command_route(RenameGadget, service=rename)
    with pytest.raises(TypeError, match="Nothing gives MakeGadget's name, price"):
        resource.command_route(MakeGadget, service=make)


def test_resource_declarations_are_checked(store: InMemoryGadgets) -> None:
    """Prefixes, lazily resolved models, ids, and nesting."""
    stored: dict[str, Any] = {
        "unit_of_work": lambda: FakeGadgetsUnitOfWork(store),
        "repository": lambda uow: uow.gadgets,
    }
    for prefix in ("gadgets", "/gadgets/"):
        with pytest.raises(TypeError, match="prefix"):
            ResourceRouter(prefix, model=Gadget, **stored)
    with pytest.raises(TypeError, match="needs a schema and a basename"):
        ResourceRouter("/x", model=lambda: Gadget, **stored)
    for lookup in ("body", "not an id"):
        with pytest.raises(TypeError, match="cannot name a resource's id"):
            ResourceRouter("/x", model=Gadget, lookup=lookup, **stored)
    with pytest.raises(TypeError, match="both parent and parent_field"):
        ResourceRouter("/x", model=Gadget, parent_field="x", **stored)
    with pytest.raises(TypeError, match="both use 'gadget_id'"):
        ResourceRouter(
            "/x", model=Gadget, parent=gadgets(store), parent_field="name", **stored
        )


def test_resources_without_a_rule_are_open_routes(store: InMemoryGadgets) -> None:
    """Deny by default: every route of a resource without a rule is listed."""
    open_resource = gadgets(store, rule=None)
    open_resource.command_route(
        RenameGadget, service=rename, body=RenameIn, path="/rename"
    )
    app = FastAPI()
    app.include_router(open_resource)

    assert open_routes(app) == [
        "GET /gadgets",
        "GET /gadgets/{gadget_id}",
        "POST /gadgets/{gadget_id}/rename",
    ]
    guarded = FastAPI()
    guarded.include_router(gadgets(store))
    marked = FastAPI()
    marked.include_router(gadgets(store, rule=None, dependencies=[Public]))
    assert open_routes(guarded) == open_routes(marked) == []


def test_model_actions_ask_for_each_routes_permission(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """``view`` to read, ``add`` to create, ``change`` for commands, ``delete``."""
    use_auth()
    resource = gadgets(store, crud=True, writable=ALL_FIELDS)
    resource.command_route(RenameGadget, service=rename, body=RenameIn, path="/rename")
    resource.command_route(
        RenameGadget,
        service=rename,
        body=RenameIn,
        path="/publish",
        action="publish",
        name="publish",
    )
    client = client_of(resource)
    gadget = next(iter(store.items.values()))
    item = f"{LOCATION}{gadget.id}"
    new = {
        "name": "n",
        "label": "N",
        "price": "1.00",
        "made_at": "2026-01-01T00:00:00Z",
    }
    calls = [
        ("GET", "/gadgets", None, "viewer"),
        ("GET", item, None, "viewer"),
        ("POST", f"{item}/rename", {"name": "x"}, "changer"),
        ("PATCH", item, {"stock": 1}, "changer"),
        ("POST", "/gadgets", new, "adder"),
        ("DELETE", item, None, "deleter"),
    ]
    for method, url, body, allowed in calls:
        anonymous = client.request(method, url, json=body)
        refused = client.request(
            method,
            url,
            json=body,
            headers=as_("costs" if allowed != "viewer" else "adder"),
        )
        assert (anonymous.status_code, refused.status_code) == (401, 403), (method, url)
        assert (
            client.request(method, url, json=body, headers=as_(allowed)).status_code
            < 300
        )
    assert (
        client.post(
            f"{LOCATION}{uuid4()}/publish", json={"name": "x"}, headers=as_("changer")
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"{LOCATION}{uuid4()}/publish", json={"name": "x"}, headers=as_("root")
        ).status_code
        == 404
    )


def test_rules_that_look_at_objects_see_each_item(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """Object rules are asked about the item before reads and commands."""
    use_auth()

    class OwnsA(BasePermission):
        async def has_object_permission(
            self, request: HTTPConnection, user: AnyUser, obj: object
        ) -> bool:
            return getattr(obj, "name", "") == "a"

    resource = gadgets(store, rule=AllOf(IsAuthenticated(), OwnsA()))
    resource.command_route(RenameGadget, service=rename, body=RenameIn, path="/rename")
    client = client_of(resource)
    by_name = {gadget.name: gadget for gadget in store.items.values()}

    assert (
        client.get(f"{LOCATION}{by_name['a'].id}", headers=as_("viewer")).status_code
        == 200
    )
    assert (
        client.get(f"{LOCATION}{by_name['b'].id}", headers=as_("viewer")).status_code
        == 403
    )
    assert (
        client.post(
            f"{LOCATION}{by_name['b'].id}/rename",
            json={"name": "z"},
            headers=as_("viewer"),
        ).status_code
        == 403
    )
    assert by_name["b"].name == "b"
    assert (
        client.post(
            f"{LOCATION}{by_name['a'].id}/rename",
            json={"name": "z"},
            headers=as_("viewer"),
        ).status_code
        == 200
    )


def test_which_rules_look_at_objects() -> None:
    """Composite rules look at objects when a rule inside them does."""
    plain = IsAuthenticated()
    owner = HasObjectPermissions("shop.change_gadget")

    assert not checks_objects(plain)
    assert not checks_objects(IsAuthenticated)
    assert checks_objects(owner)
    assert checks_objects(AnyOf(plain, owner))
    assert checks_objects(Not(owner))
    assert not checks_objects(AllOf(plain, Not(plain)))
    assert rules_for(None, "view") == ()
    assert rules_for(plain, "view") == (plain,)
    model_rules = rules_for(ModelActions(GADGETS, plain), "add")
    assert model_rules[0] is plain
    assert isinstance(model_rules[1], HasModelPermission)
    assert model_rules[1].permission() == "shop.add_gadget"
    assert content_type_of("billing.invoice") == ("billing", "invoice")


def test_scopes_narrow_lists_and_hide_items(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """Out of scope is absent from lists and 404 for items and commands."""
    use_auth()

    async def cheap_only(request: Any) -> Any:  # noqa: ANN401 - a connection, criteria.
        return (
            F("price") < Decimal("2.50")
            if request.headers.get("x-user") == "viewer"
            else None
        )

    resource = gadgets(store, scope=cheap_only)
    resource.command_route(RenameGadget, service=rename, body=RenameIn, path="/rename")
    client = client_of(resource)
    by_name = {gadget.name: gadget for gadget in store.items.values()}

    listed = client.get("/gadgets", headers=as_("viewer")).json()["items"]
    everything = client.get("/gadgets", headers=as_("changer")).json()["items"]
    hidden = client.get(f"{LOCATION}{by_name['a'].id}", headers=as_("viewer"))
    renamed = client.post(
        f"{LOCATION}{by_name['a'].id}/rename", json={"name": "z"}, headers=as_("viewer")
    )

    assert sorted(item["name"] for item in listed) == ["b", "c"]
    assert len(everything) == 3
    assert hidden.status_code == 404
    assert renamed.status_code in {403, 404}
    assert by_name["a"].name == "a"


class Review(AggregateRoot[UUID]):
    """An aggregate that references a gadget by id."""

    gadget: Reference[GadgetId]
    text: str


class WriteReview(Command):
    """Review a gadget."""

    gadget: UUID
    text: str


class ReviewIn(BaseModel):
    """A review's text."""

    text: str


def test_nested_resources_live_under_their_parent(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """``/gadgets/{gadget_id}/reviews``: lists, items, and commands of one parent."""
    use_auth()
    parent = gadgets(store)
    gadget, other = list(store.items.values())[:2]
    reviews = InMemoryRepository[Review, UUID](
        [
            Review(id=uuid4(), gadget=gadget.id, text="good"),
            Review(id=uuid4(), gadget=other.id, text="bad"),
        ]
    )

    class Reviews(UnitOfWork):
        def __init__(self) -> None:
            super().__init__(events=EventBus())
            self.reviews = reviews

        async def _commit(self) -> None:
            """Nothing to write."""

        async def _rollback(self) -> None:
            """Nothing to undo."""

    async def write(command: WriteReview, uow: Reviews) -> Review:
        review = Review(id=uuid4(), gadget=GadgetId(command.gadget), text=command.text)
        async with uow:
            await uow.reviews.add(review)
            await uow.commit()
        return review

    nested = ResourceRouter(
        "/reviews",
        model=Review,
        unit_of_work=Reviews,
        repository=lambda uow: uow.reviews,
        parent=parent,
        parent_field="gadget",
        rule=IsAuthenticated(),
        links=True,
    )
    nested.command_route(WriteReview, service=write, body=ReviewIn)
    client = client_of(parent, nested)
    base = f"/gadgets/{gadget.id}/reviews"
    others = [r for r in reviews.items.values() if r.gadget == other.id]

    listed = client.get(base, headers=as_("viewer")).json()["items"]
    elsewhere = client.get(f"{base}/{others[0].id}", headers=as_("viewer"))
    created = client.post(base, json={"text": "new"}, headers=as_("viewer"))
    one = client.get(created.headers["location"], headers=as_("viewer"))

    assert [item["text"] for item in listed] == ["good"]
    assert listed[0]["links"] == {
        "self": f"{base}/{listed[0]['id']}",
        "parent": f"/gadgets/{gadget.id}",
    }
    assert elsewhere.status_code == 404
    assert created.status_code == 201
    assert created.headers["location"].startswith(f"{base}/")
    assert one.json()["gadget"] == str(gadget.id)
    schema = client.get("/openapi.json").json()["components"]["schemas"]["ReviewOut"]
    assert "links" in schema["required"]


def test_crud_creates_changes_and_deletes_reference_data(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """``crud=True``: generic routes that record what they did."""
    use_auth()
    published: list[DomainEvent] = []
    client = client_of(gadgets(store, published, crud=True, writable=ALL_FIELDS))
    new = {
        "name": "new",
        "label": "NEW",
        "price": "2.50",
        "made_at": "2026-01-01T00:00:00Z",
    }

    created = client.post("/gadgets", json=new, headers=as_("adder"))
    item = created.headers["location"]
    patched = client.patch(
        item,
        content=json.dumps({"stock": 5, "label": "X", "color": "blue"}),
        headers=as_("changer", **{"Content-Type": MERGE_PATCH}),
    )
    same = client.patch(item, json={"stock": 5}, headers=as_("changer"))
    deleted = client.delete(item, headers=as_("deleter"))

    assert created.status_code == 201
    assert created.json()["label"] == "NEW"
    assert (patched.status_code, patched.json()["stock"], patched.json()["color"]) == (
        200,
        5,
        "blue",
    )
    assert same.status_code == 200
    assert deleted.status_code == 204
    assert client.get(item, headers=as_("viewer")).status_code == 404
    record_id = created.json()["id"]
    assert [type(event) for event in published] == [
        RecordCreated,
        RecordChanged,
        RecordDeleted,
    ]
    assert published[0] == RecordCreated(
        model="Gadget",
        record_id=record_id,
        fields=(
            "name",
            "label",
            "price",
            "made_at",
            "stock",
            "active",
            "color",
            "size",
        ),
        event_id=published[0].event_id,
        occurred_at=published[0].occurred_at,
    )
    assert published[1].fields == ("label", "stock", "color")  # type: ignore[attr-defined]
    invalid = client.post(
        "/gadgets", json={**new, "label": "X" * 21}, headers=as_("adder")
    )
    assert (invalid.status_code, invalid.json()["code"]) == (422, "validation_error")
    bodies: list[dict[str, Any]] = [{"name": None}, {"parts": []}, {"id": "x"}]
    for body in bodies:
        assert (
            client.patch(
                f"{LOCATION}{record_id}", json=body, headers=as_("changer")
            ).status_code
            == 422
        )


def test_crud_declarations_are_checked(store: InMemoryGadgets) -> None:
    """Generic routes set plain fields, and creating needs every required one."""

    class PartsIn(BaseModel):
        parts: list[str]

    class NameOnly(BaseModel):
        name: str

    with pytest.raises(TypeError, match="not parts"):
        gadgets(store, crud=True, writable=ALL_FIELDS, create=PartsIn)
    with pytest.raises(TypeError, match="lacks Gadget's label, made_at, price"):
        gadgets(store, crud=True, writable=ALL_FIELDS, create=NameOnly)


def test_values_become_domain_values() -> None:
    """Value objects of several fields are built from their parts."""
    order = fields_of(Order)
    assert to_domain(
        order["total"], {"amount": Decimal(1), "currency": "EUR"}
    ) == Money(Decimal(1), "EUR")
    assert to_domain(order["code"], "A") == order["code"].kind("A")  # type: ignore[misc]
    assert to_domain(order["paid_at"], None) is None
    assert to_domain(fields_of(Gadget)["label"], "x") == Label("x")


def test_field_policies_hide_and_guard_fields(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """Hidden fields are absent; fields guarded by a permission refuse others."""
    use_auth()
    policy = FieldPolicy(
        read_only=["active"],
        visible={"price": "shop.view_cost"},
        editable={"stock": "shop.change_stock"},
    )
    resource = gadgets(store, crud=True, writable=ALL_FIELDS, policy=policy)
    resource.command_route(
        UpdateGadget, service=update, body=GadgetChanges, method="PATCH", path="/fields"
    )
    client = client_of(resource)
    gadget = next(iter(store.items.values()))
    item = f"{LOCATION}{gadget.id}"

    seen = client.get(item, headers=as_("viewer")).json()
    priced = client.get(item, headers=as_("costs")).json()
    listed = client.get("/gadgets", headers=as_("viewer")).json()["items"]
    refused = client.patch(item, json={"stock": 3}, headers=as_("changer"))
    allowed = client.patch(item, json={"stock": 3}, headers=as_("stocker"))
    by_command = client.patch(
        f"{item}/fields", json={"stock": 4}, headers=as_("changer")
    )
    renamed = client.patch(
        f"{item}/fields", json={"name": "ok"}, headers=as_("changer")
    )
    fixed = client.patch(item, json={"active": False}, headers=as_("root"))

    assert "price" not in seen
    assert priced["price"] == str(gadget.price)
    assert all("price" not in each for each in listed)
    assert (refused.status_code, refused.json()["code"]) == (403, "field_not_editable")
    assert (
        refused.json()["detail"]
        == "You do not have permission to change some of these fields."
    )
    assert (
        allowed.status_code,
        allowed.json()["stock"],
        "price" in allowed.json(),
    ) == (200, 3, False)
    assert by_command.status_code == 403
    assert renamed.status_code == 200
    assert fixed.status_code == 422
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    assert "price" not in schemas["GadgetOut"]["required"]
    assert schemas["GadgetOut"]["properties"]["price"]["description"] == (
        "Shown only to users granted shop.view_cost."
    )
    assert schemas["GadgetPatch"]["properties"]["stock"]["description"] == (
        "Changed only by users granted shop.change_stock."
    )
    assert "active" not in schemas["GadgetPatch"]["properties"]
    assert "active" not in schemas["GadgetIn"]["properties"]
    assert schemas["GadgetPatch"]["properties"]["price"]["description"] == (
        "Changed only by users granted shop.view_cost."
    )


def test_read_only_fields_cannot_be_patched_by_commands(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """A ``PatchModel`` that names a read-only field is refused when it sets it."""
    use_auth()

    class StockPatch(PatchModel):
        stock: int | None

    resource = gadgets(store, policy=FieldPolicy(read_only=["stock"]))
    resource.command_route(
        UpdateGadget, service=update, body=StockPatch, method="PATCH"
    )
    client = client_of(resource)
    gadget = next(iter(store.items.values()))

    refused = client.patch(
        f"{LOCATION}{gadget.id}", json={"stock": 1}, headers=as_("root")
    )

    assert (refused.status_code, refused.json()["detail"]) == (
        403,
        "Some of these fields cannot be changed.",
    )


def test_openapi_documents_each_route(store: InMemoryGadgets) -> None:
    """Names, errors, conditional headers, and merge patches."""
    resource = gadgets(store, crud=True, writable=ALL_FIELDS, filters=GadgetFilters)
    resource.command_route(RenameGadget, service=rename, body=RenameIn, path="/rename")
    resource.command_route(MakeGadget, service=make, body=MakeIn, path="/make")
    client = client_of(resource)

    spec = client.get("/openapi.json").json()
    paths = spec["paths"]
    listing = paths["/gadgets"]["get"]
    read = paths["/gadgets/{gadget_id}"]["get"]
    patch = paths["/gadgets/{gadget_id}"]["patch"]
    rename_route = paths["/gadgets/{gadget_id}/rename"]["post"]
    made = paths["/gadgets/make"]["post"]

    assert listing["operationId"].startswith("list_gadgets")
    assert read["operationId"].startswith("read_gadget")
    assert rename_route["operationId"].startswith("rename_gadget")
    assert (listing["summary"], read["summary"], rename_route["summary"]) == (
        "List gadgets",
        "Read a gadget",
        "Rename a gadget.",
    )
    assert sorted(listing["responses"]) == ["200", "401", "403", "422"]
    assert sorted(read["responses"]) == ["200", "304", "401", "403", "404", "422"]
    assert sorted(rename_route["responses"]) == [
        "200",
        "401",
        "403",
        "404",
        "409",
        "412",
        "422",
    ]
    assert sorted(made["responses"]) == ["201", "401", "403", "409", "422"]
    assert {item["name"] for item in read["parameters"]} == {
        "gadget_id",
        "If-None-Match",
    }
    assert "If-Match" in {item["name"] for item in rename_route["parameters"]}
    merge = patch["requestBody"]["content"][MERGE_PATCH]["schema"]
    assert merge["title"] == "GadgetPatch"
    assert set(merge["properties"]) == {
        "name", "label", "price", "made_at", "stock", "active", "color", "size"
    }  # fmt: skip
    assert merge["properties"]["color"] == {"$ref": "#/components/schemas/Color"}
    assert "GadgetPatch" in spec["components"]["schemas"]
    assert listing["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/GadgetPage"
    }


def test_unit_of_work_classes_are_called_without_query_parameters(
    store: InMemoryGadgets,
) -> None:
    """A class as ``unit_of_work`` is wrapped, so its arguments never reach OpenAPI."""

    class Gadgets(FakeGadgetsUnitOfWork):
        def __init__(self, readonly: bool | None = None) -> None:
            super().__init__(store, readonly=readonly)

    resource = ResourceRouter(
        "/gadgets",
        model=Gadget,
        unit_of_work=Gadgets,
        repository=lambda uow: uow.gadgets,
        dependencies=[Public],
    )
    client = client_of(resource)

    listing = client.get("/openapi.json").json()["paths"]["/gadgets"]["get"]
    assert "readonly" not in {item["name"] for item in listing["parameters"]}
    assert len(client.get("/gadgets", params={"readonly": "true"}).json()["items"]) == 3


def test_ids_of_other_types_are_path_parameters_of_their_type() -> None:
    """Integer ids are integers in the path; unknown kinds fall back to UUIDs."""

    class Numbered(AggregateRoot[int]):
        name: str

    class Keyed(AggregateRoot[tuple[int, int]]):
        name: str

    store = InMemoryRepository[Numbered, int]([Numbered(id=7, name="seven")])

    class Numbers(UnitOfWork):
        numbers = store

        async def _commit(self) -> None:
            """Nothing."""

        async def _rollback(self) -> None:
            """Nothing."""

    resource = ResourceRouter(
        "/numbers",
        model=Numbered,
        unit_of_work=Numbers,
        repository=lambda uow: uow.numbers,
        dependencies=[Public],
    )
    keyed = ResourceRouter(
        "/keys",
        model=Keyed,
        unit_of_work=Numbers,
        repository=lambda uow: uow.numbers,
        dependencies=[Public],
    )
    client = client_of(resource)

    assert client.get("/numbers/7").json()["name"] == "seven"
    assert client.get("/numbers/x").status_code == 422
    assert (resource.id_type, keyed.id_type) == (int, UUID)


def test_lazily_resolved_models_take_their_schema(store: InMemoryGadgets) -> None:
    """A model resolved per request (a swappable user) works once given a schema."""
    schema = schema_for(Gadget, include=["id", "name"])
    resource = ResourceRouter(
        "/later",
        model=lambda: Gadget,
        schema=schema,
        basename="gadget",
        unit_of_work=lambda: FakeGadgetsUnitOfWork(store),
        repository=lambda uow: uow.gadgets,
        dependencies=[Public],
    )
    client = client_of(resource)
    gadget = next(iter(store.items.values()))

    assert resource.model is Gadget
    assert resource.lookup == "gadget_id"
    assert client.get(f"/later/{gadget.id}").json() == {
        "id": str(gadget.id),
        "name": gadget.name,
    }


def test_answers_in_other_schemas_take_attributes(store: InMemoryGadgets) -> None:
    """A plain schema is filled from the aggregate's attributes."""

    class Brief(BaseModel):
        name: str
        price: Decimal

    resource = gadgets(store, rule=None, dependencies=[Public], schema=Brief)
    resource.command_route(RenameGadget, service=rename, body=RenameIn, path="/rename")
    client = client_of(resource)
    gadget = next(iter(store.items.values()))

    price = str(gadget.price)
    assert client.get(f"{LOCATION}{gadget.id}").json() == {
        "name": gadget.name,
        "price": price,
    }
    renamed = client.post(f"{LOCATION}{gadget.id}/rename", json={"name": "q"})
    assert renamed.json() == {"name": "q", "price": price}


def test_business_rules_answer_as_domain_errors(store: InMemoryGadgets) -> None:
    """A service's domain error is its usual answer."""

    class Refused(BusinessRuleViolation):
        """No renaming today."""

    async def refuse(command: RenameGadget, uow: FakeGadgetsUnitOfWork) -> Gadget:
        raise Refused("Not today.")

    resource = gadgets(store, rule=None, dependencies=[Public])
    resource.command_route(RenameGadget, service=refuse, body=RenameIn, path="/rename")
    client = client_of(resource)
    gadget = next(iter(store.items.values()))

    refused = client.post(f"{LOCATION}{gadget.id}/rename", json={"name": "x"})

    assert (refused.status_code, refused.json()["code"]) == (422, "refused")


class Part(BaseModel):
    """A part, as a request gives it."""

    sku: str


class Assemble(Command):
    """Change a gadget's parts."""

    gadget_id: UUID
    main: dict[str, str] | None = None
    parts: list[dict[str, str]] | None = None


class AssemblyPatch(PatchModel):
    """Parts to set."""

    main: Part
    parts: list[Part]


def test_partial_changes_give_nested_models_as_dicts(store: InMemoryGadgets) -> None:
    """A command gets plain dicts for nested models, as a whole body gives them."""
    seen: list[Assemble] = []

    async def assemble(command: Assemble, uow: FakeGadgetsUnitOfWork) -> Gadget:
        seen.append(command)
        async with uow:
            return await uow.gadgets.get(GadgetId(command.gadget_id))

    resource = gadgets(store, rule=None, dependencies=[Public])
    resource.command_route(
        Assemble, service=assemble, body=AssemblyPatch, method="PATCH", path="/parts"
    )
    client = client_of(resource)
    gadget = next(iter(store.items.values()))

    client.patch(f"{LOCATION}{gadget.id}/parts", json={"main": {"sku": "m"}})
    client.patch(f"{LOCATION}{gadget.id}/parts", json={"parts": [{"sku": "p"}]})

    assert [(each.main, each.parts) for each in seen] == [
        ({"sku": "m"}, None),
        (None, [{"sku": "p"}]),
    ]


def test_hand_written_schemas_show_themselves(store: InMemoryGadgets) -> None:
    """A schema's own ``from_domain`` shows the aggregate; links need no parent."""

    class Shown(BaseModel):
        title: str

        @classmethod
        def from_domain(cls, gadget: Gadget) -> "Shown":
            return cls(title=gadget.name.upper())

    resource = gadgets(
        store,
        rule=None,
        dependencies=[Public],
        schema=Shown,
        links=True,
        policy=FieldPolicy(read_only=["title"]),
    )
    client = client_of(resource)
    gadget = next(iter(store.items.values()))

    shown = client.get(f"{LOCATION}{gadget.id}").json()

    assert shown == {
        "title": gadget.name.upper(),
        "links": {"self": f"{LOCATION}{gadget.id}"},
    }


def test_generic_writes_never_set_ids_or_versions(store: InMemoryGadgets) -> None:
    """A schema of one's own may not set what the domain and database set."""

    class VersionPatch(PatchModel):
        version: int

    class WithId(BaseModel):
        id: UUID
        name: str

    with pytest.raises(TypeError, match="not version"):
        gadgets(store, crud=True, writable=ALL_FIELDS, update=VersionPatch)
    with pytest.raises(TypeError, match="not id"):
        gadgets(store, crud=True, writable=ALL_FIELDS, create=WithId)


def test_policies_refuse_filters_on_hidden_fields(store: InMemoryGadgets) -> None:
    """Filtering, ordering, or searching a hidden field would tell its values."""
    policy = FieldPolicy(visible={"price": "shop.view_cost", "name": "shop.x"})

    class ByPrice(FilterSet):
        model = Gadget
        ordering_fields = ("price",)

    class ByName(FilterSet):
        model = Gadget
        title = Filter("icontains", field="name")

    class Searching(FilterSet):
        model = Gadget
        default_ordering = ("-made_at",)
        search_fields = ("name",)

    for filters in (ByPrice, ByName, Searching):
        with pytest.raises(TypeError, match="fields some callers may not see"):
            gadgets(store, filters=filters, policy=policy)
    gadgets(store, filters=GadgetFilters, policy=FieldPolicy(visible={"stock": "x.y"}))
    gadgets(store, filters=lambda: None, policy=policy)


def test_nested_resources_need_a_parent_the_caller_may_reach(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """A missing parent, or one out of its scope or rules, hides its children."""
    use_auth()

    async def cheap_only(request: HTTPConnection) -> Any:  # noqa: ANN401 - criteria.
        return F("price") < Decimal("2.50")

    class NotC(BasePermission):
        async def has_object_permission(
            self, request: HTTPConnection, user: AnyUser, obj: object
        ) -> bool:
            return getattr(obj, "name", "") != "c"

    parent = gadgets(store, scope=cheap_only, rule=AllOf(IsAuthenticated(), NotC()))
    by_name = {gadget.name: gadget for gadget in store.items.values()}
    reviews = InMemoryRepository[Review, UUID](
        [Review(id=uuid4(), gadget=gadget.id, text="ok") for gadget in by_name.values()]
    )

    class Reviews(UnitOfWork):
        def __init__(self) -> None:
            super().__init__(events=EventBus())
            self.reviews = reviews

        async def _commit(self) -> None:
            """Nothing to write."""

        async def _rollback(self) -> None:
            """Nothing to undo."""

    nested = ResourceRouter(
        "/reviews",
        model=Review,
        unit_of_work=Reviews,
        repository=lambda uow: uow.reviews,
        parent=parent,
        parent_field="gadget",
        rule=IsAuthenticated(),
    )
    client = client_of(parent, nested)

    def listing(gadget_id: object) -> int:
        return client.get(
            f"/gadgets/{gadget_id}/reviews", headers=as_("viewer")
        ).status_code

    assert listing(by_name["b"].id) == 200
    assert listing(uuid4()) == 404
    assert listing(by_name["a"].id) == 404
    assert listing(by_name["c"].id) == 403


class Tag(Command):
    """Tag a gadget with who asked."""

    gadget_id: UUID
    who: str
    name: str


def test_dependencies_provide_command_fields(store: InMemoryGadgets) -> None:
    """``provide`` fills fields from dependencies; the path still wins."""
    tagged: list[Tag] = []

    async def tag(command: Tag, uow: FakeGadgetsUnitOfWork) -> Gadget:
        tagged.append(command)
        async with uow:
            return await uow.gadgets.get(GadgetId(command.gadget_id))

    def who(x_user: Annotated[str, Header()] = "nobody") -> str:
        return x_user

    def sneaky() -> UUID:
        return uuid4()

    resource = gadgets(store, rule=None, dependencies=[Public])
    resource.command_route(
        Tag,
        service=tag,
        body=RenameIn,
        path="/tag",
        provide={"who": who, "gadget_id": sneaky},
    )
    client = client_of(resource)
    gadget = next(iter(store.items.values()))

    answer = client.post(
        f"{LOCATION}{gadget.id}/tag", json={"name": "n"}, headers={"X-User": "ann"}
    )

    assert answer.status_code == 200
    assert tagged == [Tag(gadget_id=gadget.id, who="ann", name="n")]
    with pytest.raises(TypeError, match="Tag has no field nope"):
        resource.command_route(Tag, service=tag, body=RenameIn, provide={"nope": who})
    with pytest.raises(TypeError, match="Nothing gives Tag's who"):
        resource.command_route(Tag, service=tag, body=RenameIn)


class GadgetsLabelled(QueryMessage):
    """Gadgets with one label."""

    label: Label
    most: int = 10


class GadgetAgain(QueryMessage):
    """One gadget, by its id."""

    gadget_id: UUID


class Dearest(QueryMessage):
    """The dearest gadget."""


async def labelled(query: GadgetsLabelled, uow: FakeGadgetsUnitOfWork) -> list[Gadget]:
    """Up to ``most`` gadgets with the label."""
    async with uow:
        page = await uow.gadgets.paginate(F("label") == query.label, limit=query.most)
    return page.items


async def again(query: GadgetAgain, uow: FakeGadgetsUnitOfWork) -> Gadget:
    """The gadget named."""
    async with uow:
        return await uow.gadgets.get(GadgetId(query.gadget_id))


async def dearest(query: Dearest, uow: FakeGadgetsUnitOfWork) -> Gadget:
    """The gadget with the highest price."""
    async with uow:
        found = await uow.gadgets.first(Listing(ordering=["-price"]))
    assert found is not None
    return found


def test_query_routes_answer_queries(store: InMemoryGadgets, use_auth: UseAuth) -> None:
    """Fields from the query string or the path; one aggregate, or items."""
    use_auth()
    resource = gadgets(store)
    resource.query_route(GadgetsLabelled, handler=labelled, path="/labelled")
    resource.query_route(GadgetAgain, handler=again, path="/again")
    client = client_of(resource)
    gadget = next(item for item in store.items.values() if item.name == "b")
    viewer = as_("viewer")

    found = client.get("/gadgets/labelled?label=B", headers=viewer)
    none = client.get("/gadgets/labelled?label=Z", headers=viewer)
    unknown = client.get("/gadgets/labelled?label=B&color=red", headers=viewer)
    long = client.get(f"/gadgets/labelled?label={'x' * 21}", headers=viewer)
    one = client.get(f"{LOCATION}{gadget.id}/again", headers=viewer)
    same = client.get(
        f"{LOCATION}{gadget.id}/again",
        headers=as_("viewer", **{"If-None-Match": one.headers["etag"]}),
    )
    missing = client.get(f"{LOCATION}{uuid4()}/again", headers=viewer)

    assert found.status_code == 200
    assert [item["name"] for item in found.json()["items"]] == ["b"]
    assert none.json() == {"items": []}
    assert (unknown.status_code, long.status_code) == (422, 422)
    assert (one.json()["name"], one.headers["etag"]) == (
        "b",
        client.get(f"{LOCATION}{gadget.id}", headers=viewer).headers["etag"],
    )
    assert same.status_code == 304
    assert missing.status_code == 404
    assert client.get("/gadgets/labelled?label=B").status_code == 401
    assert (
        client.get("/gadgets/labelled?label=B", headers=as_("adder")).status_code == 403
    )


def test_query_routes_keep_scopes_policies_and_actions(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """Out of scope is left out, or 404; hidden fields too; actions ask their own."""
    use_auth()

    async def cheap_only(request: HTTPConnection) -> Any:  # noqa: ANN401 - criteria.
        return (
            F("price") < Decimal("2.50")
            if request.headers["x-user"] == "viewer"
            else None
        )

    async def everything(
        query: GadgetsLabelled, uow: FakeGadgetsUnitOfWork
    ) -> tuple[Gadget, ...]:
        async with uow:
            return tuple(await uow.gadgets.filter(None))

    resource = gadgets(
        store, scope=cheap_only, policy=FieldPolicy(visible={"price": "shop.view_cost"})
    )
    resource.query_route(GadgetsLabelled, handler=everything, path="/all")
    resource.query_route(Dearest, handler=dearest, path="/dearest")
    resource.query_route(
        Dearest, handler=dearest, path="/export", action="export", name="export"
    )
    client = client_of(resource)

    seen = client.get("/gadgets/all?label=A", headers=as_("viewer")).json()["items"]
    shown = client.get("/gadgets/dearest", headers=as_("costs")).json()

    assert sorted(item["name"] for item in seen) == ["b", "c"]
    assert all("price" not in item for item in seen)
    assert client.get("/gadgets/dearest", headers=as_("viewer")).status_code == 404
    assert (shown["name"], shown["price"]) == ("a", "3.00")
    assert client.get("/gadgets/export", headers=as_("costs")).status_code == 403


def test_query_routes_on_nested_resources_take_the_parent_from_the_path(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """A parent field comes from the path, and other parents' items are left out."""
    use_auth()
    gadget, other = list(store.items.values())[:2]
    reviews = InMemoryRepository[Review, UUID](
        [
            Review(id=uuid4(), gadget=gadget.id, text="good"),
            Review(id=uuid4(), gadget=other.id, text="bad"),
        ]
    )
    asked: list[object] = []

    class Reviews(UnitOfWork):
        def __init__(self) -> None:
            super().__init__(events=EventBus())
            self.reviews = reviews

        async def _commit(self) -> None:
            """Nothing to write."""

        async def _rollback(self) -> None:
            """Nothing to undo."""

    class ReviewsOf(QueryMessage):
        """Every review, whatever the gadget."""

        gadget: UUID

    async def careless(query: ReviewsOf, uow: Reviews) -> list[Review]:
        asked.append(query.gadget)
        async with uow:
            return await uow.reviews.filter(None)

    nested = ResourceRouter(
        "/reviews",
        model=Review,
        unit_of_work=Reviews,
        repository=lambda uow: uow.reviews,
        parent=gadgets(store),
        parent_field="gadget",
        rule=IsAuthenticated(),
    )
    nested.query_route(ReviewsOf, handler=careless, path="/all")
    client = client_of(nested.parent, nested)  # type: ignore[arg-type]

    answer = client.get(f"/gadgets/{gadget.id}/reviews/all", headers=as_("viewer"))

    assert [item["text"] for item in answer.json()["items"]] == ["good"]
    assert asked == [gadget.id]


def test_query_route_declarations_are_checked(store: InMemoryGadgets) -> None:
    """A path is needed; so is what the handler returns; and it must be an aggregate."""
    resource = gadgets(store, rule=None, dependencies=[Public])

    async def unannotated(query: Dearest, uow: FakeGadgetsUnitOfWork):  # type: ignore[no-untyped-def]  # noqa: ANN202
        return None

    async def text(query: Dearest, uow: FakeGadgetsUnitOfWork) -> str:
        return "text"

    with pytest.raises(TypeError, match="starts with /"):
        resource.query_route(Dearest, handler=dearest, path="")
    with pytest.raises(TypeError, match=r"Annotate what .*unannotated returns"):
        resource.query_route(Dearest, handler=unannotated, path="/x")
    resource.query_route(Dearest, handler=text, path="/text")
    client = client_of(resource)

    with pytest.raises(RuntimeError, match="text answered str, not a Gadget"):
        client.get("/gadgets/text")


def test_query_routes_are_documented(store: InMemoryGadgets) -> None:
    """Their parameters, answers, and errors are in OpenAPI."""
    resource = gadgets(store)
    resource.query_route(GadgetsLabelled, handler=labelled, path="/labelled")
    resource.query_route(Dearest, handler=dearest, path="/dearest")
    document = client_of(resource).get("/openapi.json").json()

    listing = document["paths"]["/gadgets/labelled"]["get"]
    single = document["paths"]["/gadgets/dearest"]["get"]
    assert listing["operationId"].startswith("gadgets_labelled")
    assert [(each["name"], each["in"]) for each in listing["parameters"]] == [
        ("label", "query"),
        ("most", "query"),
    ]
    assert sorted(listing["responses"]) == ["200", "401", "403", "422"]
    assert listing["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/GadgetsLabelledAnswer"
    }
    assert "parameters" not in single or all(
        each["in"] == "header" for each in single["parameters"]
    )
    assert {"200", "304", "401", "403"} <= set(single["responses"])


def test_lists_may_show_some_fields_and_names_take_a_prefix(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """``list_fields`` trims list items; ``name_prefix`` starts every route name."""
    use_auth()
    resource = gadgets(store, list_fields=["name"], name_prefix="shop_")
    resource.command_route(RenameGadget, service=rename, body=RenameIn, path="/rename")
    client = client_of(resource)

    listed = client.get("/gadgets", headers=as_("viewer")).json()["items"]
    document = client.get("/openapi.json").json()

    assert sorted(listed, key=str) == [{"name": "a"}, {"name": "b"}, {"name": "c"}]
    assert document["components"]["schemas"]["GadgetPage"]["properties"]["items"][
        "items"
    ] == {"$ref": "#/components/schemas/GadgetSummary"}
    assert [each.name for each in resource.operations] == [
        "shop_list_gadgets",
        "shop_read_gadget",
        "shop_rename_gadget",
    ]
    assert resource.read_name == "shop_read_gadget"
    assert [
        (each.method, each.path, each.action, each.detail)
        for each in resource.operations
    ] == [
        ("GET", "/gadgets", "view", False),
        ("GET", "/gadgets/{gadget_id}", "view", True),
        ("POST", "/gadgets/{gadget_id}/rename", "change", True),
    ]
    with pytest.raises(TypeError, match=r"GadgetOut has no field nope\."):
        gadgets(store, list_fields=["nope"])


def test_routes_of_your_own_fetch_as_the_resource_does(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """``fetch`` keeps scope and object rules; nested resources cannot use it."""
    use_auth()

    async def cheap_only(request: HTTPConnection) -> Any:  # noqa: ANN401 - criteria.
        return F("price") < Decimal("2.50")

    resource = gadgets(store, scope=cheap_only)
    by_name = {gadget.name: gadget for gadget in store.items.values()}

    @resource.get("/{gadget_id}/name", dependencies=[Public])
    async def name_of(request: Request, gadget_id: UUID) -> str:
        found = await resource.fetch(request, FakeGadgetsUnitOfWork(store), gadget_id)
        return str(getattr(found, "name"))  # noqa: B009 - an aggregate of any type.

    client = client_of(resource)
    nested = ResourceRouter(
        "/reviews",
        model=Review,
        unit_of_work=FakeGadgetsUnitOfWork,
        repository=lambda uow: uow,
        parent=resource,
        parent_field="gadget",
        rule=IsAuthenticated(),
    )

    assert client.get(f"/gadgets/{by_name['b'].id}/name").json() == "b"
    assert client.get(f"/gadgets/{by_name['a'].id}/name").status_code == 404
    with pytest.raises(TypeError, match="not nested"):
        asyncio.run(nested.fetch(Mock(), Mock(), uuid4()))


class Receipt(BaseModel):
    """An answer whose JSON needs the JSON mode: an id and a time."""

    id: UUID
    at: datetime


async def receipt(command: MakeGadget, uow: FakeGadgetsUnitOfWork) -> Receipt:
    """Answer with a receipt, storing nothing."""
    return Receipt(id=UUID(int=7), at=datetime(2026, 1, 2, tzinfo=UTC))


async def shadow(command: RenameGadget, uow: FakeGadgetsUnitOfWork) -> object:
    """Answer with an object shaped as the gadget, but not one."""
    async with uow:
        gadget = await uow.gadgets.get(GadgetId(command.gadget_id))
    return SimpleNamespace(
        **{
            field.name: getattr(gadget, field.name)
            for field in dataclasses.fields(gadget)
        }
    )


def test_answers_keep_their_status_media_type_and_root_path(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """Other schemas keep the status; reads are JSON; ``Location`` has the root."""
    use_auth()
    resource = gadgets(store)
    resource.command_route(MakeGadget, service=receipt, body=MakeIn, returns=Receipt)
    resource.command_route(MakeGadget, service=make, body=MakeIn, path="/made")
    resource.command_route(RenameGadget, service=shadow, body=RenameIn, path="/shadow")
    client = TestClient(client_of(resource).app, root_path="/api")
    gadget = next(iter(store.items.values()))
    body = {"name": "n", "price": "1.00"}

    receipted = client.post("/gadgets", json=body, headers=as_("adder"))
    made = client.post("/gadgets/made", json=body, headers=as_("adder"))
    shadowed = client.post(
        f"{LOCATION}{gadget.id}/shadow", json={"name": "s"}, headers=as_("changer")
    )
    read = client.get(f"{LOCATION}{gadget.id}", headers=as_("viewer"))

    assert (receipted.status_code, receipted.json()) == (
        201,
        {"id": str(UUID(int=7)), "at": "2026-01-02T00:00:00Z"},
    )
    assert made.headers["location"] == f"/api{LOCATION}{made.json()['id']}"
    assert shadowed.json()["id"] == str(gadget.id)
    assert read.headers["content-type"] == "application/json"
    not_modified = client.get(
        f"{LOCATION}{gadget.id}",
        headers=as_("viewer", **{"If-None-Match": read.headers["etag"]}),
    )
    assert not_modified.headers["etag"] == read.headers["etag"]


class EditReview(Command):
    """Edit a review of a gadget."""

    review_id: UUID
    gadget: UUID
    text: str


def test_commands_on_nested_items_stay_under_their_parent(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """The item's id and the parent's both come from the path; others are 404."""
    use_auth()
    gadget, other = list(store.items.values())[:2]
    mine = Review(id=uuid4(), gadget=gadget.id, text="good")
    theirs = Review(id=uuid4(), gadget=other.id, text="bad")
    reviews = InMemoryRepository[Review, UUID]([mine, theirs])
    edited: list[EditReview] = []

    class Reviews(UnitOfWork):
        def __init__(self) -> None:
            super().__init__(events=EventBus())
            self.reviews = reviews

        async def _commit(self) -> None:
            """Nothing to write."""

        async def _rollback(self) -> None:
            """Nothing to undo."""

    async def edit(command: EditReview, uow: Reviews) -> Review:
        edited.append(command)
        async with uow:
            review = await uow.reviews.get(command.review_id)
            review.text = command.text
            await uow.commit()
        return review

    nested = ResourceRouter(
        "/reviews",
        model=Review,
        unit_of_work=Reviews,
        repository=lambda uow: uow.reviews,
        parent=gadgets(store),
        parent_field="gadget",
        rule=IsAuthenticated(),
    )
    nested.command_route(EditReview, service=edit, body=ReviewIn, path="/edit")
    client = client_of(nested.parent, nested)  # type: ignore[arg-type]
    base = f"/gadgets/{gadget.id}/reviews"

    own = client.post(
        f"{base}/{mine.id}/edit", json={"text": "great"}, headers=as_("viewer")
    )
    foreign = client.post(
        f"{base}/{theirs.id}/edit", json={"text": "x"}, headers=as_("viewer")
    )

    assert (own.status_code, own.json()["text"]) == (200, "great")
    assert edited == [EditReview(review_id=mine.id, gadget=gadget.id, text="great")]
    assert foreign.status_code == 404
    assert theirs.text == "bad"


def test_crud_writes_only_the_fields_writable_names(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """``writable`` shapes both derived schemas; other fields are refused."""
    use_auth()
    resource = gadgets(
        store, crud=True, writable=["name", "label", "price", "made_at", "active"]
    )
    client = client_of(resource)
    new = {"name": "n", "label": "N", "price": "1.00", "made_at": "2026-01-01T00:00Z"}

    created = client.post("/gadgets", json=new, headers=as_("root"))
    over_posted = client.post("/gadgets", json={**new, "stock": 9}, headers=as_("root"))
    gadget_id = created.json()["id"]
    changed = client.patch(
        f"/gadgets/{gadget_id}", json={"active": False}, headers=as_("root")
    )
    refused = client.patch(
        f"/gadgets/{gadget_id}", json={"stock": 9}, headers=as_("root")
    )

    assert created.status_code == 201
    assert (over_posted.status_code, refused.status_code) == (422, 422)
    assert changed.json()["active"] is False
    assert store.items[GadgetId(UUID(gadget_id))].stock is None


def test_crud_without_writable_fields_is_deprecated(store: InMemoryGadgets) -> None:
    """Every plain field, implicitly, warns; both schemas, or ``writable``, do not."""
    plain = ["name", "label", "price", "made_at"]
    whole = schema_for(Gadget, write=True, include=plain)
    patch = schema_for(Gadget, partial=True, include=["name"])
    with pytest.warns(DomainkitDeprecationWarning, match="without writable=") as caught:
        resource = gadgets(store, crud=True)
    with pytest.warns(DomainkitDeprecationWarning, match="removed in 0.11"):
        gadgets(store, crud=True, create=whole)

    assert caught[0].filename == __file__  # It names the declaration.
    create = next(r for r in resource.operations if r.name == "create_gadget")
    assert create.body is not None
    assert {"stock", "price"} <= set(create.body.model_fields)
    gadgets(store, crud=True, create=whole, update=patch)
    gadgets(store, crud=True, writable=ALL_FIELDS)


def test_writable_declarations_are_checked(store: InMemoryGadgets) -> None:
    """Fields no write sets, odd strings, unused or misplaced ``writable``."""
    whole = schema_for(
        Gadget, write=True, include=["name", "label", "price", "made_at"]
    )
    patch = schema_for(Gadget, partial=True, include=["name"])
    with pytest.raises(TypeError, match="never writes Gadget's id, version"):
        gadgets(store, crud=True, writable=["name", "id", "version"])
    with pytest.raises(TypeError, match="or ALL_FIELDS, not 'all'"):
        gadgets(store, crud=True, writable="all")
    with pytest.raises(TypeError, match="create= and update= are given"):
        gadgets(store, crud=True, writable=["name"], create=whole, update=patch)
    with pytest.raises(TypeError, match="crud is off"):
        gadgets(store, writable=["name"])
    with pytest.raises(TypeError, match="has no field nope"):
        gadgets(store, crud=True, writable=["nope"])


def test_resource_routes_take_body_limits_by_kind_and_route(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """``body_limits`` per kind, ``body_limit=`` per route; others the global."""
    use_auth()
    resource = gadgets(
        store,
        crud=True,
        writable=ALL_FIELDS,
        body_limits={"create": 100, "update": 200, "command": 300, "batch": 400},
    )
    resource.command_route(RenameGadget, service=rename, body=RenameIn, path="/a")
    resource.command_route(
        RenameGadget, service=rename, body=RenameIn, path="/b", name="b", body_limit=50
    )
    resource.batch_route(RenameGadget, service=rename, body=RenameIn)
    resource.batch_route(
        RenameGadget, service=rename, body=RenameIn, path="/c", name="c", body_limit=60
    )
    limits = {
        route.name: route_body_limit(route)
        for route in resource.routes
        if isinstance(route, APIRoute)
    }
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(resource)
    app.add_middleware(Users, users=everyone())
    app.add_middleware(RequestBodyLimitMiddleware)
    client = TestClient(app)

    created = client.post("/gadgets", content=b"x" * 101, headers=as_("root"))
    gadget = next(iter(store.items.values()))
    renamed = client.post(
        f"/gadgets/{gadget.id}/b", json={"name": "x" * 40}, headers=as_("root")
    )

    assert limits == {
        "list_gadgets": None,
        "read_gadget": None,
        "create_gadget": 100,
        "update_gadget": 200,
        "delete_gadget": None,
        "rename_gadget": 300,
        "b": 50,
        "rename_gadget_batch": 400,
        "c": 60,
    }
    assert (created.status_code, renamed.status_code) == (413, 413)


def test_resource_body_limits_are_checked(store: InMemoryGadgets) -> None:
    """Kinds that take no body, and limits out of bounds, are refused."""
    with pytest.raises(
        TypeError, match=r"\(create, update, command, batch\), not read"
    ):
        gadgets(store, body_limits={"read": 10})
    with pytest.raises(ValueError, match="not 0"):
        gadgets(store, body_limits={"batch": 0})
    with pytest.raises(ValueError, match="not 0"):
        gadgets(store).command_route(
            RenameGadget, service=rename, body=RenameIn, path="/a", body_limit=0
        )
