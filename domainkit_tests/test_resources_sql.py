"""Resources over SQL: versions from the database, conditional writes, admin tags."""

import csv
import io
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from pydantic import BaseModel

from domainkit.conf import override_settings
from domainkit.contrib.auth import Public
from domainkit.db import connections
from domainkit.ddd import Command
from domainkit.ddd.query import F
from domainkit.http.errors import install_exception_handlers
from domainkit.resources import ResourceRouter, routing
from domainkit.testing import AsyncTestClient
from domainkit_tests.conftest import Api
from domainkit_tests.gadgets import (
    Gadget,
    GadgetId,
    GadgetsUnitOfWork,
    make_gadget,
    metadata,
)
from domainkit_tests.test_auth_admin import ADMIN, staff
from domainkit_tests.test_auth_http import create_user

pytestmark = pytest.mark.anyio


class Rename(Command):
    """Rename a gadget, then look it up again in the same transaction."""

    gadget_id: UUID
    name: str


class NameIn(BaseModel):
    """A new name."""

    name: str


async def rename(command: Rename, uow: GadgetsUnitOfWork) -> Gadget:
    """Rename; the query after it flushes the change, moving the version on."""
    async with uow:
        gadget = await uow.gadgets.get(GadgetId(command.gadget_id))
        gadget.name = command.name
        again = await uow.gadgets.first(F("id") == gadget.id)
        assert again is gadget
        assert gadget.version == 2
        await uow.commit()
    return gadget


@pytest.fixture
def database(database_url: str) -> Iterator[str]:
    """An empty database, selected in the settings."""
    with override_settings(DATABASES={"default": {"URL": database_url}}):
        yield database_url


@pytest.fixture
async def client(database: str) -> AsyncIterator[AsyncTestClient]:
    """The gadget resource over the database, its tables made, one gadget stored."""
    async with connections.engine().begin() as connection:
        await connection.run_sync(metadata.create_all)
    async with GadgetsUnitOfWork() as uow:
        await uow.gadgets.add(make_gadget("first"))
        await uow.commit()
    resource = ResourceRouter(
        "/gadgets",
        model=Gadget,
        unit_of_work=GadgetsUnitOfWork,
        repository=lambda uow: uow.gadgets,
        dependencies=[Public],
        crud=True,
    )
    resource.command_route(Rename, service=rename, body=NameIn, path="/rename")
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(resource)
    async with AsyncTestClient(app) as served:
        yield served
    await connections.close_all()


async def test_versions_come_from_the_database(client: AsyncTestClient) -> None:
    """Each change moves the tag's version on; a stale tag is refused."""
    listed = (await client.get("/gadgets")).json()["items"]
    item = f"/gadgets/{listed[0]['id']}"
    first = await client.get(item)

    patched = await client.patch(
        item, json={"stock": 3}, headers={"If-Match": first.headers["etag"]}
    )
    stale = await client.patch(
        item, json={"stock": 4}, headers={"If-Match": first.headers["etag"]}
    )
    again = await client.get(item, headers={"If-None-Match": patched.headers["etag"]})

    assert first.headers["etag"].startswith('W/"1-')
    assert (patched.status_code, patched.json()["version"]) == (200, 2)
    assert patched.headers["etag"].startswith('W/"2-')
    assert (stale.status_code, stale.json()["code"]) == (412, "precondition_failed")
    assert again.status_code == 304
    assert (await client.get(item)).json()["stock"] == 3


async def test_the_version_is_checked_once_as_first_loaded(
    client: AsyncTestClient,
) -> None:
    """A flush inside the use case moves the version; the first load counted."""
    listed = (await client.get("/gadgets")).json()["items"]
    item = f"/gadgets/{listed[0]['id']}"
    etag = (await client.get(item)).headers["etag"]

    renamed = await client.post(
        f"{item}/rename", json={"name": "second"}, headers={"If-Match": etag}
    )

    assert (renamed.status_code, renamed.json()["name"]) == (200, "second")
    assert renamed.json()["version"] == 2


async def test_generic_deletes_mark_soft_deletable_aggregates(
    client: AsyncTestClient,
) -> None:
    """A deleted gadget stays stored, marked, and is gone from the API."""
    listed = (await client.get("/gadgets")).json()["items"]
    item = f"/gadgets/{listed[0]['id']}"

    deleted = await client.delete(item, headers={"If-Match": '"1"'})

    assert deleted.status_code == 204
    assert (await client.get(item)).status_code == 404
    async with GadgetsUnitOfWork() as uow:
        kept = await uow.gadgets.find(
            GadgetId(UUID(listed[0]["id"])), include_deleted=True
        )
    assert kept is not None
    assert kept.is_deleted


def test_the_admin_tags_users_and_groups(api: Api) -> None:
    """Reads in the admin API carry tags and answer 304 while current."""
    client = api()
    headers = staff("admin", "auth.view_user", "auth.view_group")
    target = create_user("target")
    url = f"{ADMIN}/users/{target}"

    read = client.get(url, headers=headers)
    unchanged = client.get(
        url, headers={**headers, "If-None-Match": read.headers["etag"]}
    )

    assert read.headers["etag"].startswith('W/"1-')
    assert unchanged.status_code == 304


class Restock(Command):
    """Set a gadget's stock."""

    gadget_id: UUID
    stock: int


class StockIn(BaseModel):
    """A stock level."""

    stock: int


async def restock(command: Restock, uow: GadgetsUnitOfWork) -> Gadget:
    """Set the stock."""
    async with uow:
        gadget = await uow.gadgets.get(GadgetId(command.gadget_id))
        gadget.stock = command.stock
        await uow.commit()
    return gadget


@pytest.fixture
async def stocked(database: str) -> AsyncIterator[tuple[AsyncTestClient, list[UUID]]]:
    """Three gadgets stored, served with exports and a batch of restocks."""
    async with connections.engine().begin() as connection:
        await connection.run_sync(metadata.create_all)
    made = [make_gadget(name) for name in ("a", "b", "c")]
    async with GadgetsUnitOfWork() as uow:
        for gadget in made:
            await uow.gadgets.add(gadget)
        await uow.commit()
    resource = ResourceRouter(
        "/gadgets",
        model=Gadget,
        unit_of_work=GadgetsUnitOfWork,
        repository=lambda uow: uow.gadgets,
        dependencies=[Public],
        exports=("csv",),
    )
    resource.batch_route(Restock, service=restock, body=StockIn)
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(resource)
    async with AsyncTestClient(app) as served:
        yield served, [gadget.id for gadget in made]
    await connections.close_all()


async def stock_levels() -> dict[str, int | None]:
    """Each gadget's stock, by name, as stored."""
    async with GadgetsUnitOfWork() as uow:
        return {gadget.name: gadget.stock for gadget in await uow.gadgets.filter(None)}


async def test_atomic_batches_roll_back_in_the_database(
    stocked: tuple[AsyncTestClient, list[UUID]],
) -> None:
    """One failing item undoes the others; without atomic, they stay."""
    client, (a, b, c) = stocked

    def items(*pairs: tuple[UUID, int]) -> list[dict[str, Any]]:
        return [{"gadget_id": str(each), "stock": stock} for each, stock in pairs]

    undone = await client.post(
        "/gadgets/batch",
        json={"atomic": True, "items": items((a, 1), (uuid4(), 2), (c, 3))},
    )
    after_undone = await stock_levels()
    done = await client.post(
        "/gadgets/batch", json={"atomic": True, "items": items((a, 4), (b, 5))}
    )
    after_done = await stock_levels()
    partly = await client.post(
        "/gadgets/batch", json={"items": items((a, 6), (uuid4(), 7), (c, 8))}
    )

    assert [each["status"] for each in undone.json()["results"]] == [424, 404, 424]
    assert after_undone == {"a": None, "b": None, "c": None}
    assert [each["item"]["version"] for each in done.json()["results"]] == [2, 2]
    assert after_done == {"a": 4, "b": 5, "c": None}
    assert [each["status"] for each in partly.json()["results"]] == [200, 404, 200]
    assert await stock_levels() == {"a": 6, "b": 5, "c": 8}


async def test_exports_page_through_the_database(
    stocked: tuple[AsyncTestClient, list[UUID]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chunks continue after each other's last row, in the listing's order."""
    client, ids = stocked
    monkeypatch.setattr(routing, "EXPORT_CHUNK", 2)

    answer = await client.get("/gadgets", headers={"Accept": "text/csv"})

    header, *rows = list(csv.reader(io.StringIO(answer.text)))
    assert header[:3] == ["id", "version", "deleted_at"]
    assert sorted(row[0] for row in rows) == sorted(str(each) for each in ids)
