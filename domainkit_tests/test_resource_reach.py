"""What each route may reach: scopes, rules on objects, parents, and links.

Every way into an aggregate (generic writes, queries, batches, ``fetch``,
nested routes) asks the same questions of the request: is it in the
caller's scope, may the caller act on it, is it the parent's.
"""

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from starlette.requests import HTTPConnection

from domainkit.contrib.auth import AnyUser
from domainkit.ddd import (
    Command,
    DomainEvent,
    EventBus,
    InMemoryRepository,
    UnitOfWork,
)
from domainkit.ddd import Query as QueryMessage
from domainkit.ddd.query import F
from domainkit.http import Filter, FilterSet
from domainkit.http.access import BasePermission, IsAuthenticated, Public
from domainkit.resources import (
    ALL_FIELDS,
    FieldPolicy,
    ModelActions,
    RecordChanged,
    RecordDeleted,
    ResourceRouter,
    to_domain,
)
from domainkit.resources.access import content_type_of
from domainkit.resources.routing.values import first_line
from domainkit_tests.conftest import AUTH, CONTENTTYPES, SHOP, UseAuth
from domainkit_tests.gadgets import (
    FakeGadgetsUnitOfWork,
    Gadget,
    GadgetId,
    InMemoryGadgets,
    make_gadget,
)
from domainkit_tests.sample_domains.shop.models import Product
from domainkit_tests.test_auth_access import Never
from domainkit_tests.test_resources import (
    GADGETS,
    LOCATION,
    Dearest,
    GadgetAgain,
    MakeGadget,
    MakeIn,
    Receipt,
    RenameGadget,
    RenameIn,
    Review,
    ReviewIn,
    Summary,
    WriteReview,
    as_,
    client_of,
    dearest,
    gadgets,
    receipt,
    rename,
    shadow,
)


class NotC(BasePermission):
    """Refuses the gadget named c, and an object it is not shown."""

    async def has_object_permission(
        self, request: HTTPConnection, user: AnyUser, obj: object
    ) -> bool:
        """Anything but c, asked with a request."""
        assert request.headers is not None
        return obj is not None and getattr(obj, "name", "") != "c"


async def hide_b(request: HTTPConnection) -> Any:  # noqa: ANN401 - criteria.
    """Out of scope: the gadget the request names in X-Hide, b by default."""
    return F("name") != request.headers.get("x-hide", "b")


@pytest.fixture
def store() -> InMemoryGadgets:
    """Gadgets a (1.00), b (2.00), and c (3.00)."""
    return InMemoryGadgets(
        [
            make_gadget("a", price="1.00"),
            make_gadget("b", price="2.00"),
            make_gadget("c", price="3.00"),
        ]
    )


def ids(store: InMemoryGadgets) -> dict[str, UUID]:
    """Gadget ids by name."""
    return {gadget.name: gadget.id for gadget in store.items.values()}


async def first(query: GadgetAgain, uow: FakeGadgetsUnitOfWork) -> Gadget:
    """Answer with gadget a, whichever the path names."""
    async with uow:
        found = await uow.gadgets.first(F("name") == "a")
    assert found is not None
    return found


def test_every_route_asks_the_scope_and_the_rules_about_its_aggregate(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """Out of scope is 404 and a refused object 403, however it is reached."""
    use_auth()
    resource = gadgets(
        store,
        rule=ModelActions(GADGETS, NotC()),
        scope=hide_b,
        crud=True,
        writable=ALL_FIELDS,
    )
    resource.query_route(GadgetAgain, handler=again_or_first, path="/again")
    resource.query_route(GadgetAgain, handler=first, path="/first", name="first")
    resource.query_route(Dearest, handler=dearest, path="/dearest")
    resource.batch_route(RenameGadget, service=rename, body=RenameIn)

    @resource.get("/{gadget_id}/name", dependencies=[Public])
    async def name_of(request: Request, gadget_id: UUID) -> str:
        found = await resource.fetch(request, FakeGadgetsUnitOfWork(store), gadget_id)
        return str(getattr(found, "name"))  # noqa: B009 - any aggregate.

    client = client_of(resource)
    known = ids(store)
    root = as_("root")

    def status(method: str, name: str, path: str = "", **body: Any) -> int:  # noqa: ANN401 - JSON.
        url = f"{LOCATION}{known[name]}{path}"
        answer = client.request(method, url, json=body or None, headers=root)
        return answer.status_code

    assert [status("PATCH", name, stock=1) for name in "abc"] == [200, 404, 403]
    assert [status("GET", name, "/again") for name in "abc"] == [200, 404, 403]
    assert status("GET", "c", "/first") == 403
    assert [status("GET", name, "/name") for name in "abc"] == [200, 404, 403]
    dearest_one = client.get("/gadgets/dearest", headers=root)
    assert dearest_one.status_code == 403
    hidden = client.get("/gadgets/dearest", headers=as_("root", **{"X-Hide": "c"}))
    assert hidden.status_code == 404
    assert str(known["c"]) in hidden.json()["detail"]
    batch = client.post(
        "/gadgets/batch",
        json={
            "items": [
                {"gadget_id": str(known[name]), "name": f"{name}2"} for name in "abc"
            ]
        },
        headers=root,
    )
    assert [each["status"] for each in batch.json()["results"]] == [200, 404, 403]
    assert [status("DELETE", name) for name in "abc"] == [204, 404, 403]
    missing = client.get(f"{LOCATION}{(unknown := uuid4())}", headers=root)
    assert str(unknown) in missing.json()["detail"]


class ObjectsWhen:
    """Any authenticated user; ``actions`` also ask ``NotC`` about the gadget."""

    def __init__(self, *actions: str) -> None:
        """Ask about objects for ``actions``."""
        self.actions = actions

    def rules_for(self, action: str) -> tuple[Any, ...]:
        """``NotC`` for the actions named."""
        return (IsAuthenticated(), *([NotC()] if action in self.actions else []))


def statuses_under(store: InMemoryGadgets, *actions: str) -> dict[str, int]:
    """What each route answers root about gadget c, under a rule on ``actions``."""
    root = as_("root")
    c = ids(store)["c"]
    item = f"{LOCATION}{c}"
    resource = gadgets(
        store, rule=ObjectsWhen(*actions), crud=True, writable=ALL_FIELDS
    )
    resource.command_route(RenameGadget, service=rename, body=RenameIn, path="/rename")
    resource.batch_route(RenameGadget, service=rename, body=RenameIn)
    resource.query_route(GadgetAgain, handler=again_or_first, path="/again")

    @resource.get("/{gadget_id}/fetched", dependencies=[Public])
    async def fetched(request: Request, gadget_id: UUID) -> str:
        found = await resource.fetch(request, FakeGadgetsUnitOfWork(store), gadget_id)
        return str(getattr(found, "name"))  # noqa: B009 - any aggregate.

    reviews_uow, _, _ = nested_store(store)
    nested = ResourceRouter(
        "/reviews",
        model=Review,
        unit_of_work=reviews_uow,
        repository=lambda uow: uow.reviews,
        parent=resource,
        parent_field="gadget",
        rule=IsAuthenticated(),
    )
    client = client_of(resource, nested)
    batch = client.post(
        "/gadgets/batch",
        json={"items": [{"gadget_id": str(c), "name": "c"}]},
        headers=root,
    )
    return {
        "read": client.get(item, headers=root).status_code,
        "query": client.get(f"{item}/again", headers=root).status_code,
        "fetch": client.get(f"{item}/fetched", headers=root).status_code,
        "nested": client.get(f"{item}/reviews", headers=root).status_code,
        "command": client.post(
            f"{item}/rename", json={"name": "c"}, headers=root
        ).status_code,
        "batch": batch.json()["results"][0]["status"],
        "patch": client.patch(item, json={"stock": 1}, headers=root).status_code,
    }


SEEN = ("read", "query", "fetch", "nested")
CHANGED = ("command", "batch", "patch")


@pytest.mark.parametrize(
    ("action", "refused"),
    [("view", SEEN), ("change", CHANGED), ("delete", ())],
)
def test_objects_are_asked_the_rules_of_the_routes_action(
    store: InMemoryGadgets, use_auth: UseAuth, action: str, refused: tuple[str, ...]
) -> None:
    """A rule on objects for one action refuses its routes and leaves the others."""
    use_auth()

    statuses = statuses_under(store, action)

    assert statuses == {
        route: 403 if route in refused else 200 for route in (*SEEN, *CHANGED)
    }


def test_a_rule_on_deleting_objects_refuses_their_deletion(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """The generic DELETE asks the rule for ``delete``."""
    use_auth()
    resource = gadgets(
        store, rule=ObjectsWhen("delete"), crud=True, writable=ALL_FIELDS
    )

    deleted = client_of(resource).delete(
        f"{LOCATION}{ids(store)['c']}", headers=as_("root")
    )

    assert deleted.status_code == 403


async def again_or_first(query: GadgetAgain, uow: FakeGadgetsUnitOfWork) -> Gadget:
    """The gadget named."""
    async with uow:
        return await uow.gadgets.get(GadgetId(query.gadget_id))


class PolishReview(Command):
    """A command on one review that does not name its gadget."""

    review_id: UUID


class ReviewAgain(QueryMessage):
    """One review, without its gadget."""

    review_id: UUID


class ReviewsBy(QueryMessage):
    """The reviews of the path's gadget with a text."""

    gadget: UUID
    text: str


def nested_store(store: InMemoryGadgets) -> tuple[Any, Review, Review]:
    """A unit of work over reviews of a and of b."""
    known = ids(store)
    mine = Review(id=uuid4(), gadget=GadgetId(known["a"]), text="good")
    theirs = Review(id=uuid4(), gadget=GadgetId(known["c"]), text="bad")
    reviews = InMemoryRepository[Review, UUID]([mine, theirs])

    class Reviews(UnitOfWork):
        def __init__(self) -> None:
            super().__init__(events=EventBus())
            self.reviews = reviews

        async def _commit(self) -> None:
            """Nothing to write."""

        async def _rollback(self) -> None:
            """Nothing to undo."""

    return Reviews, mine, theirs


@dataclass(frozen=True)
class Nested:
    """Reviews nested under gadgets, the caller's and another's."""

    client: TestClient
    store: InMemoryGadgets
    mine: Review
    theirs: Review

    @property
    def base(self) -> str:
        """The reviews of the caller's gadget."""
        return f"/gadgets/{self.mine.gadget}/reviews"

    @property
    def links(self) -> dict[str, str]:
        """The links of the caller's review."""
        return {
            "self": f"{self.base}/{self.mine.id}",
            "parent": f"/gadgets/{self.mine.gadget}",
        }


@pytest.fixture
def nested(store: InMemoryGadgets, use_auth: UseAuth) -> Nested:
    """Generic writes, queries, batches, and exports of reviews, under a gadget."""
    use_auth()
    reviews_uow, mine, theirs = nested_store(store)
    parent = gadgets(store, scope=hide_b)

    async def polish(command: PolishReview, uow: Any) -> Review:  # noqa: ANN401 - the unit.
        async with uow:
            return await uow.reviews.get(command.review_id)  # type: ignore[no-any-return]

    async def again(query: ReviewAgain, uow: Any) -> Review:  # noqa: ANN401 - the unit.
        async with uow:
            return await uow.reviews.get(query.review_id)  # type: ignore[no-any-return]

    async def by_text(query: ReviewsBy, uow: Any) -> list[Review]:  # noqa: ANN401 - the unit.
        async with uow:
            return await uow.reviews.filter(  # type: ignore[no-any-return]
                (F("gadget") == query.gadget) & (F("text") == query.text)
            )

    async def write(command: WriteReview, uow: Any) -> Review:  # noqa: ANN401 - the unit.
        review = Review(id=uuid4(), gadget=GadgetId(command.gadget), text=command.text)
        async with uow:
            await uow.reviews.add(review)
            await uow.commit()
        return review

    router = ResourceRouter(
        "/reviews",
        model=Review,
        unit_of_work=reviews_uow,
        repository=lambda uow: uow.reviews,
        parent=parent,
        parent_field="gadget",
        rule=IsAuthenticated(),
        links=True,
        crud=True,
        writable=ALL_FIELDS,
        exports=("ndjson",),
    )
    router.command_route(PolishReview, service=polish, path="/polish")
    router.query_route(ReviewAgain, handler=again, path="/again")
    router.query_route(ReviewsBy, handler=by_text, path="/by-text")
    router.batch_route(PolishReview, service=polish, path="/polish-all")
    router.batch_route(WriteReview, service=write, body=ReviewIn, path="/write-all")
    return Nested(client_of(parent, router), store, mine, theirs)


def test_a_nested_create_takes_its_parent_from_the_path(nested: Nested) -> None:
    """201, a location under the parent, and a link to it."""
    created = nested.client.post(nested.base, json={"text": "new"}, headers=as_("root"))

    assert created.status_code == 201
    assert created.headers["location"].startswith(f"{nested.base}/")
    assert created.json()["links"]["parent"] == f"/gadgets/{nested.mine.gadget}"
    assert created.json()["gadget"] == str(nested.mine.gadget)


def test_a_nested_item_never_moves_to_another_parent(nested: Nested) -> None:
    """Creating or changing it with another parent's id is refused with 422."""
    gadget = nested.mine.gadget
    elsewhere = {"gadget": str(nested.theirs.gadget)}

    moved = nested.client.post(
        nested.base, json={**elsewhere, "text": "x"}, headers=as_("root")
    )
    moving = nested.client.patch(
        f"{nested.base}/{nested.mine.id}", json=elsewhere, headers=as_("root")
    )

    assert (moved.status_code, moving.status_code) == (422, 422)
    assert nested.mine.gadget == gadget


def test_a_nested_change_links_to_the_item_it_changed(nested: Nested) -> None:
    """A PATCH of a review made through the route answers it at its location."""
    item = nested.client.post(
        nested.base, json={"text": "new"}, headers=as_("root")
    ).headers["location"]

    patched = nested.client.patch(item, json={"text": "newer"}, headers=as_("root"))

    assert (patched.status_code, patched.json()["links"]["self"]) == (200, item)


def test_a_nested_command_reaches_only_its_parents_items(nested: Nested) -> None:
    """The caller's review answers with its links; another gadget's is 404."""
    polished = nested.client.post(
        f"{nested.base}/{nested.mine.id}/polish", headers=as_("root")
    )
    foreign = nested.client.post(
        f"{nested.base}/{nested.theirs.id}/polish", headers=as_("root")
    )

    assert polished.json()["links"] == nested.links
    assert foreign.status_code == 404


def test_nested_queries_link_what_they_find(nested: Nested) -> None:
    """A query of one review, and one of many, link each to itself and its parent."""
    one = nested.client.get(
        f"{nested.base}/{nested.mine.id}/again", headers=as_("root")
    )
    found = nested.client.get(
        f"{nested.base}/by-text", params={"text": "good"}, headers=as_("root")
    )

    assert one.json()["links"] == nested.links
    assert [each["links"] for each in found.json()["items"]] == [nested.links]


def test_a_nested_batch_reaches_only_its_parents_items(nested: Nested) -> None:
    """Each item answers for itself: the caller's 200 with links, another's 404."""
    batch = nested.client.post(
        f"{nested.base}/polish-all",
        json={
            "items": [
                {"review_id": str(nested.mine.id)},
                {"review_id": str(nested.theirs.id)},
            ]
        },
        headers=as_("root"),
    )

    results = batch.json()["results"]
    assert [each["status"] for each in results] == [200, 404]
    assert results[0]["item"]["links"] == nested.links
    assert "location" not in results[0]


def test_a_nested_batch_of_creates_gives_each_its_parent_and_location(
    nested: Nested,
) -> None:
    """201, the parent from the path, and the item's location under it."""
    written = nested.client.post(
        f"{nested.base}/write-all",
        json={"items": [{"text": "many"}]},
        headers=as_("root"),
    )

    (made,) = written.json()["results"]
    assert (made["status"], made["item"]["gadget"]) == (201, str(nested.mine.gadget))
    assert made["location"] == f"{nested.base}/{made['item']['id']}"


def test_a_nested_export_links_each_line(nested: Nested) -> None:
    """NDJSON lines carry the same links as the items."""
    lines = nested.client.get(
        nested.base, headers=as_("root", Accept="application/x-ndjson")
    )

    exported = [json.loads(line) for line in lines.text.splitlines()]
    assert nested.links in [each["links"] for each in exported]


def test_the_items_of_a_hidden_parent_are_not_found(nested: Nested) -> None:
    """A parent the caller cannot reach hides its items' list too: 404."""
    hidden = nested.client.get(
        f"/gadgets/{ids(nested.store)['b']}/reviews", headers=as_("root")
    )

    assert hidden.status_code == 404


class Undocumented(Command):  # noqa: D101 - none, on purpose.
    gadget_id: UUID
    name: str


class Blank(Command):
    """Blanked below."""

    gadget_id: UUID
    name: str


Blank.__doc__ = "   "


def test_routes_take_rules_of_their_own_and_summaries_from_docstrings(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """``rule=`` on a command, query, or batch; no docstring, no summary."""
    use_auth()
    resource = gadgets(store)
    resource.command_route(
        RenameGadget, service=rename, body=RenameIn, path="/never", rule=Never()
    )
    resource.query_route(Dearest, handler=dearest, path="/never", rule=Never())
    resource.batch_route(RenameGadget, service=rename, body=RenameIn, rule=Never())
    resource.command_route(
        Undocumented, service=rename, body=RenameIn, path="/undocumented"
    )
    resource.command_route(Blank, service=rename, body=RenameIn, path="/blank")
    client = client_of(resource)
    gadget = ids(store)["a"]
    root = as_("root")

    assert (
        client.post(f"{LOCATION}{gadget}/never", json={"name": "x"}, headers=root)
    ).status_code == 403
    assert client.get("/gadgets/never", headers=root).status_code == 403
    assert (
        client.post("/gadgets/batch", json={"items": []}, headers=root).status_code
        == 403
    )
    paths = client.get("/openapi.json").json()["paths"]
    # Without a docstring, FastAPI names the route instead.
    assert (
        paths["/gadgets/{gadget_id}/undocumented"]["post"]["summary"] == "Undocumented"
    )
    assert paths["/gadgets/{gadget_id}/blank"]["post"]["summary"] == "Blank"
    assert (first_line(None), first_line("  "), first_line(" A.\nB")) == (
        None,
        None,
        "A.",
    )


def test_answers_in_other_schemas_from_aggregates_and_batches(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """``returns=`` shapes an aggregate's answer; batches answer as commands."""
    use_auth()
    resource = gadgets(store)
    resource.command_route(
        RenameGadget, service=rename, body=RenameIn, path="/brief", returns=Summary
    )
    resource.batch_route(
        RenameGadget, service=rename, body=RenameIn, path="/briefs", returns=Summary
    )
    resource.batch_route(RenameGadget, service=shadow, body=RenameIn, path="/shadows")
    resource.batch_route(MakeGadget, service=receipt, body=MakeIn, returns=Receipt)
    client = client_of(resource)
    gadget = ids(store)["a"]
    root = as_("root")
    item = {"items": [{"gadget_id": str(gadget), "name": "z"}]}

    brief = client.post(f"{LOCATION}{gadget}/brief", json={"name": "y"}, headers=root)
    briefs = client.post("/gadgets/briefs", json=item, headers=root).json()
    shadows = client.post("/gadgets/shadows", json=item, headers=root).json()
    receipts = client.post(
        "/gadgets/batch",
        json={"items": [{"name": "n", "price": "1.00"}]},
        headers=root,
    ).json()

    assert (brief.status_code, brief.json()) == (200, {"name": "y"})
    assert briefs["results"] == [{"status": 200, "item": {"name": "z"}}]
    assert shadows["results"][0]["item"]["id"] == str(gadget)
    assert "location" not in shadows["results"][0]
    assert receipts["results"] == [
        {
            "status": 201,
            "item": {"id": str(UUID(int=7)), "at": "2026-01-02T00:00:00Z"},
        }
    ]


class Restock(Command):
    """Set a gadget's stock, and who asked."""

    gadget_id: UUID
    stock: int
    who: str
    note: str = ""


def test_batch_declarations_take_bounds_defaults_and_dependencies(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """One item may be the bound; defaults need no body; ``provide`` fills."""
    use_auth()
    restocked: list[Restock] = []

    async def restock(command: Restock, uow: FakeGadgetsUnitOfWork) -> Gadget:
        restocked.append(command)
        async with uow:
            return await uow.gadgets.get(GadgetId(command.gadget_id))

    from pydantic import BaseModel

    class Level(BaseModel):
        stock: int

    class Sneaky(BaseModel):
        stock: int
        if_match: str

    resource = gadgets(store)
    resource.batch_route(
        Restock,
        service=restock,
        body=Level,
        max_items=1,
        provide={"who": lambda: "ann"},
    )
    client = client_of(resource)
    gadget = ids(store)["a"]
    root = as_("root")

    one = client.post(
        "/gadgets/batch",
        json={"items": [{"gadget_id": str(gadget), "stock": 3}]},
        headers=root,
    )
    two = client.post(
        "/gadgets/batch",
        json={"items": [{"gadget_id": str(gadget), "stock": 3}] * 2},
        headers=root,
    )

    assert one.status_code == 207
    assert restocked == [Restock(gadget_id=gadget, stock=3, who="ann")]
    assert two.status_code == 422
    with pytest.raises(TypeError, match="if_match, out of the body"):
        resource.batch_route(Restock, service=restock, body=Sneaky)


def test_listings_ignore_accept_without_exports(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """A resource without exports answers JSON, whatever is asked, without Vary."""
    use_auth()
    client = client_of(gadgets(store))

    answer = client.get("/gadgets", headers=as_("viewer", Accept="text/csv"))

    assert answer.headers["content-type"] == "application/json"
    assert "vary" not in answer.headers


def test_generic_writes_record_what_and_which(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """``RecordChanged`` and ``RecordDeleted`` name the model and the record."""
    use_auth()
    published: list[DomainEvent] = []
    client = client_of(gadgets(store, published, crud=True, writable=ALL_FIELDS))
    gadget = ids(store)["a"]

    client.patch(f"{LOCATION}{gadget}", json={"stock": 4}, headers=as_("changer"))
    client.delete(f"{LOCATION}{gadget}", headers=as_("deleter"))

    changed, deleted = published
    assert isinstance(changed, RecordChanged)
    assert isinstance(deleted, RecordDeleted)
    assert (changed.model, changed.record_id) == ("Gadget", str(gadget))
    assert (deleted.model, deleted.record_id) == ("Gadget", str(gadget))


def test_declarations_name_what_is_wrong(store: InMemoryGadgets) -> None:
    """Lazy models need a basename; policies name the schema; filters with -."""

    class Hidden(FilterSet):
        model = Gadget
        ordering_fields = ("stock",)
        default_ordering = ("-stock",)

    with pytest.raises(TypeError, match="needs a schema and a basename"):
        ResourceRouter(
            "/gadgets",
            model=lambda: Gadget,
            unit_of_work=FakeGadgetsUnitOfWork,
            repository=lambda uow: uow.gadgets,
            schema=Summary,
        )
    with pytest.raises(TypeError, match="GadgetOut"):
        gadgets(store, policy=FieldPolicy(visible={"nope": "x.y"}))

    class SortedByIt(FilterSet):
        model = Gadget
        default_ordering = ("-stock",)

    class ByPart(FilterSet):
        model = Gadget
        sku = Filter(field="parts.sku")

    for filters, field in ((Hidden, "stock"), (SortedByIt, "stock"), (ByPart, "parts")):
        with pytest.raises(TypeError, match=f"may not see: {field}"):
            gadgets(
                store,
                filters=filters,
                policy=FieldPolicy(visible={field: "shop.view_cost"}),
            )
    with pytest.raises(TypeError, match="1 item or more"):
        gadgets(store).batch_route(RenameGadget, service=rename, max_items=0)
    gadgets(store).batch_route(RenameGadget, service=rename, body=RenameIn, max_items=1)


def test_values_of_other_fields_pass_through_to_domain() -> None:
    """Mappings for fields that are not value objects stay as they are."""
    from domainkit.ddd.fields import fields_of
    from domainkit_tests.test_resource_schemas import Order

    extra = fields_of(Order)["extra"]

    assert to_domain(extra, {"a": "b"}) == {"a": "b"}
    assert to_domain(extra, None) is None


def test_content_types_come_from_classes_labels_and_functions(
    use_auth: UseAuth,
) -> None:
    """A mapped class, its label, or a function returning the class."""
    use_auth(
        settings={
            "INSTALLED_DOMAINS": [
                CONTENTTYPES,
                AUTH,
                SHOP,
                "domainkit_tests.sample_domains.alpha",
            ]
        }
    )

    assert content_type_of(Product) == ("shop", "product")
    assert content_type_of(lambda: Product) == ("shop", "product")
    assert content_type_of("shop.product") == ("shop", "product")


def test_generic_writes_refuse_schemas_that_move_children(
    store: InMemoryGadgets,
) -> None:
    """A create or update schema of a nested resource cannot hold the parent."""
    from domainkit.resources import schema_for

    reviews_uow, _, _ = nested_store(store)
    choices: list[dict[str, Any]] = [
        {"create": schema_for(Review, write=True, include=["gadget", "text"])},
        {"update": schema_for(Review, partial=True, include=["gadget"])},
    ]
    for options in choices:
        with pytest.raises(TypeError, match="not gadget"):
            ResourceRouter(
                "/reviews",
                model=Review,
                unit_of_work=reviews_uow,
                repository=lambda uow: uow.reviews,
                parent=gadgets(store),
                parent_field="gadget",
                rule=IsAuthenticated(),
                crud=True,
                writable=ALL_FIELDS,
                **options,
            )


class Touch(Command):
    """A command with nothing but the gadget's id."""

    gadget_id: UUID


def test_batch_items_without_a_body_take_nothing_else(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """Their schema is the id and ``if_match``; other keys are refused."""
    use_auth()

    async def touch(command: Touch, uow: FakeGadgetsUnitOfWork) -> Gadget:
        async with uow:
            return await uow.gadgets.get(GadgetId(command.gadget_id))

    resource = gadgets(store)
    resource.batch_route(Touch, service=touch)
    client = client_of(resource)
    gadget = ids(store)["a"]

    def post(item: dict[str, str]) -> int:
        return client.post(
            "/gadgets/batch", json={"items": [item]}, headers=as_("root")
        ).status_code

    assert post({"gadget_id": str(gadget)}) == 207
    assert post({"gadget_id": str(gadget), "name": "x"}) == 422
