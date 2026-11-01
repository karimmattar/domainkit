"""SEC-16a-1: commands and atomic batches write past a rule asked of an older version.

WEB-16 moved the object rules of generic writes and atomic batches out of
the write's transaction. For ``PATCH``/``DELETE`` (``CrudRoutes.checked``)
the version the rules saw is pinned: a change committed in between
answers ``409 concurrent_update``. For an atomic batch
(``BatchRoute.check_objects``) nothing is pinned: every item is asked
about first, then the transaction loads each aggregate afresh and runs its
service with no rule and no version check (``objects = self.objects and
not isinstance(work, Enlisted)``). Before the fix the rule ran inside that
transaction, on the very instance the service then wrote (identity map
plus ``version_id_col``), so a concurrent change failed the flush. A
single command (``reach.on_one``) has the same gap.

So a caller allowed on the version the rules saw writes to the aggregate
after it was taken from them (moved to another owner, frozen): a
version-bumping change that the rule would refuse. An atomic batch of up
to 100 items widens the window at will: item N is asked about before
items 1..N-1 run.

Here the rule lets the caller act on a gadget while it is red (stands for
"owned by the caller"); right after it says yes, another request commits
the gadget blue (given away). The write must answer 409 (or 403) and leave
the gadget alone, as the generic ``PATCH`` does (the control test).
"""

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Receive, Scope, Send

from domainkit.db import connections
from domainkit.ddd import Command
from domainkit.http.access import AnonymousPrincipal, BasePermission, Public
from domainkit.http.errors import install_exception_handlers
from domainkit.resources import ALL_FIELDS, ResourceRouter
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


class OwnedWhileRed(BasePermission):
    """The caller owns a gadget while it is red; it is given away right after."""

    async def has_object_permission(
        self,
        request: HTTPConnection,
        user: Any,  # noqa: ANN401 - a principal.
        obj: object,
    ) -> bool:
        """Answer for the version seen; then a concurrent request gives it away."""
        assert isinstance(obj, Gadget)
        allowed = obj.color == Color.RED
        if allowed:
            async with GadgetsUnitOfWork() as uow:  # Another request, committed.
                stored = await uow.gadgets.get(GadgetId(obj.id))
                stored.color = Color.BLUE
                await uow.commit()
        return allowed


async def renamed(command: RenameGadget, uow: GadgetsUnitOfWork) -> Gadget:
    """Rename a gadget."""
    async with uow:
        gadget = await uow.gadgets.get(GadgetId(command.gadget_id))
        gadget.name = command.name
        await uow.commit()
    return gadget


class Anyone:
    """Middleware: every request is anonymous."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app``."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Set the principal."""
        scope["user"] = AnonymousPrincipal()
        await self.app(scope, receive, send)


@pytest.fixture
async def gadget(database_url: str) -> AsyncIterator[GadgetId]:
    """One red gadget, ``a``, stored."""
    async with connections.engine().begin() as connection:
        await connection.run_sync(metadata.create_all)
    made = make_gadget("a")
    async with GadgetsUnitOfWork() as uow:
        await uow.gadgets.add(made)
        await uow.commit()
    yield GadgetId(made.id)
    await connections.close_all()


def served(**options: Any) -> AsyncTestClient:  # noqa: ANN401 - router options.
    """The gadget resource, guarded by ``OwnedWhileRed``."""
    resource = ResourceRouter(
        "/gadgets",
        model=Gadget,
        unit_of_work=GadgetsUnitOfWork,
        repository=lambda uow: uow.gadgets,
        dependencies=[Public],
        rule=OwnedWhileRed(),
        **options,
    )
    resource.batch_route(RenameGadget, service=renamed, body=RenameIn)
    resource.command_route(RenameGadget, service=renamed, body=RenameIn, path="/rename")
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(resource)
    app.add_middleware(Anyone)
    return AsyncTestClient(app)


async def name_of(gadget: GadgetId) -> str:
    """The stored name."""
    async with GadgetsUnitOfWork() as uow:
        return (await uow.gadgets.get(gadget)).name


async def test_control_the_generic_patch_refuses_a_gadget_taken_since_its_check(
    gadget: GadgetId,
) -> None:
    """What WEB-16's fix does for ``PATCH``: 409, nothing written (passes)."""
    async with served(crud=True, writable=ALL_FIELDS) as client:
        answer = await client.patch(f"/gadgets/{gadget}", json={"name": "mine"})

    assert (answer.status_code, await name_of(gadget)) == (409, "a")


async def test_an_atomic_batch_does_not_write_a_gadget_taken_since_its_check(
    gadget: GadgetId,
) -> None:
    """The batch's item answers 409 or 403 and the gadget keeps its name."""
    async with served() as client:
        answer = await client.post(
            "/gadgets/batch",
            json={
                "atomic": True,
                "items": [{"gadget_id": str(gadget), "name": "mine"}],
            },
        )

    statuses = tuple(each["status"] for each in answer.json()["results"])
    assert (statuses, await name_of(gadget)) in {((409,), "a"), ((403,), "a")}, (
        f"renamed a gadget given away after the check: {statuses}"
    )


async def test_a_command_does_not_write_a_gadget_taken_since_its_check(
    gadget: GadgetId,
) -> None:
    """The command answers 409 or 403 and the gadget keeps its name."""
    async with served() as client:
        answer = await client.post(f"/gadgets/{gadget}/rename", json={"name": "mine"})

    assert (answer.status_code, await name_of(gadget)) in {(409, "a"), (403, "a")}, (
        f"renamed a gadget given away after the check: {answer.status_code}"
    )


_ = Command  # The commands come from test_resources.
