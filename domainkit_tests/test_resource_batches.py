"""Batches: one command for many items, each answered, atomically or not."""

from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy.exc import TimeoutError as PoolTimeout

from domainkit.ddd import Command, NotFound
from domainkit.ddd.query import F
from domainkit.http.errors import APIError
from domainkit.resources import PatchModel
from domainkit.resources.batches import Enlisted, failure, not_applied
from domainkit_tests.conftest import UseAuth
from domainkit_tests.gadgets import FakeGadgetsUnitOfWork, Gadget, InMemoryGadgets
from domainkit_tests.test_resources import (
    DropGadget,
    GadgetChanges,
    MakeGadget,
    MakeIn,
    RenameGadget,
    RenameIn,
    Summary,
    as_,
    client_of,
    drop,
    gadgets,
    make,
    rename,
    summarize,
)

BATCH = "/gadgets/batch"
LONG = "x" * 25
"""A name the schema takes but whose label the domain refuses (over 20)."""


@pytest.fixture
def store() -> InMemoryGadgets:
    """Gadgets a, b, c."""
    from domainkit_tests.gadgets import make_gadget

    return InMemoryGadgets([make_gadget(name) for name in ("a", "b", "c")])


def ids(store: InMemoryGadgets) -> dict[str, UUID]:
    """Gadget ids by name."""
    return {gadget.name: gadget.id for gadget in store.items.values()}


def test_items_run_one_by_one_and_answer_each(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """Without ``atomic``, failures leave the others done; 207 with each result."""
    use_auth()
    resource = gadgets(store)
    resource.batch_route(RenameGadget, service=rename, body=RenameIn)
    client = client_of(resource)
    known = ids(store)
    tag = client.get(f"/gadgets/{known['c']}", headers=as_("viewer")).headers["etag"]

    answer = client.post(
        BATCH,
        json={
            "items": [
                {"gadget_id": str(known["a"]), "name": "x"},
                {"gadget_id": str(uuid4()), "name": "y"},
                {"gadget_id": str(known["b"]), "name": "z", "if_match": '"7"'},
                {"gadget_id": str(known["c"]), "name": "w", "if_match": tag},
            ]
        },
        headers=as_("changer"),
    )

    body = answer.json()
    assert answer.status_code == 207
    assert body["failed"] == 2
    assert [result["status"] for result in body["results"]] == [200, 404, 412, 200]
    assert body["results"][0]["item"]["name"] == "x"
    assert body["results"][0]["etag"].startswith('W/"1-')
    assert body["results"][1]["error"]["code"] == "not_found"
    assert body["results"][2]["error"]["code"] == "precondition_failed"
    assert sorted(gadget.name for gadget in store.items.values()) == ["b", "w", "x"]
    assert (
        client.post(BATCH, json={"items": []}, headers=as_("changer")).status_code
        == 422
    )
    assert (
        client.post(BATCH, json={"items": [{}]}, headers=as_("viewer")).status_code
        == 403
    )


def test_atomic_batches_apply_all_or_nothing(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """One failure undoes the batch: it answers, the others are 424."""
    use_auth()
    resource = gadgets(store)
    resource.batch_route(MakeGadget, service=make, body=MakeIn, max_items=3)
    client = client_of(resource)

    def batch(*names: str, atomic: bool = True) -> Any:  # noqa: ANN401 - JSON.
        items = [{"name": name, "price": "1.00"} for name in names]
        answer = client.post(
            BATCH, json={"items": items, "atomic": atomic}, headers=as_("adder")
        )
        return answer.status_code, answer.json()

    undone = batch("d", LONG, "e")
    done = batch("f", "g")
    too_many = batch("h", "i", "j", "k")

    assert undone[0] == 207
    assert [result["status"] for result in undone[1]["results"]] == [424, 422, 424]
    assert undone[1]["results"][0]["error"]["code"] == "not_applied"
    assert undone[1]["failed"] == 1
    assert [result["status"] for result in done[1]["results"]] == [201, 201]
    assert done[1]["results"][0]["location"] == (
        f"/gadgets/{done[1]['results'][0]['item']['id']}"
    )
    assert sorted(gadget.name for gadget in store.items.values()) == [
        "a",
        "b",
        "c",
        "f",
        "g",
    ]
    assert too_many[0] == 422


def test_atomic_batches_keep_their_events_for_one_commit(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """Each service's own commit waits for the batch's, which publishes them all."""
    use_auth()
    published: list[Any] = []
    resource = gadgets(store, published)
    resource.batch_route(RenameGadget, service=rename, body=RenameIn)
    client = client_of(resource)
    known = ids(store)

    answer = client.post(
        BATCH,
        json={
            "atomic": True,
            "items": [
                {"gadget_id": str(known["a"]), "name": "x"},
                {"gadget_id": str(known["b"]), "name": "y"},
            ],
        },
        headers=as_("changer"),
    )

    assert [result["status"] for result in answer.json()["results"]] == [200, 200]
    assert len(published) == 2


def test_batches_answer_as_their_commands_do(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """``returns=None`` is 204 per item, another schema its own; scope applies."""
    use_auth()

    async def without_b(request: Any) -> Any:  # noqa: ANN401 - a connection, criteria.
        return F("name") != "b"

    resource = gadgets(store, scope=without_b)
    resource.batch_route(DropGadget, service=drop, path="/drops", action="delete")
    resource.batch_route(
        RenameGadget, service=summarize, body=RenameIn, returns=Summary, path="/names"
    )
    client = client_of(resource)
    known = ids(store)

    drops = client.post(
        "/gadgets/drops",
        json={
            "items": [{"gadget_id": str(known["a"])}, {"gadget_id": str(known["b"])}]
        },
        headers=as_("deleter"),
    )
    names = client.post(
        "/gadgets/names",
        json={"items": [{"gadget_id": str(known["c"]), "name": "n"}]},
        headers=as_("changer"),
    )

    assert [result["status"] for result in drops.json()["results"]] == [204, 404]
    assert "item" not in drops.json()["results"][0]
    assert names.json()["results"] == [{"status": 200, "item": {"name": "n"}}]


def test_batch_routes_come_before_item_routes(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """``POST /gadgets/batch`` is not ``POST /gadgets/{gadget_id}``."""
    use_auth()
    resource = gadgets(store)
    resource.command_route(RenameGadget, service=rename, body=RenameIn)
    resource.batch_route(RenameGadget, service=rename, body=RenameIn)
    client = client_of(resource)
    known = ids(store)

    answer = client.post(
        BATCH,
        json={"items": [{"gadget_id": str(known["a"]), "name": "x"}]},
        headers=as_("changer"),
    )

    assert answer.status_code == 207


class Broken(Command):
    """A command whose service has a bug."""

    gadget_id: UUID


async def broken(command: Broken, uow: FakeGadgetsUnitOfWork) -> Gadget:
    """Fail as a bug does."""
    raise ZeroDivisionError


async def empty(command: MakeGadget, uow: FakeGadgetsUnitOfWork) -> None:
    """Forget to answer."""


def test_bugs_are_not_results(store: InMemoryGadgets, use_auth: UseAuth) -> None:
    """An unexpected error, or a missing answer, fails the request."""
    use_auth()
    resource = gadgets(store)
    resource.batch_route(Broken, service=broken)
    resource.batch_route(MakeGadget, service=empty, body=MakeIn, path="/empty")
    client = client_of(resource)

    with pytest.raises(ZeroDivisionError):
        client.post(
            BATCH,
            json={"items": [{"gadget_id": str(uuid4())}]},
            headers=as_("changer"),
        )
    with pytest.raises(RuntimeError, match="returned nothing"):
        client.post(
            "/gadgets/empty",
            json={"items": [{"name": "n", "price": "1"}]},
            headers=as_("adder"),
        )


class PricedIn(BaseModel):
    """A body with the id in it."""

    gadget_id: UUID
    name: str


def test_batch_declarations_are_checked(store: InMemoryGadgets) -> None:
    """Paths, bounds, partial bodies, ids in bodies, and the command's fields."""
    resource = gadgets(store)

    for options, message in (
        ({"path": "batch"}, "starts with /"),
        ({"max_items": 0}, "1 item or more"),
        ({"body": GadgetChanges}, "not partial changes"),
        ({"body": PricedIn}, "leave it, and if_match, out of the body"),
    ):
        with pytest.raises(TypeError, match=message):
            resource.batch_route(RenameGadget, service=rename, **options)
    with pytest.raises(TypeError, match="has no field price"):
        resource.batch_route(RenameGadget, service=rename, body=MakeIn)
    with pytest.raises(TypeError, match="Nothing gives RenameGadget's name"):
        resource.batch_route(RenameGadget, service=rename)
    assert issubclass(GadgetChanges, PatchModel)


def test_refusals_become_results_and_bugs_do_not() -> None:
    """Domain errors and HTTP errors map to results; other errors do not."""
    missing = failure(NotFound("Gone."))
    refused = failure(APIError(409, "Taken.", code="taken"))
    plain = failure(HTTPException(418, "Teapot."))

    assert missing is not None
    assert missing.error is not None
    assert (missing.status, missing.error.code) == (404, "not_found")
    assert refused is not None
    assert refused.error is not None
    assert (refused.status, refused.error.code) == (409, "taken")
    assert plain is not None
    assert plain.error is not None
    assert (plain.status, plain.error.code) == (418, "http_error")
    assert plain.error.detail == "Teapot."
    busy = failure(PoolTimeout())
    assert busy is not None
    assert busy.error is not None
    assert (busy.status, busy.error.code) == (503, "database_busy")
    assert failure(ValueError("bug")) is None
    undone = not_applied()
    assert undone.status == 424
    assert undone.error is not None
    assert undone.error.detail == (
        "Another item of the batch failed, so nothing was applied."
    )


@pytest.mark.anyio
async def test_lent_units_of_work_leave_the_batch_in_charge(
    store: InMemoryGadgets,
) -> None:
    """Enter, commit, roll back, and exit do nothing; the rest is the real one's."""
    uow = FakeGadgetsUnitOfWork(store)
    lent = Enlisted(uow)

    async with lent as entered:
        await entered.commit()
        await entered.rollback()

    assert entered is lent
    assert lent.gadgets is store
