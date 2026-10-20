"""What a security review of the resources found: each finding, as a regression.

Every route asks its own rule about the object it acts on; generic writes
keep to the field policy and the caller's scope; a stale ``If-Match``
writes nothing; fields hidden from a caller are not theirs to change.
"""

from decimal import Decimal
from typing import Any

import pytest
from starlette.requests import HTTPConnection

from domainkit.contrib.auth import (
    AnyUser,
    BasePermission,
    HasObjectPermissions,
    IsAuthenticated,
)
from domainkit.contrib.auth.access import current_user
from domainkit.ddd import Query as QueryMessage
from domainkit.ddd.query import F
from domainkit.resources import FieldPolicy, ModelActions, ResourceRouter, schema_for
from domainkit_tests.conftest import UseAuth
from domainkit_tests.gadgets import (
    FakeGadgetsUnitOfWork,
    Gadget,
    GadgetId,
    InMemoryGadgets,
    make_gadget,
)
from domainkit_tests.test_resources import (
    GADGETS,
    LOCATION,
    GadgetAgain,
    RenameGadget,
    RenameIn,
    again,
    as_,
    client_of,
    gadgets,
    rename,
)

TWO: Decimal = Decimal("2.00")
NEW: dict[str, Any] = {
    "name": "n",
    "label": "L",
    "price": "9.99",
    "made_at": "2026-01-01T00:00:00Z",
}
"""A valid body of the generic create."""


@pytest.fixture
def store() -> InMemoryGadgets:
    """Gadgets b (2.00), a (3.00), and c (1.00)."""
    return InMemoryGadgets(
        [
            make_gadget("b", price="2.00"),
            make_gadget("a", price="3.00"),
            make_gadget("c"),
        ]
    )


class NotB(BasePermission):
    """Lets the request through; refuses the gadget that costs 2.00 (b)."""

    async def has_object_permission(
        self, request: HTTPConnection, user: AnyUser, obj: object
    ) -> bool:
        """Anything but b."""
        return getattr(obj, "price", None) != TWO


class Everything(QueryMessage):
    """Every gadget."""


async def everything(query: Everything, uow: FakeGadgetsUnitOfWork) -> list[Gadget]:
    """Every gadget there is."""
    async with uow:
        return await uow.gadgets.filter(None)


def names(store: InMemoryGadgets) -> dict[str, GadgetId]:
    """Gadget ids by name."""
    return {gadget.name: gadget.id for gadget in store.items.values()}


def test_a_routes_own_rule_is_asked_about_its_object(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """Commands, batches, and queries ask ``rule=`` about the aggregate."""
    use_auth()
    resource = gadgets(store)
    resource.command_route(
        RenameGadget, service=rename, body=RenameIn, path="/rename", rule=NotB()
    )
    resource.batch_route(RenameGadget, service=rename, body=RenameIn, rule=NotB())
    resource.query_route(GadgetAgain, handler=again, path="/again", rule=NotB())
    resource.query_route(Everything, handler=everything, path="/every", rule=NotB())
    client = client_of(resource)
    known = names(store)

    renamed = [
        client.post(
            f"{LOCATION}{known[name]}/rename",
            json={"name": f"{name}2"},
            headers=as_("changer"),
        ).status_code
        for name in "ab"
    ]
    batch = client.post(
        "/gadgets/batch",
        json={"items": [{"gadget_id": str(known[n]), "name": f"{n}3"} for n in "ab"]},
        headers=as_("changer"),
    )
    asked = [
        client.get(f"{LOCATION}{known[name]}/again", headers=as_("viewer")).status_code
        for name in "ab"
    ]
    every = client.get("/gadgets/every", headers=as_("viewer"))
    read = client.get(f"{LOCATION}{known['b']}", headers=as_("viewer"))

    assert renamed == [200, 403]
    assert [each["status"] for each in batch.json()["results"]] == [200, 403]
    assert asked == [200, 403]
    assert sorted(item["name"] for item in every.json()["items"]) == ["a3", "c"]
    assert read.status_code == 200
    assert store.items[known["b"]].name == "b"


class Peek(QueryMessage):
    """One gadget, which needs a grant on it."""

    gadget_id: GadgetId


def test_object_permissions_on_a_route_need_a_grant_on_the_object(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """``HasObjectPermissions`` as a route's rule: no grant, no command or query."""
    use_auth()
    resource = gadgets(store, rule=IsAuthenticated())
    resource.command_route(
        RenameGadget,
        service=rename,
        body=RenameIn,
        path="/publish",
        rule=HasObjectPermissions("shop.publish_gadget"),
        action="publish",
    )
    resource.query_route(
        GadgetAgain,
        handler=again,
        path="/secret",
        rule=HasObjectPermissions("shop.peek_gadget"),
        action="peek",
        name="peek",
    )
    client = client_of(resource)
    gadget = next(iter(store.items.values()))

    published = client.post(
        f"{LOCATION}{gadget.id}/publish", json={"name": "x"}, headers=as_("viewer")
    )
    peeked = client.get(f"{LOCATION}{gadget.id}/secret", headers=as_("viewer"))

    assert (published.status_code, peeked.status_code) == (403, 403)
    assert gadget.name != "x"


def test_generic_creates_keep_to_the_field_policy(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """Read-only fields are not in the body; others need what changing needs."""
    use_auth()
    policy = FieldPolicy(
        read_only=["active"],
        editable={"stock": "shop.change_stock"},
        visible={"size": "shop.view_cost"},
    )
    client = client_of(gadgets(store, policy=policy, crud=True))

    read_only = client.post(
        "/gadgets", json={**NEW, "active": False}, headers=as_("adder")
    )
    stock = client.post("/gadgets", json={**NEW, "stock": 9}, headers=as_("adder"))
    hidden = client.post("/gadgets", json={**NEW, "size": 10}, headers=as_("adder"))
    plain = client.post("/gadgets", json=NEW, headers=as_("adder"))
    allowed = client.post("/gadgets", json={**NEW, "stock": 9}, headers=as_("root"))

    assert read_only.status_code == 422
    assert (stock.status_code, stock.json()["code"]) == (403, "field_not_editable")
    assert (hidden.status_code, hidden.json()["code"]) == (403, "field_not_editable")
    assert (plain.status_code, allowed.status_code) == (201, 201)
    assert sorted(each.stock or 0 for each in store.items.values()) == [0, 0, 0, 0, 9]


def test_fields_hidden_from_the_caller_are_not_theirs_to_change(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """A field the caller may not see cannot be overwritten blind."""
    use_auth()
    policy = FieldPolicy(visible={"stock": "shop.view_cost"})
    client = client_of(gadgets(store, policy=policy, crud=True))
    gadget = next(iter(store.items.values()))
    gadget.stock = 42
    url = f"{LOCATION}{gadget.id}"

    blind = client.patch(url, json={"stock": 41}, headers=as_("changer"))
    seen = client.patch(url, json={"stock": 40}, headers=as_("root"))

    assert (blind.status_code, blind.json()["code"]) == (403, "field_not_editable")
    assert seen.status_code == 200
    assert gadget.stock == 40


async def red_only(request: HTTPConnection) -> Any:  # noqa: ANN401 - criteria.
    """The tenant of everyone but superusers: red gadgets."""
    return None if current_user(request).is_superuser else F("color") == "red"


def test_generic_writes_stay_in_the_callers_scope(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """A create or a change that would land outside the scope is refused."""
    use_auth()
    client = client_of(gadgets(store, scope=red_only, crud=True))
    gadget = next(iter(store.items.values()))

    outside = client.post(
        "/gadgets", json={**NEW, "color": "blue"}, headers=as_("adder")
    )
    moved = client.patch(
        f"{LOCATION}{gadget.id}", json={"color": "blue"}, headers=as_("changer")
    )
    inside = client.post("/gadgets", json={**NEW, "color": "red"}, headers=as_("adder"))
    anywhere = client.post(
        "/gadgets", json={**NEW, "color": "blue"}, headers=as_("root")
    )

    assert (outside.status_code, outside.json()["code"]) == (403, "permission_denied")
    assert (moved.status_code, moved.json()["code"]) == (403, "permission_denied")
    assert (inside.status_code, anywhere.status_code) == (201, 201)


async def rename_directly(command: RenameGadget, uow: FakeGadgetsUnitOfWork) -> Gadget:
    """Change a gadget it never loads through the repository, then commit."""
    async with uow:
        gadget = uow.gadgets.items[GadgetId(command.gadget_id)]
        gadget.name = command.name
        await uow.commit()
    return gadget


def test_an_unchecked_if_match_commits_nothing(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """The unit of work refuses to commit before the expected aggregate is read."""
    use_auth()
    units: list[FakeGadgetsUnitOfWork] = []

    def unit_of_work() -> FakeGadgetsUnitOfWork:
        units.append(FakeGadgetsUnitOfWork(store))
        return units[-1]

    resource = ResourceRouter(
        "/gadgets",
        model=Gadget,
        unit_of_work=unit_of_work,
        repository=lambda uow: uow.gadgets,
        rule=ModelActions(GADGETS),
    )
    resource.command_route(
        RenameGadget, service=rename_directly, body=RenameIn, path="/rename"
    )
    client = client_of(resource)
    gadget = next(iter(store.items.values()))

    with pytest.raises(RuntimeError, match="If-Match"):
        client.post(
            f"{LOCATION}{gadget.id}/rename",
            json={"name": "overwritten"},
            headers=as_("changer", **{"If-Match": '"999"'}),
        )

    assert sum(unit.commits for unit in units) == 0


class NoRequests(BasePermission):
    """Refuses every request; has nothing against any object."""

    async def has_permission(self, request: HTTPConnection, user: AnyUser) -> bool:
        """No."""
        return False


def test_a_routes_own_rule_is_asked_about_the_request_too(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """``rule=`` that refuses every request refuses commands, batches, queries."""
    use_auth()
    resource = gadgets(store)
    resource.command_route(
        RenameGadget, service=rename, body=RenameIn, path="/rename", rule=NoRequests()
    )
    resource.batch_route(RenameGadget, service=rename, body=RenameIn, rule=NoRequests())
    resource.query_route(GadgetAgain, handler=again, path="/again", rule=NoRequests())
    client = client_of(resource)
    gadget = next(iter(store.items.values()))
    item = f"{LOCATION}{gadget.id}"

    renamed = client.post(f"{item}/rename", json={"name": "x"}, headers=as_("root"))
    batch = client.post(
        "/gadgets/batch",
        json={"items": [{"gadget_id": str(gadget.id), "name": "x"}]},
        headers=as_("root"),
    )
    asked = client.get(f"{item}/again", headers=as_("root"))

    assert (renamed.status_code, batch.status_code, asked.status_code) == (
        403,
        403,
        403,
    )


def test_write_bodies_name_every_permission_a_field_needs() -> None:
    """A field both guarded and hidden needs both permissions to be set."""
    policy = FieldPolicy(
        editable={"stock": "shop.change_stock"}, visible={"stock": "shop.view_cost"}
    )

    described = policy.describe_changes(schema_for(Gadget, partial=True))

    assert described.model_fields["stock"].description == (
        "Changed only by users granted shop.change_stock and shop.view_cost."
    )
