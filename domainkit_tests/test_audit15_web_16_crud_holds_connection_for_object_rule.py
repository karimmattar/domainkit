"""WEB-16: generic writes ask object rules while holding the request's connection.

``domainkit.resources.routing.reach`` says a rule that looks at objects
"may read the database itself (object permission grants), and must not
wait for a connection while this request holds one", and reads, commands,
and queries close their unit of work before asking it. The generic
``PATCH`` and ``DELETE`` of ``crud=True`` (``CrudRoutes.changed`` and
``deleted``) ask the rule inside ``async with context.uow``, after the
aggregate's ``SELECT`` took a connection. ``ObjectPermissionBackend``
opens a unit of work of its own for each check: once as many writes run
at once as the pool holds, each waits for a second connection that none
gives back, and all of them fail after ``POOL_TIMEOUT`` with
``503 database_busy``, as does every other request meanwhile.

Here the pool holds one connection: a read of the item answers, the
generic write of the same item, asked by the same rule, does not.
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
from domainkit.resources import ALL_FIELDS, ResourceRouter
from domainkit.testing import AsyncTestClient
from domainkit_tests.gadgets import (
    Gadget,
    GadgetsUnitOfWork,
    make_gadget,
    metadata,
)

pytestmark = pytest.mark.anyio


class LooksUpGrants(BasePermission):
    """Asks the database about the object, as ``ObjectPermissionBackend`` does."""

    async def has_object_permission(
        self,
        request: HTTPConnection,
        user: Any,  # noqa: ANN401 - any principal.
        obj: object,
    ) -> bool:
        """Read in a unit of work of its own: a second connection."""
        async with GadgetsUnitOfWork() as uow:
            await uow.gadgets.count(None)
        return True


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
    """The gadget resource, generic writes on, guarded by the rule above."""
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
        rule=LooksUpGrants(),
        crud=True,
        writable=ALL_FIELDS,
    )
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(resource)
    app.add_middleware(Anyone)
    app.add_middleware(RequestContextMiddleware)  # A pool timeout is a 503.
    async with AsyncTestClient(app) as served:
        yield served
    await connections.close_all()


async def test_generic_writes_ask_object_rules_with_no_connection_held(
    client: AsyncTestClient,
) -> None:
    """The rule gets its connection on a write as it does on a read."""
    listed = (await client.get("/gadgets")).json()["items"]
    item = f"/gadgets/{listed[0]['id']}"

    read = await client.get(item)
    patched = await client.patch(item, json={"stock": 3})
    deleted = await client.delete(item)

    assert read.status_code == 200
    assert (patched.status_code, patched.json().get("code")) == (200, None)
    assert deleted.status_code == 204
