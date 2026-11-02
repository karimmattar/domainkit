"""``@use_case``: one async function is the command, the service, and the route."""

import dataclasses
from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel

from domainkit.ddd import (
    Command,
    EventBus,
    InMemoryRepository,
    MaxLength,
    Range,
    UnitOfWork,
    UseCase,
    use_case,
)
from domainkit.http.access import IsAuthenticated
from domainkit.resources import ResourceRouter
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
