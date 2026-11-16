"""A command route whose rule asks for an authorization gets a twin (user report A6).

``POST <the route's path>/authorization`` builds the command as the route
does, checks what the act would check with the asking rule passing, and
answers ``201`` with what the rule asks for (ADR 0144).
"""

from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.requests import HTTPConnection

from domainkit.contrib.auth import AnyUser
from domainkit.ddd import UNSET, EventBus, InMemoryRepository, UnitOfWork
from domainkit.ddd.query import F
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.access import (
    AllOf,
    AsksAuthorization,
    BasePermission,
    HasPermissions,
    IsAuthenticated,
    Not,
    open_routes,
)
from domainkit.http.errors import install_exception_handlers
from domainkit.http.middleware import RequestBodyLimitMiddleware, route_body_limit
from domainkit.http.ratelimit import MemoryBackend, Throttle
from domainkit.resources import (
    FieldPolicy,
    ModelActions,
    ResourceRouter,
    asking_authorization,
)
from domainkit_tests.conftest import UseAuth
from domainkit_tests.gadgets import (
    FakeGadgetsUnitOfWork,
    Gadget,
    InMemoryGadgets,
    make_gadget,
)
from domainkit_tests.test_resources import (
    GADGETS,
    LOCATION,
    DropGadget,
    EditReview,
    GadgetChanges,
    MakeGadget,
    MakeIn,
    RenameGadget,
    RenameIn,
    Review,
    ReviewIn,
    Tag,
    TouchGadget,
    UpdateGadget,
    Users,
    as_,
    client_of,
    drop,
    everyone,
    gadgets,
    make,
    rename,
    touch,
    update,
)
from domainkit_tests.test_use_cases import restock

AUTHORIZED = {"X-Authorized": "yes"}
"""What the fake rule takes for an authorization of the act."""


class Asked(BaseModel):
    """What the fake rule answers: an authorization it stored."""

    id: str
    purpose: str


class Asks(BasePermission):
    """Asks for an authorization of the act; allows a request that sends one."""

    def __init__(self, purpose: str = "shop.act") -> None:
        """Answer for ``purpose``; nothing asked yet."""
        self.purpose = purpose
        self.asked: list[object] = []

    def authorization_model(self) -> type[BaseModel]:
        """The twin's 201 body."""
        return Asked

    async def ask_authorization(self, request: Request, command: object) -> BaseModel:
        """Record the command; answer an authorization of it."""
        self.asked.append(command)
        return Asked(id=f"a{len(self.asked)}", purpose=self.purpose)

    async def has_permission(self, request: HTTPConnection, user: AnyUser) -> bool:
        """A user whose request sends ``X-Authorized``."""
        return user.is_authenticated and "x-authorized" in request.headers


class AsksInIt(Asks):
    """Asks as ``Asks`` does, reading in the unit of work the twin lends."""

    def __init__(self) -> None:
        """No unit of work lent yet."""
        super().__init__()
        self.lent: list[UnitOfWork | None] = []

    async def ask_authorization(
        self, request: Request, command: object, uow: UnitOfWork | None = None
    ) -> BaseModel:
        """Record the unit of work; answer as ``Asks``."""
        self.lent.append(uow)
        return await super().ask_authorization(request, command)


class Seen(BasePermission):
    """Looks at objects: records each, and allows gadget ``a`` only."""

    def __init__(self) -> None:
        """Nothing seen yet."""
        self.seen: list[str] = []

    async def has_object_permission(
        self, request: HTTPConnection, user: AnyUser, obj: object
    ) -> bool:
        """Gadget ``a``."""
        name = getattr(obj, "name", "")
        self.seen.append(name)
        return name == "a"


def by_name(store: InMemoryGadgets) -> dict[str, UUID]:
    """The ids of the gadgets of ``store``, by name."""
    return {gadget.name: gadget.id for gadget in store.items.values()}


def route_names(resource: ResourceRouter) -> list[str]:
    """The names of the resource's routes, in the order they match."""
    return [route.name for route in resource.routes if isinstance(route, APIRoute)]


def test_the_rule_is_an_asks_authorization() -> None:
    """The fake rule implements the contract the twin reads (user report A6)."""
    assert isinstance(Asks(), AsksAuthorization)


def test_a_route_whose_rule_asks_gets_a_twin_that_answers_201_with_the_model(
    use_auth: UseAuth,
) -> None:
    """The twin asks the rule and answers what it returns, never stored (A6)."""
    use_auth()
    asks = Asks()
    store = InMemoryGadgets([make_gadget("a")])
    resource = gadgets(store)
    resource.command_route(
        RenameGadget, service=rename, body=RenameIn, path="/rename", rule=asks
    )
    client = client_of(resource)
    (gadget_id,) = store.items
    act = f"{LOCATION}{gadget_id}/rename"

    asked = client.post(
        f"{act}/authorization", json={"name": "b"}, headers=as_("changer")
    )
    refused = client.post(act, json={"name": "b"}, headers=as_("changer"))
    done = client.post(act, json={"name": "b"}, headers=as_("changer", **AUTHORIZED))

    assert (asked.status_code, asked.json()) == (
        201,
        {"id": "a1", "purpose": "shop.act"},
    )
    assert asked.headers["cache-control"] == "no-store"
    assert asks.asked == [RenameGadget(gadget_id=gadget_id, name="b")]
    assert refused.status_code == 403
    assert (done.status_code, done.json()["name"]) == (200, "b")
    assert route_names(resource) == [
        "list_gadgets",
        "read_gadget",
        "rename_gadget",
        "rename_gadget_authorization",
    ]


def test_the_twin_builds_the_command_from_the_path_the_body_and_provide(
    use_auth: UseAuth,
) -> None:
    """The path wins over ``provide``, and it over the body, as on the act (A6)."""
    use_auth()
    asks = Asks()
    store = InMemoryGadgets([make_gadget("a")])

    def who(request: Request) -> str:
        return request.headers.get("x-user", "nobody")

    def sneaky() -> UUID:
        return uuid4()

    resource = gadgets(store, rule=None)
    resource.command_route(
        Tag,
        service=touch,
        body=RenameIn,
        path="/tag",
        provide={"who": who, "gadget_id": sneaky},
        rule=IsAuthenticated() & asks,
    )
    resource.command_route(
        TouchGadget, service=touch, rule=IsAuthenticated(), returns=None
    )
    resource.command_route(MakeGadget, service=make, body=MakeIn, rule=asks)
    client = client_of(resource)
    (gadget_id,) = store.items

    tagged = client.post(
        f"{LOCATION}{gadget_id}/tag/authorization",
        json={"name": "n"},
        headers=as_("viewer"),
    )
    made = client.post(
        "/gadgets/authorization",
        json={"name": "new", "price": "2.50"},
        headers=as_("viewer"),
    )
    anonymous = client.post(
        "/gadgets/authorization", json={"name": "new", "price": "2.50"}
    )

    assert (tagged.status_code, made.status_code) == (201, 201)
    assert anonymous.status_code == 401  # Nothing to authorize for no one.
    assert asks.asked == [
        Tag(gadget_id=gadget_id, who="viewer", name="n"),
        MakeGadget(name="new", price=Decimal("2.50")),
    ]
    # The collection's twin is matched before POST /gadgets/{gadget_id}.
    assert route_names(resource).index("make_gadget_authorization") < (
        route_names(resource).index("touch_gadget")
    )


def test_the_twin_of_a_partial_change_takes_the_fields_set_and_the_policy(
    use_auth: UseAuth,
) -> None:
    """A ``PatchModel`` body gives the fields sent; the policy guards them (A6)."""
    use_auth()
    asks = Asks()
    store = InMemoryGadgets([make_gadget("a")])
    resource = gadgets(
        store, policy=FieldPolicy(editable={"stock": "shop.change_stock"})
    )
    resource.command_route(
        UpdateGadget, service=update, body=GadgetChanges, method="PATCH", rule=asks
    )
    client = client_of(resource)
    (gadget_id,) = store.items
    twin = f"{LOCATION}{gadget_id}/authorization"

    named = client.post(twin, json={"name": "z"}, headers=as_("changer"))
    stocked = client.post(twin, json={"stock": None}, headers=as_("stocker"))
    guarded = client.post(twin, json={"stock": 3}, headers=as_("changer"))

    assert (named.status_code, stocked.status_code) == (201, 201)
    assert (guarded.status_code, guarded.json()["code"]) == (403, "field_not_editable")
    assert asks.asked == [
        UpdateGadget(gadget_id=gadget_id, name="z", stock=UNSET),
        UpdateGadget(gadget_id=gadget_id, name=UNSET, stock=None),
    ]


def test_a_use_case_s_route_gets_its_twin(use_auth: UseAuth) -> None:
    """``ResourceRouter.use_case`` declares a command route, twin included (A6)."""
    use_auth()
    asks = Asks()
    store = InMemoryGadgets([make_gadget("a")])
    resource = gadgets(store)
    resource.use_case(restock, path="/restock", rule=asks)
    client = client_of(resource)
    (gadget_id,) = store.items

    asked = client.post(
        f"{LOCATION}{gadget_id}/restock/authorization",
        json={"quantity": 4},
        headers=as_("changer"),
    )
    too_many = client.post(
        f"{LOCATION}{gadget_id}/restock/authorization",
        json={"quantity": 101},
        headers=as_("changer"),
    )

    assert asked.status_code == 201
    assert too_many.status_code == 422
    assert asks.asked == [restock.command(gadget_id=gadget_id, quantity=4)]
    assert "restock_authorization" in route_names(resource)
    assert store.items[gadget_id].stock is None


def test_the_twin_lends_its_route_s_unit_of_work_to_a_rule_that_takes_one(
    use_auth: UseAuth,
) -> None:
    """``ask_authorization(..., uow)`` reads in the route's; others are not given it.

    A rule written for 2.4 takes no ``uow``, and is called as before (ADR
    0161, user report A13).
    """
    use_auth()
    in_it = AsksInIt()
    plain = Asks()
    store = InMemoryGadgets([make_gadget("a")])
    made: list[FakeGadgetsUnitOfWork] = []

    def unit_of_work() -> FakeGadgetsUnitOfWork:
        made.append(FakeGadgetsUnitOfWork(store))
        return made[-1]

    resource = ResourceRouter(
        "/gadgets",
        model=Gadget,
        unit_of_work=unit_of_work,
        repository=lambda uow: uow.gadgets,
        rule=ModelActions(GADGETS),
    )
    resource.use_case(restock, path="/restock", rule=in_it)
    resource.command_route(MakeGadget, service=make, body=MakeIn, rule=plain)
    client = client_of(resource)
    (gadget_id,) = store.items

    restocking = client.post(
        f"{LOCATION}{gadget_id}/restock/authorization",
        json={"quantity": 4},
        headers=as_("changer"),
    )
    making = client.post(
        "/gadgets/authorization",
        json={"name": "n", "price": "1.00"},
        headers=as_("adder"),
    )

    assert (restocking.status_code, making.status_code) == (201, 201)
    assert in_it.lent == [made[0]]
    assert plain.asked == [MakeGadget(name="n", price=Decimal("1.00"))]
    assert [uow.commits for uow in made] == [0, 0]


class AsksByPosition(Asks):
    """Takes ``uow`` only by position: it cannot be given ``uow=``."""

    async def ask_authorization(
        self, request: Request, command: object, uow: object = None, /
    ) -> BaseModel:
        """Answer as ``Asks``, recording what came as ``uow``."""
        self.asked.append(uow)
        return await super().ask_authorization(request, command)


class Unreadable:
    """An ``ask_authorization`` whose signature cannot be read."""

    __signature__ = "not a signature"

    def __init__(self, rule: Asks) -> None:
        """Answer as ``rule`` does."""
        self.rule = rule

    async def __call__(self, request: Request, command: object) -> BaseModel:
        """Answer as the rule does."""
        return await self.rule.ask_authorization(request, command)


def test_the_twin_lends_no_unit_of_work_it_cannot_give_by_name(
    use_auth: UseAuth,
) -> None:
    """A positional-only ``uow``, or no signature to read: called as in 2.4."""
    use_auth()
    by_position = AsksByPosition()
    unreadable = Asks("shop.other")
    unreadable.ask_authorization = Unreadable(Asks("shop.other"))  # type: ignore[method-assign]
    store = InMemoryGadgets([make_gadget("a")])
    resource = gadgets(store)
    resource.use_case(restock, path="/restock", rule=by_position)
    resource.command_route(MakeGadget, service=make, body=MakeIn, rule=unreadable)
    client = client_of(resource)
    (gadget_id,) = store.items

    restocking = client.post(
        f"{LOCATION}{gadget_id}/restock/authorization",
        json={"quantity": 4},
        headers=as_("changer"),
    )
    making = client.post(
        "/gadgets/authorization",
        json={"name": "n", "price": "1.00"},
        headers=as_("adder"),
    )

    assert (restocking.status_code, making.status_code) == (201, 201)
    assert by_position.asked[0] is None  # Nothing came as uow.
    assert making.json()["purpose"] == "shop.other"


class AsksOfItsService(Asks):
    """Asks, and refuses a service that is not ``rename``, as an act's rule may."""

    def service_problem(self, service: object) -> str | None:
        """Why ``service`` would not spend it; None for ``rename``."""
        named = getattr(service, "__name__", service)
        return None if service is rename else f"runs {named}, not rename"


def test_a_route_whose_rule_refuses_its_service_is_refused_when_declared() -> None:
    """``service_problem`` names it, and the route is not declared (A13)."""
    store = InMemoryGadgets([make_gadget("a")])
    resource = gadgets(store)
    resource.command_route(
        RenameGadget,
        service=rename,
        body=RenameIn,
        path="/rename",
        rule=AsksOfItsService(),
    )

    with pytest.raises(
        ImproperlyConfigured,
        match="asks for an authorization of its act, but runs touch, not rename",
    ):
        resource.command_route(
            RenameGadget,
            service=touch,
            body=RenameIn,
            path="/touch",
            rule=AsksOfItsService(),
        )
    assert "rename_gadget_authorization" in route_names(resource)


def test_the_twin_refuses_what_the_act_would_refuse(use_auth: UseAuth) -> None:
    """Other rules answer 401 or 403, reach 404, and object rules are asked (A6)."""
    use_auth()
    asks = Asks()
    seen = Seen()
    store = InMemoryGadgets(
        [make_gadget("a"), make_gadget("b"), make_gadget("c", price="9.00")]
    )

    async def cheap_only(request: Any) -> Any:  # noqa: ANN401 - a connection, criteria.
        return F("price") < Decimal("5")

    resource = gadgets(store, scope=cheap_only)
    resource.command_route(
        RenameGadget,
        service=rename,
        body=RenameIn,
        path="/rename",
        rule=AllOf(seen, asks),
    )
    client = client_of(resource)
    ids = by_name(store)

    def ask(name: str, who: str | None = "changer", id_: object = None) -> int:
        answer = client.post(
            f"{LOCATION}{id_ or ids[name]}/rename/authorization",
            json={"name": "z"},
            headers=as_(who) if who else {},
        )
        return answer.status_code

    assert ask("a") == 201
    assert ask("a", who="viewer") == 403  # ModelActions: no change_gadget.
    assert ask("a", who=None) == 401
    assert ask("b") == 403  # The object rule refuses b.
    assert ask("c") == 404  # Out of scope.
    assert ask("a", id_=uuid4()) == 404  # Not there.
    assert seen.seen == ["a", "b"]
    assert len(asks.asked) == 1
    assert [gadget.name for gadget in store.items.values()] == ["a", "b", "c"]


def test_the_twin_of_a_nested_route_takes_the_parent_and_its_reach(
    use_auth: UseAuth,
) -> None:
    """The parent's id fills its field, and a review of another parent is 404 (A6)."""
    use_auth()
    asks = Asks()
    store = InMemoryGadgets([make_gadget("a"), make_gadget("b")])
    gadget, other = store.items
    mine = Review(id=uuid4(), gadget=gadget, text="good")
    theirs = Review(id=uuid4(), gadget=other, text="bad")
    reviews = InMemoryRepository[Review, UUID]([mine, theirs])

    class Reviews(UnitOfWork):
        def __init__(self) -> None:
            super().__init__(events=EventBus())
            self.reviews = reviews

        async def _commit(self) -> None:
            """Nothing to write."""

        async def _rollback(self) -> None:
            """Nothing to undo."""

    async def never(command: EditReview, uow: Reviews) -> Review:
        raise AssertionError(command)

    nested = ResourceRouter(
        "/reviews",
        model=Review,
        unit_of_work=Reviews,
        repository=lambda uow: uow.reviews,
        parent=gadgets(store),
        parent_field="gadget",
        rule=IsAuthenticated(),
    )
    nested.command_route(
        EditReview, service=never, body=ReviewIn, path="/edit", rule=asks
    )
    client = client_of(nested.parent, nested)  # type: ignore[arg-type]
    base = f"/gadgets/{gadget}/reviews"

    own = client.post(
        f"{base}/{mine.id}/edit/authorization",
        json={"text": "x"},
        headers=as_("viewer"),
    )
    foreign = client.post(
        f"{base}/{theirs.id}/edit/authorization",
        json={"text": "x"},
        headers=as_("viewer"),
    )
    no_parent = client.post(
        f"/gadgets/{uuid4()}/reviews/{mine.id}/edit/authorization",
        json={"text": "x"},
        headers=as_("viewer"),
    )

    assert (own.status_code, foreign.status_code, no_parent.status_code) == (
        201,
        404,
        404,
    )
    assert asks.asked == [EditReview(review_id=mine.id, gadget=gadget, text="x")]


def test_the_twin_never_runs_the_service_nor_commits(use_auth: UseAuth) -> None:
    """Neither the service nor its unit of work's commit is reached (A6)."""
    use_auth()
    store = InMemoryGadgets([make_gadget("a")])
    ran: list[object] = []
    made: list[FakeGadgetsUnitOfWork] = []

    def unit_of_work() -> FakeGadgetsUnitOfWork:
        made.append(FakeGadgetsUnitOfWork(store))
        return made[-1]

    async def recorded(command: object, uow: FakeGadgetsUnitOfWork) -> Gadget:
        ran.append(command)
        raise AssertionError(command)

    resource = ResourceRouter(
        "/gadgets",
        model=Gadget,
        unit_of_work=unit_of_work,
        repository=lambda uow: uow.gadgets,
        rule=ModelActions(GADGETS, Asks()),
    )
    resource.command_route(
        RenameGadget, service=recorded, body=RenameIn, path="/rename"
    )
    resource.command_route(MakeGadget, service=recorded, body=MakeIn)
    client = client_of(resource)
    (gadget_id,) = store.items

    renaming = client.post(
        f"{LOCATION}{gadget_id}/rename/authorization",
        json={"name": "b"},
        headers=as_("changer"),
    )
    making = client.post(
        "/gadgets/authorization",
        json={"name": "n", "price": "1.00"},
        headers=as_("adder"),
    )

    assert (renaming.status_code, making.status_code) == (201, 201)
    assert ran == []
    assert [uow.commits for uow in made] == [0, 0]
    assert [gadget.name for gadget in store.items.values()] == ["a"]


def test_a_route_without_an_asking_rule_gets_no_twin(use_auth: UseAuth) -> None:
    """Nothing changes for a route whose rules ask for nothing (A6)."""
    use_auth()
    store = InMemoryGadgets([make_gadget("a")])
    resource = gadgets(store)
    resource.command_route(
        RenameGadget,
        service=rename,
        body=RenameIn,
        path="/rename",
        rule=IsAuthenticated() & ~HasPermissions("shop.view_cost"),
    )
    resource.command_route(DropGadget, service=drop, method="DELETE")
    client = client_of(resource)
    (gadget_id,) = store.items

    asked = client.post(
        f"{LOCATION}{gadget_id}/rename/authorization",
        json={"name": "b"},
        headers=as_("changer"),
    )

    assert asked.status_code == 404
    assert route_names(resource) == [
        "list_gadgets",
        "read_gadget",
        "rename_gadget",
        "drop_gadget",
    ]
    assert [each.name for each in resource.operations] == route_names(resource)


def test_an_asking_rule_inside_all_of_gets_a_twin_whose_other_rules_hold(
    use_auth: UseAuth,
) -> None:
    """At any depth of ``&``, in the route's rule or the resource's (A6)."""
    use_auth()
    deep = Asks()
    shared = Asks()
    store = InMemoryGadgets([make_gadget("a")])
    own = gadgets(store, rule=None)
    own.command_route(
        RenameGadget,
        service=rename,
        body=RenameIn,
        path="/rename",
        rule=IsAuthenticated() & (HasPermissions("shop.change_gadget") & deep),
    )
    resources = gadgets(store, rule=ModelActions(GADGETS, IsAuthenticated() & shared))
    resources.command_route(RenameGadget, service=rename, body=RenameIn, path="/r")
    client = client_of(own, resources)
    (gadget_id,) = store.items

    def ask(path: str, who: str | None) -> int:
        return client.post(
            f"{LOCATION}{gadget_id}/{path}/authorization",
            json={"name": "b"},
            headers=as_(who) if who else {},
        ).status_code

    assert [ask("rename", "changer"), ask("rename", "viewer")] == [201, 403]
    assert ask("rename", None) == 401
    assert [ask("r", "changer"), ask("r", "viewer")] == [201, 403]
    assert (len(deep.asked), len(shared.asked)) == (1, 1)


def test_an_asking_rule_inside_any_of_or_not_is_refused_at_declaration() -> None:
    """``|`` and ``~`` cannot decide an authorization: ImproperlyConfigured (A6)."""
    store = InMemoryGadgets([make_gadget("a")])
    resource = gadgets(store)
    before = route_names(resource)

    for rule, inside in (
        (IsAuthenticated() | Asks(), r"AnyOf \(\|\)"),
        (~Asks(), r"Not \(~\)"),
        (IsAuthenticated() & Not(IsAuthenticated() & Asks()), r"Not \(~\)"),
        (AllOf(IsAuthenticated(), IsAuthenticated() | (Asks() & Seen())), "AnyOf"),
    ):
        with pytest.raises(ImproperlyConfigured, match=f"rename_gadget.*{inside}"):
            resource.command_route(
                RenameGadget, service=rename, body=RenameIn, path="/rename", rule=rule
            )

    assert route_names(resource) == before


def test_two_asking_rules_on_one_route_are_refused_at_declaration() -> None:
    """One act takes one authorization: two rules that ask are refused (A6)."""
    store = InMemoryGadgets([make_gadget("a")])
    resource = gadgets(store, rule=ModelActions(GADGETS, Asks()))
    alone = gadgets(store)
    before = route_names(resource)

    with pytest.raises(
        ImproperlyConfigured,
        match=r"POST /gadgets/\{gadget_id\}/rename \(rename_gadget\) has 2 rules",
    ):
        resource.command_route(
            RenameGadget, service=rename, body=RenameIn, path="/rename", rule=Asks()
        )
    with pytest.raises(ImproperlyConfigured, match="Asks and Asks"):
        alone.command_route(
            RenameGadget, service=rename, body=RenameIn, rule=Asks() & Asks()
        )

    assert route_names(resource) == before


def test_twins_of_one_path_name_their_methods(use_auth: UseAuth) -> None:
    """A ``PATCH`` and a ``DELETE`` of one item both get a twin (user report A17).

    Each twin's path names its route's method, the first's moved there
    when the second is declared; one alone keeps ``<path>/authorization``.
    """
    use_auth()
    asks = Asks()
    store = InMemoryGadgets([make_gadget("a")])
    resource = gadgets(store)
    resource.command_route(
        UpdateGadget, service=update, body=GadgetChanges, method="PATCH", rule=asks
    )
    alone = route_names(resource)[-1], resource.routes[-1].path  # type: ignore[attr-defined]
    resource.command_route(DropGadget, service=drop, method="DELETE", rule=asks)
    resource.command_route(MakeGadget, service=make, body=MakeIn, rule=asks)
    resource.command_route(
        MakeGadget, service=make, body=MakeIn, method="PUT", name="put", rule=asks
    )
    client = client_of(resource)
    (gadget_id,) = store.items
    item = f"{LOCATION}{gadget_id}/authorization"

    patched = client.post(f"{item}/patch", json={"name": "z"}, headers=as_("changer"))
    dropped = client.post(f"{item}/delete", headers=as_("deleter"))
    bare = client.post(item, json={"name": "z"}, headers=as_("changer"))
    made = client.post(
        "/gadgets/authorization/post",
        json={"name": "n", "price": "1.00"},
        headers=as_("adder"),
    )
    put = client.post(
        "/gadgets/authorization/put",
        json={"name": "n", "price": "1.00"},
        headers=as_("changer"),
    )
    refused = client.post(f"{item}/delete", headers=as_("changer"))

    assert alone == (
        "update_gadget_authorization",
        "/gadgets/{gadget_id}/authorization",
    )
    assert [each.status_code for each in (patched, dropped, made, put)] == [201] * 4
    assert bare.status_code == 404
    assert refused.status_code == 403  # ModelActions: no delete_gadget.
    assert asks.asked == [
        UpdateGadget(gadget_id=gadget_id, name="z", stock=UNSET),
        DropGadget(gadget_id=gadget_id),
        MakeGadget(name="n", price=Decimal("1.00")),
        MakeGadget(name="n", price=Decimal("1.00")),
    ]
    assert {
        route.name: route.path
        for route in resource.routes
        if isinstance(route, APIRoute) and route.name.endswith("_authorization")
    } == {
        "update_gadget_authorization": "/gadgets/{gadget_id}/authorization/patch",
        "drop_gadget_authorization": "/gadgets/{gadget_id}/authorization/delete",
        "make_gadget_authorization": "/gadgets/authorization/post",
        "put_authorization": "/gadgets/authorization/put",
    }
    assert {
        each.name: each.path
        for each in resource.operations
        if each.name.endswith("_authorization")
    } == {
        "update_gadget_authorization": "/gadgets/{gadget_id}/authorization/patch",
        "drop_gadget_authorization": "/gadgets/{gadget_id}/authorization/delete",
        "make_gadget_authorization": "/gadgets/authorization/post",
        "put_authorization": "/gadgets/authorization/put",
    }
    assert sorted(each.name for each in resource.operations) == sorted(
        route_names(resource)
    )
    # The collection's twins are matched before the routes of one item.
    assert route_names(resource).index("put_authorization") < (
        route_names(resource).index("read_gadget")
    )
    assert [store.items[gadget_id].name, len(store.items)] == ["a", 1]


def test_twins_that_would_share_a_path_are_refused() -> None:
    """Two asking routes of one path and method, or a path taken (A6, A17)."""
    store = InMemoryGadgets([make_gadget("a")])
    resource = gadgets(store)

    async def taken() -> None:
        """A route of the project's own, where a twin would go."""

    resource.add_api_route("/{gadget_id}/authorization/delete", taken, methods=["POST"])
    resource.add_api_route(
        "/{gadget_id}/rename/authorization", taken, methods=["POST"], name="r"
    )
    resource.command_route(
        UpdateGadget, service=update, body=GadgetChanges, method="PATCH", rule=Asks()
    )
    before = route_names(resource)

    with pytest.raises(
        ImproperlyConfigured,
        match=r"PATCH /gadgets/\{gadget_id\} \(again\) asks for an authorization "
        r"at POST /gadgets/\{gadget_id\}/authorization/patch, which "
        r"update_gadget_authorization answers already",
    ):
        resource.command_route(
            UpdateGadget,
            service=update,
            body=GadgetChanges,
            method="PATCH",
            name="again",
            rule=Asks(),
        )
    with pytest.raises(
        ImproperlyConfigured,
        match=r"at POST /gadgets/\{gadget_id\}/authorization/delete, which taken",
    ):
        resource.command_route(DropGadget, service=drop, method="DELETE", rule=Asks())
    with pytest.raises(
        ImproperlyConfigured, match=r"/rename/authorization, which r answers"
    ):
        resource.command_route(
            RenameGadget, service=rename, body=RenameIn, path="/rename", rule=Asks()
        )

    assert route_names(resource) == before
    assert resource.routes[-1].path == "/gadgets/{gadget_id}/authorization"  # type: ignore[attr-defined]


def test_a_route_s_own_dependencies_run_on_the_act_not_its_twin(
    use_auth: UseAuth,
) -> None:
    """A limit in ``dependencies`` counts acts, after the rules (user report A14).

    The twin asks the rules and runs ``provide``, which tell its request
    apart with ``asking_authorization``; the route's own dependencies run on
    the act alone.
    """
    use_auth()
    asks = Asks()
    store = InMemoryGadgets([make_gadget("a")])
    counted: list[str] = []
    provided: list[bool] = []
    shared: list[bool] = []

    def count(request: Request) -> None:
        counted.append(request.url.path)

    def who(request: Request) -> str:
        provided.append(asking_authorization(request))
        return request.headers["x-user"]

    def everywhere(request: Request) -> None:
        shared.append(asking_authorization(request))

    resource = gadgets(store, dependencies=[Depends(everywhere)])
    resource.command_route(
        Tag,
        service=touch,
        body=RenameIn,
        path="/tag",
        provide={"who": who},
        rule=asks,
        dependencies=[
            Depends(count),
            Depends(Throttle("2/minute", scope="tags", backend=MemoryBackend())),
        ],
    )
    resource.use_case(
        restock, path="/restock", rule=asks, dependencies=[Depends(count)]
    )
    client = client_of(resource)
    (gadget_id,) = store.items
    act = f"{LOCATION}{gadget_id}/tag"

    def tag(*headers: str) -> int:
        return client.post(
            act,
            json={"name": "n"},
            headers=as_("changer", **dict.fromkeys(headers, "y")),
        ).status_code

    def ask() -> int:
        return client.post(
            f"{act}/authorization", json={"name": "n"}, headers=as_("changer")
        ).status_code

    asked = [ask(), ask(), ask()]
    unauthorized = tag()
    acts = [tag("X-Authorized"), tag("X-Authorized"), tag("X-Authorized")]
    stocked = client.post(
        f"{LOCATION}{gadget_id}/restock/authorization",
        json={"quantity": 1},
        headers=as_("changer"),
    )
    plain = client.get(f"{LOCATION}{gadget_id}", headers=as_("viewer"))

    assert asked == [201, 201, 201]
    assert unauthorized == 403  # Refused by its rules: not counted.
    assert acts == [200, 200, 429]
    assert (stocked.status_code, plain.status_code) == (201, 200)
    assert counted == [act] * 3
    assert provided == [True, True, True, False, False]
    assert shared == [True] * 3 + [False] * 4 + [True, False]
    assert len(asks.asked) == 4
    assert not asking_authorization(HTTPConnection({"type": "http"}))  # Unrouted.


def test_openapi_documents_the_twin_as_its_own_operation(use_auth: UseAuth) -> None:
    """Its name, summary, errors, body, and 201 schema; it declares a rule (A6)."""
    use_auth()
    store = InMemoryGadgets([make_gadget("a")])
    resource = gadgets(store, rule=None)
    resource.command_route(
        RenameGadget, service=rename, body=RenameIn, path="/rename", rule=Asks()
    )
    resource.command_route(MakeGadget, service=make, body=MakeIn, rule=Asks())
    app = FastAPI()
    app.include_router(resource)

    spec = app.openapi()
    twin = spec["paths"]["/gadgets/{gadget_id}/rename/authorization"]["post"]
    made = spec["paths"]["/gadgets/authorization"]["post"]
    route = next(
        each
        for each in resource.routes
        if isinstance(each, APIRoute) and each.name == "rename_gadget_authorization"
    )

    assert twin["operationId"].startswith("rename_gadget_authorization")
    assert made["operationId"].startswith("make_gadget_authorization")
    assert twin["summary"] == "Ask for an authorization: Rename a gadget."
    assert made["summary"] == "Ask for an authorization: Make a gadget."
    assert sorted(twin["responses"]) == ["201", "401", "403", "404", "408", "422"]
    assert sorted(made["responses"]) == ["201", "401", "403", "408", "422"]
    assert twin["responses"]["201"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/Asked"
    }
    assert twin["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/RenameIn"
    }
    assert [each["name"] for each in twin["parameters"]] == ["gadget_id"]
    assert route.status_code == 201
    # The resource declares no rule; its routes with one are not open.
    assert open_routes(app) == ["GET /gadgets", "GET /gadgets/{gadget_id}"]


def test_the_twin_holds_the_act_s_body_limit(use_auth: UseAuth) -> None:
    """The route's ``body_limit`` is its twin's: a larger body is 413 (A6)."""
    use_auth()
    asks = Asks()
    store = InMemoryGadgets([make_gadget("a")])
    resource = gadgets(store, body_limits={"command": 300})
    resource.command_route(
        RenameGadget,
        service=rename,
        body=RenameIn,
        path="/b",
        name="b",
        body_limit=50,
        rule=asks,
    )
    resource.command_route(MakeGadget, service=make, body=MakeIn, rule=Asks())
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
    (gadget_id,) = store.items

    large = client.post(
        f"{LOCATION}{gadget_id}/b/authorization",
        json={"name": "x" * 40},
        headers=as_("changer"),
    )
    small = client.post(
        f"{LOCATION}{gadget_id}/b/authorization",
        json={"name": "x"},
        headers=as_("changer"),
    )

    assert limits["b_authorization"] == limits["b"] == 50
    assert limits["make_gadget_authorization"] == limits["make_gadget"] == 300
    assert (large.status_code, small.status_code) == (413, 201)
    assert len(asks.asked) == 1
