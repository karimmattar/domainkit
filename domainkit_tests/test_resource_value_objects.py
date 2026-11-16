"""A command gets the dataclasses it declares, as the body validated them (U5).

Where a command field is declared as a dataclass, such as a value object,
or a tuple of one, and the body's field is typed as that same class (or a
tuple of it), the command gets the instances pydantic validated, not the
dicts ``model_dump()`` makes of them. Nothing else changes: schemas, what
a body may send, and every other field are as in 2.4.
"""

import dataclasses
import hashlib
from collections.abc import Mapping
from dataclasses import InitVar
from datetime import datetime
from typing import Annotated, Any, NewType
from uuid import UUID

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import BaseModel

from domainkit.ddd import (
    AggregateRoot,
    Command,
    EventBus,
    InMemoryRepository,
    MaxLength,
    UnitOfWork,
    ValueObject,
    use_case,
)
from domainkit.http.access import Public
from domainkit.http.errors import install_exception_handlers
from domainkit.resources import ALL_FIELDS, ResourceRouter, schema_for
from domainkit_tests.conftest import UseAuth
from domainkit_tests.gadgets import (
    FakeGadgetsUnitOfWork,
    InMemoryGadgets,
    make_gadget,
)
from domainkit_tests.test_resources import LOCATION, as_, client_of, gadgets

ItemId = NewType("ItemId", UUID)


class Place(ValueObject):
    """A value object of several fields, one of them bounded."""

    shelf: Annotated[str, MaxLength(4)]
    row: int


class Code(ValueObject):
    """A value object of one field."""

    value: Annotated[str, MaxLength(8)]


class Stop(ValueObject):
    """A value object that holds another, and a time."""

    code: Code
    at: datetime


class Node(ValueObject):
    """A value object that holds others of its kind."""

    label: str
    children: tuple["Node", ...] = ()


class Scaled(ValueObject):
    """A value object with a required ``InitVar``."""

    cents: int
    label: str
    unit: InitVar[str]

    def __post_init__(self, unit: str) -> None:
        """Count euros in cents."""
        if unit == "eur":
            object.__setattr__(self, "cents", self.cents * 100)


class Hashed(ValueObject):
    """A value object whose ``__post_init__`` must run once."""

    value: str

    def __post_init__(self) -> None:
        """Keep a digest of the value, not the value."""
        digest = hashlib.sha256(self.value.encode()).hexdigest()[:12]
        object.__setattr__(self, "value", digest)


class Checked(ValueObject):
    """A value object that checks with ``assert``."""

    row: int

    def __post_init__(self) -> None:
        """Refuse a negative row."""
        if self.row < 0:  # As ``assert`` raises it, which pydantic answers.
            raise AssertionError("A row is not negative.")


@dataclasses.dataclass(frozen=True)
class Point:
    """A plain dataclass."""

    x: int
    y: int


class Shelf(AggregateRoot[ItemId]):
    """An aggregate with tuples of value objects."""

    name: str
    places: tuple[Place, ...] = ()
    stops: tuple[Stop, ...] = ()


class Grove(AggregateRoot[ItemId]):
    """An aggregate of value objects that hold their own kind."""

    name: str
    nodes: tuple[Node, ...] = ()


class Measure(AggregateRoot[ItemId]):
    """An aggregate with a required field its class sets itself."""

    low: int
    high: int
    length: int = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        """Set the length."""
        self.length = self.high - self.low


class Items(InMemoryRepository[Any, ItemId]):
    """Aggregates in memory."""


class ItemsUnitOfWork(UnitOfWork):
    """A unit of work over ``Items``."""

    def __init__(self, items: Items) -> None:
        """Hold ``items``."""
        super().__init__(events=EventBus())
        self.items = items

    async def _commit(self) -> None:
        """Nothing to write."""

    async def _rollback(self) -> None:
        """Nothing to undo."""


def crud(model: type) -> TestClient:
    """A client of a ``crud=True`` resource of ``model``, open to anyone."""
    items = Items()
    resource = ResourceRouter(
        "/items",
        model=model,
        unit_of_work=lambda: ItemsUnitOfWork(items),
        repository=lambda uow: uow.items,
        dependencies=[Public],
        crud=True,
        writable=ALL_FIELDS,
    )
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(resource)
    return TestClient(app, raise_server_exceptions=False)


def served(
    command: type, body: type[BaseModel], seen: list[Any]
) -> tuple[TestClient, str]:
    """A client of a command route of ``command`` and ``body``; its path."""

    async def service(given: object, uow: object) -> None:
        seen.append(given)

    store = InMemoryGadgets([make_gadget("a")])
    resource = gadgets(store)
    resource.command_route(
        command, service=service, body=body, path="/act", returns=None
    )
    (gadget_id,) = store.items
    return TestClient(client_of(resource).app), f"{LOCATION}{gadget_id}/act"


def test_a_use_case_s_tuple_of_value_objects_comes_as_instances(
    use_auth: UseAuth,
) -> None:
    """``tuple[Place, ...]`` reaches the function as places, in a batch too.

    A value object of several fields alone comes as a dict, as its body
    field is a nested model; a ``list`` of them as dicts: as in 2.4.
    """
    use_auth()
    seen: list[dict[str, object]] = []

    @use_case
    async def keep(
        uow: FakeGadgetsUnitOfWork,
        gadget_id: UUID,
        places: tuple[Place, ...],
        codes: tuple[Code, ...] = (),
        points: tuple[Point, ...] = (),
        home: Place | None = None,
        listed: list[Place] | None = None,
    ) -> None:
        """Say where a gadget is kept."""
        seen.append(
            {
                "places": places,
                "codes": codes,
                "points": points,
                "home": home,
                "listed": listed,
            }
        )

    store = InMemoryGadgets([make_gadget("a")])
    resource = gadgets(store)
    resource.use_case(keep, path="/keep")
    resource.batch_route(
        keep.command,
        service=keep.run,
        body=schema_for(keep.command, write=True, include=["places"]),
        returns=None,
    )
    client = client_of(resource)
    (gadget_id,) = store.items
    place = {"shelf": "A", "row": 1}

    kept = client.post(
        f"{LOCATION}{gadget_id}/keep",
        json={
            "places": [place, {"shelf": "B", "row": 2}],
            "codes": [{"value": "c"}],
            "points": [{"x": 1, "y": 2}],
            "home": place,
            "listed": [place],
        },
        headers=as_("changer"),
    )
    batched = client.post(
        "/gadgets/batch",
        json={"items": [{"gadget_id": str(gadget_id), "places": [place]}]},
        headers=as_("changer"),
    )

    assert (kept.status_code, batched.json()["results"][0]["status"]) == (204, 204)
    assert seen == [
        {
            "places": (Place("A", 1), Place("B", 2)),
            "codes": (Code("c"),),
            "points": (Point(1, 2),),
            "home": place,
            "listed": [place],
        },
        {
            "places": (Place("A", 1),),
            "codes": (),
            "points": (),
            "home": None,
            "listed": None,
        },
    ]
    assert [type(each) for each in seen[0]["places"]] == [Place, Place]  # type: ignore[attr-defined]


class Loose(Command):
    """Fields that ask for anything but the classes the body validates."""

    gadget_id: UUID
    point: dict[str, int]
    points: tuple[dict[str, int], ...]
    place: Any
    places: Mapping[str, object]


class Typed(Command):
    """The same fields, declared as the classes the body validates."""

    gadget_id: UUID
    point: Point
    points: tuple[Point, ...]
    place: Place
    places: tuple[Place, ...]


class ClassesIn(BaseModel):
    """A hand-written body whose fields are typed as a dataclass and a value object."""

    point: Point
    points: tuple[Point, ...]
    place: Place
    places: Place


class TupleIn(BaseModel):
    """As ``ClassesIn``, with a tuple of places."""

    point: Point
    points: tuple[Point, ...]
    place: Place
    places: tuple[Place, ...]


class Later(Command):
    """A command whose hints do not resolve when its route is declared."""

    gadget_id: UUID
    places: tuple[Place, ...]
    later: "Undefined"  # type: ignore[name-defined]  # noqa: F821 - never resolves.


class LaterIn(BaseModel):
    """A body for ``Later``."""

    places: tuple[Place, ...]
    later: int


def test_a_class_the_body_validates_reaches_only_a_command_that_declares_it(
    use_auth: UseAuth,
) -> None:
    """The instance where the command declares its class; else ``model_dump()``.

    ``dict``, ``Any``, ``Mapping``, and a command whose hints do not
    resolve get the dicts 2.4 gave. The markers of an instance are
    unchecked, as in 2.4: pydantic builds it.
    """
    use_auth()
    seen: list[Any] = []
    loose, loose_path = served(Loose, ClassesIn, seen)
    typed, typed_path = served(Typed, TupleIn, seen)
    later, later_path = served(Later, LaterIn, seen)
    place = {"shelf": "LONGER", "row": 1}
    body = {"point": {"x": 1, "y": 2}, "points": [{"x": 3, "y": 4}], "place": place}

    answers = [
        loose.post(loose_path, json={**body, "places": place}, headers=as_("changer")),
        typed.post(
            typed_path, json={**body, "places": [place]}, headers=as_("changer")
        ),
        later.post(
            later_path, json={"places": [place], "later": 7}, headers=as_("changer")
        ),
    ]

    assert [each.status_code for each in answers] == [204, 204, 204]
    gadget_id = seen[0].gadget_id
    assert seen[0] == Loose(
        gadget_id=gadget_id,
        point={"x": 1, "y": 2},
        points=({"x": 3, "y": 4},),
        place=place,
        places=place,
    )
    assert (seen[1].point, seen[1].points) == (Point(1, 2), (Point(3, 4),))
    shelves = [seen[1].place, *seen[1].places]
    assert [(type(each), each.shelf) for each in shelves] == [(Place, "LONGER")] * 2
    assert (seen[2].places, seen[2].later) == ((place,), 7)
    assert [type(each) for each in vars(seen[0]).values()][1:] == [
        dict,
        tuple,
        dict,
        dict,
    ]


class Weigh(Command):
    """Takes a value object with a required ``InitVar``."""

    gadget_id: UUID
    scaled: Scaled


class WeighIn(BaseModel):
    """A hand-written body that types its field as the value object."""

    scaled: Scaled


class Keep(Command):
    """Takes a value object whose ``__post_init__`` must run once."""

    gadget_id: UUID
    secret: Hashed
    checked: Checked | None = None


class KeepIn(BaseModel):
    """A hand-written body that types its fields as the value objects."""

    secret: Hashed
    checked: Checked | None = None


def test_value_objects_are_taken_as_pydantic_built_them(use_auth: UseAuth) -> None:
    """Never built again: an ``InitVar`` is honoured, ``__post_init__`` runs once.

    A check that raises ``AssertionError`` is pydantic's ``422``, as in 2.4.
    """
    use_auth()
    seen: list[Any] = []
    weigh, weigh_path = served(Weigh, WeighIn, seen)
    keep, keep_path = served(Keep, KeepIn, seen)

    weighed = weigh.post(
        weigh_path,
        json={"scaled": {"cents": 5, "label": "x", "unit": "eur"}},
        headers=as_("changer"),
    )
    kept = keep.post(
        keep_path, json={"secret": {"value": "secret"}}, headers=as_("changer")
    )
    refused = keep.post(
        keep_path,
        json={"secret": {"value": "s"}, "checked": {"row": -1}},
        headers=as_("changer"),
    )

    assert (weighed.status_code, kept.status_code) == (204, 204)
    assert seen[0].scaled.cents == 500
    assert seen[1].secret == Hashed("secret")
    assert (refused.status_code, refused.json()["errors"][0]["loc"]) == (
        422,
        ["body", "checked"],
    )


def test_a_use_case_s_value_object_with_an_init_var_is_taken(use_auth: UseAuth) -> None:
    """A derived body takes such a value object as 2.4 did (a dict of its fields)."""
    use_auth()
    seen: list[object] = []

    @use_case
    async def weigh(
        uow: FakeGadgetsUnitOfWork, gadget_id: UUID, scaled: Scaled
    ) -> None:
        """Weigh."""
        seen.append(scaled)

    store = InMemoryGadgets([make_gadget("a")])
    resource = gadgets(store)
    resource.use_case(weigh, path="/weigh")
    client = client_of(resource)
    (gadget_id,) = store.items

    answer = client.post(
        f"{LOCATION}{gadget_id}/weigh",
        json={"scaled": {"cents": 5, "label": "x"}},
        headers=as_("changer"),
    )

    assert answer.status_code == 204
    assert seen == [{"cents": 5, "label": "x"}]


def test_crud_takes_tuples_of_value_objects_as_reads_show_them() -> None:
    """A create, and a change, of ``tuple[Stop, ...]``: the shape a read shows."""
    client = crud(Shelf)
    stops = [{"code": {"value": "c1"}, "at": "2026-01-01T00:00:00Z"}]

    created = client.post("/items", json={"name": "s", "stops": stops})
    shown = client.get(f"/items/{created.json()['id']}").json()
    again = client.post("/items", json={"name": "t", "stops": shown["stops"]})
    changed = client.patch(
        f"/items/{created.json()['id']}", json={"places": [{"shelf": "A", "row": 1}]}
    )

    assert (created.status_code, again.status_code) == (201, 201)
    assert (changed.status_code, changed.json()["places"]) == (
        200,
        [{"shelf": "A", "row": 1}],
    )


def test_schemas_are_derived_as_in_2_4() -> None:
    """A tree of value objects, and a field the class sets itself, have schemas."""
    schema_for(Grove, write=True)
    schema_for(Grove, partial=True)
    crud(Grove)
    crud(Measure)

    @use_case
    async def plant(uow: FakeGadgetsUnitOfWork, gadget_id: UUID, root: Node) -> None:
        """Plant."""

    gadgets(InMemoryGadgets([make_gadget("a")])).use_case(plant, path="/plant")

    assert list(schema_for(Measure, write=True).model_fields) == [
        "low",
        "high",
        "length",
    ]
    assert list(schema_for(Measure, partial=True, include=["length"]).model_fields) == [
        "length"
    ]
    assert schema_for(Shelf, write=True).model_json_schema()["properties"]["places"][
        "items"
    ] == {"$ref": "#/$defs/Place"}


def test_a_batch_item_that_raises_a_validation_error_is_its_422(
    use_auth: UseAuth,
) -> None:
    """The item answers ``422 validation_error``, as a request of its own would."""
    use_auth()

    @use_case
    async def mark(uow: FakeGadgetsUnitOfWork, gadget_id: UUID, label: str) -> None:
        """Refuse an empty label, as a request's validation does."""
        if not label:
            raise RequestValidationError(
                [{"type": "missing", "loc": ("body", "label")}]
            )

    store = InMemoryGadgets([make_gadget("a")])
    resource = gadgets(store)
    resource.batch_route(
        mark.command,
        service=mark.run,
        body=schema_for(mark.command, write=True, include=["label"]),
        returns=None,
    )
    client = client_of(resource)
    (gadget_id,) = store.items

    answer = client.post(
        "/gadgets/batch",
        json={
            "items": [
                {"gadget_id": str(gadget_id), "label": "x"},
                {"gadget_id": str(gadget_id), "label": ""},
            ]
        },
        headers=as_("changer"),
    )

    assert answer.status_code == 207
    assert answer.json()["results"] == [
        {"status": 204},
        {
            "status": 422,
            "error": {"code": "validation_error", "detail": "The request is invalid."},
        },
    ]


def test_a_command_whose_hints_raise_type_error_gets_dicts() -> None:
    """A hint that cannot be resolved (a quoted name in a union) is 2.4's dicts."""
    from domainkit.resources.routing.values import instances

    class Body(BaseModel):
        owner: dict[str, str] | None = None

    @dataclasses.dataclass
    class Broken:
        owner: "'Owner' | None" = None  # noqa: F821 - resolving it raises TypeError.

    assert instances(Broken, Body) == frozenset()
