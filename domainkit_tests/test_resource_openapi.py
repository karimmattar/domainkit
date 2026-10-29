"""What OpenAPI says of a resource with every option, as reviewed (a snapshot)."""

import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from domainkit.ddd import EventBus, InMemoryRepository, UnitOfWork
from domainkit.ddd.query import F
from domainkit.http import Pagination
from domainkit.http.access import IsAuthenticated, Public
from domainkit.resources import ALL_FIELDS, FieldPolicy, ResourceRouter
from domainkit_tests.gadgets import (
    FakeGadgetsUnitOfWork,
    Gadget,
    InMemoryGadgets,
    make_gadget,
)
from domainkit_tests.test_resources import (
    Dearest,
    DropGadget,
    EditReview,
    GadgetFilters,
    GadgetsLabelled,
    MakeGadget,
    MakeIn,
    RenameGadget,
    RenameIn,
    Review,
    ReviewIn,
    Summary,
    WriteReview,
    client_of,
    dearest,
    drop,
    gadgets,
    labelled,
    make,
    rename,
)

SNAPSHOT = Path(__file__).parent / "snapshots" / "openapi" / "resources.json"


class Reviews(UnitOfWork):
    """Reviews in memory."""

    def __init__(self) -> None:
        """No reviews."""
        super().__init__(events=EventBus())
        self.reviews = InMemoryRepository[Review, UUID]()

    async def _commit(self) -> None:
        """Nothing to write."""

    async def _rollback(self) -> None:
        """Nothing to undo."""


async def write(command: WriteReview, uow: Reviews) -> Review:
    """Store a review."""
    review = Review(id=uuid4(), gadget=command.gadget, text=command.text)  # type: ignore[arg-type]
    async with uow:
        await uow.reviews.add(review)
        await uow.commit()
    return review


async def edit(command: EditReview, uow: Reviews) -> Review:
    """Edit a review."""
    async with uow:
        review = await uow.reviews.get(command.review_id)
        review.text = command.text
        await uow.commit()
    return review


async def everything(request: Any) -> Any:  # noqa: ANN401 - a connection, criteria.
    """No narrowing at all, but a scope all the same."""
    return F("name") != ""


def test_openapi_describes_every_option_as_reviewed() -> None:
    """Paths, parameters, bodies, answers, and errors; UPDATE_SNAPSHOTS=1 rewrites."""
    store = InMemoryGadgets([make_gadget("a")])
    resource = gadgets(
        store,
        filters=GadgetFilters,
        pagination=Pagination(offset=True, count=True),
        policy=FieldPolicy(read_only=["active"], visible={"stock": "shop.view_cost"}),
        crud=True,
        writable=ALL_FIELDS,
        links=True,
        require_if_match=True,
        scope=everything,
        exports=("csv", "ndjson"),
        list_fields=["name", "price", "stock"],
    )
    resource.command_route(MakeGadget, service=make, body=MakeIn, path="/made")
    resource.command_route(RenameGadget, service=rename, body=RenameIn, path="/rename")
    resource.command_route(DropGadget, service=drop, method="DELETE", path="/drop")
    resource.query_route(GadgetsLabelled, handler=labelled, path="/labelled")
    resource.batch_route(RenameGadget, service=rename, body=RenameIn)
    resource.command_route(
        RenameGadget, service=rename, body=RenameIn, path="/brief", returns=Summary
    )
    resource.query_route(Dearest, handler=dearest, path="/dearest")
    open_to_all = ResourceRouter(
        "/open",
        model=Gadget,
        unit_of_work=lambda: FakeGadgetsUnitOfWork(store),
        repository=lambda uow: uow.gadgets,
        dependencies=[Public],
        exports=("csv",),
        name_prefix="open_",
    )
    open_to_all.command_route(
        RenameGadget, service=rename, body=RenameIn, path="/rename"
    )
    nested = ResourceRouter(
        "/reviews",
        model=Review,
        unit_of_work=Reviews,
        repository=lambda uow: uow.reviews,
        parent=resource,
        parent_field="gadget",
        rule=IsAuthenticated(),
        links=True,
    )
    nested.command_route(WriteReview, service=write, body=ReviewIn)
    nested.command_route(EditReview, service=edit, body=ReviewIn, path="/edit")

    document = client_of(resource, nested, open_to_all).get("/openapi.json").json()
    text = json.dumps(document, indent=1, sort_keys=True) + "\n"
    if os.environ.get("UPDATE_SNAPSHOTS"):
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(text, encoding="utf-8")

    assert text == SNAPSHOT.read_text(encoding="utf-8")


def test_a_batch_route_takes_the_summary_it_is_given() -> None:
    """``summary=`` names the batch operation in place of its own."""
    resource = gadgets(InMemoryGadgets([make_gadget("a")]))
    resource.batch_route(
        RenameGadget, service=rename, body=RenameIn, summary="Rename many gadgets"
    )

    document = client_of(resource).get("/openapi.json").json()

    assert document["paths"]["/gadgets/batch"]["post"]["summary"] == (
        "Rename many gadgets"
    )
