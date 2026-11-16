"""``@use_case``: one async function is the command, the service, and the route."""

import dataclasses
import logging
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, Field, ValidationError, field_serializer

from domainkit.ddd import (
    Command,
    EventBus,
    InMemoryRepository,
    InvalidField,
    MaxLength,
    MinLength,
    Range,
    UnitOfWork,
    UseCase,
    ValueObject,
    use_case,
)
from domainkit.ddd.fields import fields_of
from domainkit.http.access import IsAuthenticated
from domainkit.log import JsonFormatter
from domainkit.money import Money
from domainkit.resources import ResourceRouter, schema_for, to_domain
from domainkit_tests.conftest import UseAuth
from domainkit_tests.gadgets import (
    FakeGadgetsUnitOfWork,
    Gadget,
    GadgetId,
    InMemoryGadgets,
    make_gadget,
)
from domainkit_tests.test_resources import LOCATION, Review, as_, client_of, gadgets


class Boom(Exception):
    """Raised inside a use case."""


@use_case
async def restock(
    uow: FakeGadgetsUnitOfWork,
    gadget_id: UUID,
    quantity: Annotated[int, Range(min=1, max=100)],
    note: Annotated[str, MaxLength(5)] = "",
) -> Gadget:
    """Add ``quantity`` to a gadget's stock."""
    gadget = await uow.gadgets.get(GadgetId(gadget_id))
    gadget.stock = (gadget.stock or 0) + quantity
    gadget.version += 1
    if note == "boom":
        raise Boom
    return gadget


@use_case(name="make_gadget")
async def make(
    uow: FakeGadgetsUnitOfWork, name: Annotated[str, MaxLength(30)]
) -> Gadget:
    """Make a gadget called ``name``."""
    gadget = make_gadget(name)
    await uow.gadgets.add(gadget)
    return gadget


@use_case
async def retire(uow: FakeGadgetsUnitOfWork, gadget_id: UUID) -> None:
    """Take a gadget off sale."""
    gadget = await uow.gadgets.get(GadgetId(gadget_id))
    gadget.active = False
    gadget.version += 1


@pytest.fixture
def store() -> InMemoryGadgets:
    """One gadget, a."""
    return InMemoryGadgets([make_gadget("a")])


def test_the_command_is_made_from_the_parameters_after_the_unit_of_work() -> None:
    """Its name, fields, defaults, limits, and docstring come from the function."""
    command = restock.command

    assert issubclass(command, Command)
    assert (command.__name__, command.__qualname__) == ("Restock", "Restock")
    assert command.__module__ == __name__
    assert command.__doc__ == "Add ``quantity`` to a gadget's stock."
    assert restock.fields == ("gadget_id", "quantity", "note")
    assert {each.name: each.default for each in dataclasses.fields(command)}[
        "note"
    ] == ""
    assert command.__annotations__["quantity"] == Annotated[int, Range(min=1, max=100)]
    assert (restock.name, restock.__name__, restock.__doc__) == (
        "restock",
        "restock",
        "Add ``quantity`` to a gadget's stock.",
    )
    assert (restock.__qualname__, restock.__module__) == ("restock", __name__)
    assert repr(restock) == "<use case restock (Restock)>"
    with pytest.raises(dataclasses.FrozenInstanceError):
        restock.command(gadget_id=uuid4(), quantity=1).quantity = 2


def test_a_name_names_the_command_and_the_use_case() -> None:
    """``@use_case(name=...)`` overrides the function's name."""
    assert (make.name, make.command.__name__) == ("make_gadget", "MakeGadget")
    assert isinstance(make, UseCase)
    assert (make.returns_nothing, retire.returns_nothing) == (False, True)


def test_a_function_without_a_docstring_gets_one() -> None:
    """The command still documents what it runs."""

    async def nameless(uow: UnitOfWork) -> None:
        pass

    assert use_case(nameless).command.__doc__ == "Run nameless."


@pytest.mark.anyio
async def test_calling_it_runs_the_function_in_the_unit_of_work_and_commits(
    store: InMemoryGadgets,
) -> None:
    """Opened, called with the fields, committed once; the result is returned."""
    uow = FakeGadgetsUnitOfWork(store)
    (gadget_id,) = store.items

    gadget = await restock(uow, gadget_id=gadget_id, quantity=3)
    again = await restock.run(restock.command(gadget_id=gadget_id, quantity=2), uow)

    assert gadget is again
    assert (gadget.stock, uow.commits) == (5, 2)


@pytest.mark.anyio
async def test_a_failure_rolls_back_and_commits_nothing(store: InMemoryGadgets) -> None:
    """The error reaches the caller; the repository is as it was."""
    uow = FakeGadgetsUnitOfWork(store)
    (gadget_id,) = store.items

    with pytest.raises(Boom):
        await restock(uow, gadget_id=gadget_id, quantity=3, note="boom")

    assert (store.items[gadget_id].stock, uow.commits) == (None, 0)


@pytest.mark.anyio
async def test_it_runs_only_its_own_command(store: InMemoryGadgets) -> None:
    """Another command is a TypeError, before the unit of work opens."""
    uow = FakeGadgetsUnitOfWork(store)

    with pytest.raises(TypeError, match="restock runs Restock, not MakeGadget"):
        await restock.run(make.command(name="x"), uow)

    assert uow.commits == 0


@pytest.mark.anyio
async def test_its_fields_are_checked_as_a_command_s_are(
    store: InMemoryGadgets,
) -> None:
    """A missing or unknown field is a TypeError, as for any dataclass."""
    uow = FakeGadgetsUnitOfWork(store)

    with pytest.raises(TypeError):
        await restock(uow, quantity=1)
    with pytest.raises(TypeError):
        await restock(uow, gadget_id=uuid4(), quantity=1, colour="red")


async def _with_positional_only(uow: UnitOfWork, name: str, /) -> None:
    pass


async def _with_args(uow: UnitOfWork, *names: str) -> None:
    pass


async def _with_kwargs(uow: UnitOfWork, **names: str) -> None:
    pass


async def _without_a_hint(uow: UnitOfWork, name) -> None:  # type: ignore[no-untyped-def]  # noqa: ANN001
    pass


async def _without_parameters() -> None:
    pass


def _synchronous(uow: UnitOfWork) -> None:
    pass


@pytest.mark.parametrize(
    ("function", "message"),
    [
        (_with_positional_only, "name must be an ordinary parameter"),
        (_with_args, "names must be an ordinary parameter"),
        (_with_kwargs, "names must be an ordinary parameter"),
        (_without_a_hint, "give name a type hint"),
        (_without_parameters, "takes no unit of work"),
        (_synchronous, "needs an async function"),
    ],
)
def test_functions_that_cannot_be_a_command_are_refused(
    function: Callable[..., Any], message: str
) -> None:
    """Each field of a command is given by name and typed."""
    with pytest.raises(TypeError, match=message):
        use_case(function)


def test_routes_take_their_body_and_id_from_the_use_case(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """``ResourceRouter.use_case``: the id from the path, the rest from the body."""
    use_auth()
    resource = gadgets(store)
    resource.use_case(restock, path="/restock")
    resource.use_case(make)
    resource.use_case(retire, path="/retire")
    client = client_of(resource)
    (gadget_id,) = store.items
    item = f"{LOCATION}{gadget_id}"

    restocked = client.post(
        f"{item}/restock", json={"quantity": 4}, headers=as_("changer")
    )
    created = client.post("/gadgets", json={"name": "new"}, headers=as_("adder"))
    retired = client.post(f"{item}/retire", headers=as_("changer"))
    too_many = client.post(
        f"{item}/restock", json={"quantity": 101}, headers=as_("changer")
    )
    too_long = client.post(
        f"{item}/restock",
        json={"quantity": 1, "note": "longer"},
        headers=as_("changer"),
    )
    its_id = client.post(
        f"{item}/restock",
        json={"quantity": 1, "gadget_id": str(uuid4())},
        headers=as_("changer"),
    )
    refused = client.post(
        f"{item}/restock", json={"quantity": 1}, headers=as_("viewer")
    )

    assert (restocked.status_code, restocked.json()["stock"]) == (200, 4)
    assert (created.status_code, created.json()["name"]) == (201, "new")
    assert created.headers["location"] == f"/gadgets/{created.json()['id']}"
    assert (retired.status_code, retired.content) == (204, b"")
    assert store.items[gadget_id].active is False
    assert [too_many.status_code, too_long.status_code, its_id.status_code] == [
        422,
        422,
        422,
    ]
    assert refused.status_code == 403
    assert store.items[gadget_id].stock == 4
    operations = {route.name for route in resource.routes if hasattr(route, "name")}
    assert {"restock", "make_gadget", "retire"} <= operations


def test_routes_leave_what_is_provided_or_given_out_of_the_body(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """``provide`` and the parent's id fill their fields; an explicit body wins."""
    use_auth()
    parent = gadgets(store)
    (gadget_id,) = store.items
    reviews = InMemoryRepository[Review, UUID]([])

    class Reviews(UnitOfWork):
        def __init__(self) -> None:
            super().__init__(events=EventBus())
            self.reviews = reviews

        async def _commit(self) -> None:
            """Nothing to write."""

        async def _rollback(self) -> None:
            """Nothing to undo."""

    @use_case
    async def write_review(
        uow: Reviews, gadget: UUID, text: str, author: str
    ) -> Review:
        """Review a gadget."""
        review = Review(id=uuid4(), gadget=GadgetId(gadget), text=f"{author}: {text}")
        await uow.reviews.add(review)
        return review

    nested = ResourceRouter(
        "/reviews",
        model=Review,
        unit_of_work=Reviews,
        repository=lambda uow: uow.reviews,
        parent=parent,
        parent_field="gadget",
        rule=IsAuthenticated(),
    )
    nested.use_case(write_review, provide={"author": lambda: "ann"})
    client = client_of(parent, nested)

    created = client.post(
        f"/gadgets/{gadget_id}/reviews", json={"text": "fine"}, headers=as_("viewer")
    )
    smuggled = client.post(
        f"/gadgets/{gadget_id}/reviews",
        json={"text": "x", "author": "eve"},
        headers=as_("viewer"),
    )

    assert created.status_code == 201
    assert created.json()["text"] == "ann: fine"
    assert smuggled.status_code == 422
    assert [review.gadget for review in reviews.items.values()] == [gadget_id]


class QuantityIn(BaseModel):
    """Only a quantity, without the use case's limits."""

    quantity: int


def test_an_explicit_body_replaces_the_derived_one(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """``body=`` is used as given, and the command still keeps its own limits.

    One validation path: a body without the limits lets 500 through, and the
    command refuses it, 422 naming the field.
    """
    use_auth()
    resource = gadgets(store)
    resource.use_case(restock, body=QuantityIn, path="/add")
    client = client_of(resource)
    (gadget_id,) = store.items

    added = client.post(
        f"{LOCATION}{gadget_id}/add", json={"quantity": 50}, headers=as_("changer")
    )
    refused = client.post(
        f"{LOCATION}{gadget_id}/add", json={"quantity": 500}, headers=as_("changer")
    )

    assert (added.status_code, added.json()["stock"]) == (200, 50)
    assert refused.status_code == 422
    assert refused.json()["code"] == "invalid_field"
    assert [error["loc"] for error in refused.json()["errors"]] == [
        ["body", "quantity"]
    ]
    assert store.items[gadget_id].stock == 50


SECRET = "hunter2-long"


@use_case
async def lock_gadget(
    uow: FakeGadgetsUnitOfWork,
    gadget_id: UUID,
    code: Annotated[str, MinLength(8), Field(repr=False)],
    hint: Annotated[str, Field(repr=False)] | None = None,
) -> Gadget:
    """Lock a gadget behind a code."""
    gadget = await uow.gadgets.get(GadgetId(gadget_id))
    gadget.version += 1
    return gadget


def test_a_parameter_hinted_repr_false_stays_out_of_the_command_s_repr(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``Field(repr=False)`` makes a field with ``repr=False`` (user report D2).

    The value never shows in ``repr`` or ``str``, so neither in a log line
    of the command, plain or JSON; the command still carries and compares it.
    """
    gadget_id = UUID(int=7)
    command = lock_gadget.command(gadget_id=gadget_id, code=SECRET, hint="pet's name")
    logger = logging.getLogger("domainkit_tests.use_cases")
    with caplog.at_level(logging.INFO, logger=logger.name):
        logger.info("ran %s", command)
        logger.info("ran %r", command)
    lines = [JsonFormatter().format(record) for record in caplog.records]

    assert repr(command) == f"LockGadget(gadget_id={gadget_id!r})"
    assert str(command) == f"{command}" == repr(command)
    assert {each.name: each.repr for each in dataclasses.fields(command)} == {
        "gadget_id": True,
        "code": False,
        "hint": False,
    }
    assert (command.code, command.hint) == (SECRET, "pet's name")
    assert command != lock_gadget.command(gadget_id=gadget_id, code="other-code")
    assert all(SECRET not in each and "pet" not in each for each in lines)
    assert SECRET not in caplog.text
    assert lock_gadget.command(gadget_id=gadget_id, code=SECRET).hint is None


def test_every_other_command_shows_its_fields_as_before() -> None:
    """Only a field hinted ``Field(repr=False)`` is left out (user report D2)."""
    gadget_id = UUID(int=7)

    assert repr(restock.command(gadget_id=gadget_id, quantity=2)) == (
        f"Restock(gadget_id={gadget_id!r}, quantity=2, note='')"
    )
    assert [each.repr for each in dataclasses.fields(restock.command)] == [True] * 3


@pytest.mark.anyio
async def test_no_refusal_of_a_hidden_parameter_quotes_its_value(
    store: InMemoryGadgets,
) -> None:
    """A value too short, a field set again, a wrong command: none says it (D2)."""
    uow = FakeGadgetsUnitOfWork(store)
    (gadget_id,) = store.items
    command = lock_gadget.command(gadget_id=gadget_id, code=SECRET)
    errors: list[BaseException] = []

    for attempt in (
        lambda: lock_gadget.command(gadget_id=gadget_id, code="hunter2"),
        lambda: setattr(command, "code", "hunter2-other"),
        lambda: lock_gadget.command(code=SECRET),
    ):
        with pytest.raises((InvalidField, AttributeError, TypeError)) as caught:
            attempt()
        errors.append(caught.value)
    with pytest.raises(TypeError) as wrong:
        await restock.run(command, uow)
    errors.append(wrong.value)

    assert isinstance(errors[0], InvalidField)
    assert errors[0].field == "code"
    assert all("hunter2" not in f"{each!s} {each!r}" for each in errors)


def test_a_hidden_parameter_stays_out_of_the_request_too(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """The derived body hides it, and no refusal quotes it (user report D2).

    The body's ``repr`` leaves it out and its validation errors leave the
    input out; the route's 422 never echoes it.
    """
    use_auth()
    resource = gadgets(store)
    resource.use_case(lock_gadget, path="/lock")
    client = client_of(resource)
    (gadget_id,) = store.items
    body = schema_for(lock_gadget.command, write=True, include=["code", "hint"])

    locked = client.post(
        f"{LOCATION}{gadget_id}/lock", json={"code": SECRET}, headers=as_("changer")
    )
    short = client.post(
        f"{LOCATION}{gadget_id}/lock", json={"code": "hunter2"}, headers=as_("changer")
    )
    with pytest.raises(ValidationError) as invalid:
        body.model_validate({"code": "hunter2", "hint": 5})

    assert locked.status_code == 200
    assert short.status_code == 422
    assert "hunter2" not in short.text
    assert repr(body.model_validate({"code": SECRET})) == "LockGadgetIn()"
    assert "hunter2" not in str(invalid.value)


class Place(ValueObject):
    """Where a gadget is kept: a value object of several fields."""

    shelf: Annotated[str, MaxLength(4)]
    row: int


class Shelf(ValueObject):
    """A value object of one field."""

    value: str


@dataclasses.dataclass(frozen=True)
class Point:
    """A plain dataclass."""

    x: int
    y: int


class Spot(ValueObject):
    """A value object that holds a plain dataclass, and a float."""

    at: Point
    weight: float


class Span(ValueObject):
    """A value object whose own check raises ``ValueError``."""

    low: int
    high: int

    def __post_init__(self) -> None:
        """Refuse a span that ends before it starts."""
        if self.high < self.low:
            raise ValueError("A span ends after it starts.")


def test_value_objects_reach_the_function_built(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """A derived body's value objects reach the function built (user report U5).

    Several fields, alone or in a tuple, from their parts, checked as a
    value object field's are; one field, around its value; a dataclass the
    body validates, as validated. A value object's own refusal is 422.
    """
    use_auth()
    seen: list[dict[str, object]] = []

    @use_case
    async def keep(
        uow: FakeGadgetsUnitOfWork,
        gadget_id: UUID,
        places: tuple[Place, ...],
        home: Place | None = None,
        label: Shelf | None = None,
        shelves: tuple[Shelf, ...] = (),
        points: tuple[Point, ...] = (),
        spot: Spot | None = None,
        span: Span | None = None,
    ) -> None:
        """Say where a gadget is kept."""
        seen.append(
            {
                "places": places,
                "home": home,
                "label": label,
                "shelves": shelves,
                "points": points,
                "spot": spot,
            }
        )

    resource = gadgets(store)
    resource.use_case(keep, path="/keep")
    resource.use_case(
        keep,
        path="/keep",
        method="PATCH",
        name="keep_some",
        body=schema_for(keep.command, partial=True, include=["places", "label"]),
    )
    resource.batch_route(
        keep.command,
        service=keep.run,
        body=schema_for(keep.command, write=True, include=["places"]),
        returns=None,
    )
    client = client_of(resource)
    (gadget_id,) = store.items
    item = f"{LOCATION}{gadget_id}"

    def post(**body: object) -> Any:  # noqa: ANN401 - a response.
        return client.post(f"{item}/keep", json=body, headers=as_("changer"))

    kept = post(
        places=[{"shelf": "A1", "row": 2}, {"shelf": "B", "row": 0}],
        home={"shelf": "H", "row": 1},
        label="l",
        shelves=[{"value": "s"}],
        points=[{"x": 1, "y": 2}],
        spot={"at": {"x": 3, "y": 4}, "weight": 0.5},
    )
    none = post(places=[])
    patched = client.patch(
        f"{item}/keep",
        json={"places": [{"shelf": "P", "row": 3}]},
        headers=as_("changer"),
    )
    batched = client.post(
        "/gadgets/batch",
        json={
            "items": [
                {"gadget_id": str(gadget_id), "places": [{"shelf": "Q", "row": 4}]}
            ]
        },
        headers=as_("changer"),
    )
    too_long = post(places=[{"shelf": "LONGER", "row": 1}])
    endless = client.post(
        f"{item}/keep",
        content='{"places": [], "spot": {"at": {"x": 1, "y": 1}, "weight": NaN}}',
        headers={**as_("changer"), "Content-Type": "application/json"},
    )
    backwards = post(places=[], span={"low": 2, "high": 1})
    schemas = client.get("/openapi.json").json()["components"]["schemas"]

    assert [each.status_code for each in (kept, none, patched)] == [204, 204, 204]
    assert batched.json()["results"][0]["status"] == 204
    empty = {"home": None, "label": None, "shelves": (), "points": (), "spot": None}
    assert seen == [
        {
            "places": (Place("A1", 2), Place("B", 0)),
            "home": Place("H", 1),
            "label": Shelf("l"),
            "shelves": (Shelf("s"),),
            "points": (Point(1, 2),),
            "spot": Spot(Point(3, 4), 0.5),
        },
        {"places": (), **empty},
        {"places": (Place("P", 3),), **empty},
        {"places": (Place("Q", 4),), **empty},
    ]
    assert all(type(each) is Place for each in seen[0]["places"])  # type: ignore[attr-defined]
    assert (too_long.status_code, too_long.json()["errors"][0]["loc"]) == (
        422,
        ["body", "places", 0, "shelf"],
    )
    assert (endless.status_code, endless.json()["errors"][0]["loc"]) == (
        422,
        ["body", "spot", "weight"],
    )
    assert (backwards.status_code, backwards.json()["errors"]) == (
        422,
        [
            {
                "loc": ["body", "span"],
                "msg": "Value error, A span ends after it starts.",
                "type": "value_error",
            }
        ],
    )
    assert schemas["KeepIn"]["properties"]["places"]["items"] == {
        "$ref": "#/components/schemas/PlaceIn"
    }
    assert schemas["PlaceIn"]["properties"]["shelf"]["maxLength"] == 4
    command: type = keep.command
    assert to_domain(fields_of(command)["places"], [{"shelf": "A", "row": 1}]) == (
        Place("A", 1),
    )


class Parts(BaseModel):
    """A part, as a request gives it."""

    sku: str


class Line(ValueObject):
    """A value object a hand-written body gives in a shape of its own."""

    account: str
    amount: Money


class Assembled(Command):
    """Fields a hand-written body gives."""

    gadget_id: UUID
    main: dict[str, str]
    parts: tuple[dict[str, str], ...]
    named: dict[str, dict[str, str]]
    lines: tuple[Line, ...]
    place: Place
    places: tuple[Place, ...]
    note: str


class AmountIn(BaseModel):
    """An amount, as a hand-written body gives it."""

    amount: Decimal
    currency: str


class LineIn(BaseModel):
    """A line, in a shape of the body's own."""

    account: str
    amount: AmountIn


class AssemblyIn(BaseModel):
    """A hand-written body: nested models, and value objects as their class."""

    main: Parts
    parts: tuple[Parts, ...]
    named: dict[str, Parts]
    lines: list[LineIn]
    place: dict[str, Any]
    places: tuple[Place, ...]
    note: str

    @field_serializer("note")
    def lower(self, value: str) -> str:
        """A serializer ``model_dump`` runs."""
        return value.lower()


class Later(Command):
    """A command whose hints do not resolve when its route is declared."""

    gadget_id: UUID
    places: tuple[Place, ...]
    later: "Undefined"  # type: ignore[name-defined]  # noqa: F821 - never resolves.


class LaterIn(BaseModel):
    """A body for ``Later``."""

    places: tuple[Place, ...]
    later: int


def test_hand_written_bodies_give_the_command_what_model_dump_gives(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """Shapes of the body's own come as in 2.4: ``model_dump()`` (U5).

    Only a field of the value object's own class (or a tuple of it) gives
    it as validated, built again. A command whose hints do not resolve
    takes every field as ``model_dump()`` gives it.
    """
    use_auth()
    seen: list[object] = []

    async def assemble(command: object, uow: FakeGadgetsUnitOfWork) -> None:
        seen.append(command)

    resource = gadgets(store)
    resource.command_route(
        Assembled, service=assemble, body=AssemblyIn, path="/assemble", returns=None
    )
    resource.command_route(
        Later, service=assemble, body=LaterIn, path="/later", returns=None
    )
    client = client_of(resource)
    (gadget_id,) = store.items
    item = f"{LOCATION}{gadget_id}"
    place = {"shelf": "A", "row": 1}

    assembled = client.post(
        f"{item}/assemble",
        json={
            "main": {"sku": "m"},
            "parts": [{"sku": "p"}],
            "named": {"n": {"sku": "q"}},
            "lines": [
                {"account": "a", "amount": {"amount": "1.00", "currency": "EUR"}}
            ],
            "place": {**place, "note": "kept"},
            "places": [place],
            "note": "LOUD",
        },
        headers=as_("changer"),
    )
    later = client.post(
        f"{item}/later", json={"places": [place], "later": 7}, headers=as_("changer")
    )

    assert (assembled.status_code, later.status_code) == (204, 204)
    assert seen == [
        Assembled(
            gadget_id=gadget_id,
            main={"sku": "m"},
            parts=({"sku": "p"},),
            named={"n": {"sku": "q"}},
            lines=[  # type: ignore[arg-type]
                {
                    "account": "a",
                    "amount": {"amount": Decimal("1.00"), "currency": "EUR"},
                }
            ],
            place={**place, "note": "kept"},  # type: ignore[arg-type]
            places=(Place("A", 1),),
            note="loud",
        ),
        Later(gadget_id=gadget_id, places=(place,), later=7),  # type: ignore[arg-type]
    ]


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


def test_a_class_the_body_validates_reaches_only_a_command_that_declares_it(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """A dataclass or value object comes as itself where the command declares it.

    Where the command declares ``dict``, ``Any``, ``Mapping``, or anything
    else, it comes as ``model_dump()`` gives it, as in 2.4 (U5).
    """
    use_auth()
    seen: list[object] = []

    async def keep(command: object, uow: FakeGadgetsUnitOfWork) -> None:
        seen.append(command)

    resource = gadgets(store)
    resource.command_route(
        Loose, service=keep, body=ClassesIn, path="/loose", returns=None
    )
    resource.command_route(
        Typed, service=keep, body=TupleIn, path="/typed", returns=None
    )
    client = client_of(resource)
    (gadget_id,) = store.items
    place = {"shelf": "A", "row": 1}
    body = {"point": {"x": 1, "y": 2}, "points": [{"x": 3, "y": 4}], "place": place}

    loose = client.post(
        f"{LOCATION}{gadget_id}/loose",
        json={**body, "places": place},
        headers=as_("changer"),
    )
    typed = client.post(
        f"{LOCATION}{gadget_id}/typed",
        json={**body, "places": [place]},
        headers=as_("changer"),
    )

    assert (loose.status_code, typed.status_code) == (204, 204)
    assert seen == [
        Loose(
            gadget_id=gadget_id,
            point={"x": 1, "y": 2},
            points=({"x": 3, "y": 4},),
            place=place,
            places=place,
        ),
        Typed(
            gadget_id=gadget_id,
            point=Point(1, 2),
            points=(Point(3, 4),),
            place=Place("A", 1),
            places=(Place("A", 1),),
        ),
    ]
    assert [type(each) for each in vars(seen[0]).values()][1:] == [
        dict,
        tuple,
        dict,
        dict,
    ]
