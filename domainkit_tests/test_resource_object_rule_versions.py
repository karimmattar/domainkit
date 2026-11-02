"""Object rules are asked off the write's connection, and the write holds their version.

WEB-16 asks a rule that looks at the aggregate in a unit of work of its own,
so the rule may query the database (object permission grants) without the
request holding a second pooled connection. SEC-16a-1: the write then holds
the aggregate to the version the rule saw, for commands and atomic batches
as for generic writes; a change in between is 409 ``concurrent_update``.
"""

from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Receive, Scope, Send

from domainkit.conf import override_settings
from domainkit.db import connections
from domainkit.http.access import AnonymousPrincipal, BasePermission, Public
from domainkit.http.errors import install_exception_handlers
from domainkit.http.middleware import RequestContextMiddleware
from domainkit.resources import ResourceRouter
from domainkit.testing import AsyncTestClient
from domainkit_tests.gadgets import (
    Color,
    Gadget,
    GadgetId,
    GadgetsUnitOfWork,
    make_gadget,
    metadata,
)
from domainkit_tests.test_resources import RenameGadget, RenameIn

pytestmark = pytest.mark.anyio


class LooksUpGrants(BasePermission):
    """Allows red gadgets, reading the database in a unit of work of its own."""

    async def has_object_permission(
        self,
        request: HTTPConnection,
        user: Any,  # noqa: ANN401 - any principal.
        obj: object,
    ) -> bool:
        """A second connection, as ``ObjectPermissionBackend`` takes."""
        async with GadgetsUnitOfWork() as uow:
            await uow.gadgets.count(None)
        return getattr(obj, "color", None) == Color.RED


class Anyone:
    """Middleware: every request is anonymous."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app``."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Set the principal."""
        scope["user"] = AnonymousPrincipal()
        await self.app(scope, receive, send)


async def renamed(command: RenameGadget, uow: GadgetsUnitOfWork) -> Gadget:
    """Rename a gadget."""
    async with uow:
        gadget = await uow.gadgets.get(GadgetId(command.gadget_id))
        gadget.name = command.name
        await uow.commit()
    return gadget


async def untouched(command: RenameGadget, uow: GadgetsUnitOfWork) -> None:
    """Never load the gadget: nothing of it is written, nothing is held."""
    async with uow:
        await uow.commit()


@pytest.fixture
def database(database_url: str) -> Iterator[str]:
    """A database whose pool holds one connection, and waits a second for it."""
    with override_settings(
        DATABASES={
            "default": {
                "URL": database_url,
                "POOL_SIZE": 1,
                "MAX_OVERFLOW": 0,
                "POOL_TIMEOUT": 1,
            }
        }
    ):
        yield database_url


@pytest.fixture
async def client(database: str) -> AsyncIterator[AsyncTestClient]:
    """Gadgets a and b, red, served behind ``LooksUpGrants``."""
    async with connections.engine().begin() as connection:
        await connection.run_sync(metadata.create_all)
    async with GadgetsUnitOfWork() as uow:
        for name in "ab":
            await uow.gadgets.add(make_gadget(name))
        await uow.commit()
    resource = ResourceRouter(
        "/gadgets",
        model=Gadget,
        unit_of_work=GadgetsUnitOfWork,
        repository=lambda uow: uow.gadgets,
        dependencies=[Public],
        rule=LooksUpGrants(),
    )
    resource.command_route(RenameGadget, service=renamed, body=RenameIn, path="/rename")
    resource.command_route(
        RenameGadget,
        service=untouched,
        body=RenameIn,
        path="/touch",
        name="touch",
        returns=None,
    )
    resource.batch_route(RenameGadget, service=renamed, body=RenameIn)
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(resource)
    app.add_middleware(Anyone)
    app.add_middleware(RequestContextMiddleware)  # A pool timeout is a 503.
    async with AsyncTestClient(app) as served:
        yield served
    await connections.close_all()


async def stored() -> dict[str, Gadget]:
    """The gadgets, by name."""
    async with GadgetsUnitOfWork() as uow:
        return {each.name: each for each in await uow.gadgets.filter(None)}


async def test_rules_get_their_connection_and_writes_hold_what_they_saw(
    client: AsyncTestClient,
) -> None:
    """One connection is enough; a batch acting twice on one gadget is held once."""
    gadgets = await stored()
    a, b = str(gadgets["a"].id), str(gadgets["b"].id)

    command = await client.post(f"/gadgets/{a}/rename", json={"name": "a2"})
    batch = await client.post(
        "/gadgets/batch",
        json={
            "atomic": True,
            "items": [
                {"gadget_id": b, "name": "b2"},
                {"gadget_id": b, "name": "b3"},
                {"gadget_id": a, "name": "a3"},
            ],
        },
    )
    touched = await client.post(f"/gadgets/{a}/touch", json={"name": "x"})

    assert command.status_code == 200
    assert [each["status"] for each in batch.json()["results"]] == [200, 200, 200]
    assert sorted(await stored()) == ["a3", "b3"]
    assert touched.status_code == 204


async def test_if_match_answers_for_a_change_after_the_rule(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ``If-Match``, a gadget changed after its rule is the request's 412."""
    gadget = (await stored())["a"]
    allow = LooksUpGrants.has_object_permission

    async def changed_meanwhile(
        self: LooksUpGrants,
        request: HTTPConnection,
        user: Any,  # noqa: ANN401 - any principal.
        obj: object,
    ) -> bool:
        async with GadgetsUnitOfWork() as uow:
            again = await uow.gadgets.get(GadgetId(gadget.id))
            again.name += "!"
            await uow.commit()
        return await allow(self, request, user, obj)

    monkeypatch.setattr(LooksUpGrants, "has_object_permission", changed_meanwhile)

    conditional = await client.post(
        f"/gadgets/{gadget.id}/rename",
        json={"name": "late"},
        headers={"If-Match": f'"{gadget.version}"'},
    )
    blind = await client.post(f"/gadgets/{gadget.id}/rename", json={"name": "late"})

    assert (conditional.status_code, conditional.json()["code"]) == (
        412,
        "precondition_failed",
    )
    assert (blind.status_code, blind.json()["code"]) == (409, "concurrent_update")
    assert blind.json()["detail"] == (
        "This record changed while it was being checked; read it again and retry."
    )
    assert "late" not in await stored()
