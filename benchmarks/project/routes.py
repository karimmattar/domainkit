"""The routes the rungs call, after the ones the init template mounts.

Routes are matched in order, so ``/health``, ``/auth`` and ``/admin/auth``
come first, as in a new project's ``config/urls.py``.
"""

from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from starlette.requests import HTTPConnection

from domainkit.contrib.auth import AnyUser
from domainkit.contrib.auth.admin import router as admin_router
from domainkit.contrib.auth.api import router as auth_router
from domainkit.ddd import Command, F
from domainkit.ddd.query import CriteriaLike
from domainkit.http.health import router as health_router
from domainkit.resources import ModelActions, ResourceRouter

from .catalog.models import Item, ItemId
from .catalog.uow import CatalogUnitOfWork

try:
    from domainkit.http.access import ModelPermissions, Public, Require
except ImportError:  # A tree before 0.10 (``--baseline``): they were auth's.
    from domainkit.contrib.auth import (  # type: ignore[attr-defined,no-redef]
        ModelPermissions,
        Public,
        Require,
    )

router = APIRouter()
router.include_router(health_router)
router.include_router(auth_router, prefix="/auth")
router.include_router(admin_router, prefix="/admin/auth")

Caller = Annotated[AnyUser, Depends(Require())]
"""Any user who logged in."""
Clerk = Annotated[AnyUser, Depends(Require(ModelPermissions(Item)))]
"""A user with the item permission the method needs (``catalog.view_item``...)."""


class ItemIn(BaseModel):
    """A new item."""

    name: str = Field(max_length=100)
    price: int = Field(ge=0)


class ItemOut(BaseModel):
    """An item."""

    id: UUID
    name: str
    price: int


@router.get("/hello", dependencies=[Public])
async def hello() -> dict[str, str]:
    """The lightest route: a constant."""
    return {"status": "ok"}


inner = APIRouter()


@inner.get("/hello", dependencies=[Public])
async def nested_hello() -> dict[str, str]:
    """The same constant, two routers deep: ``/a/b/hello``."""
    return {"status": "ok"}


middle = APIRouter()
middle.include_router(inner, prefix="/b")
router.include_router(middle, prefix="/a")


@router.get("/me")
async def me(user: Caller) -> dict[str, str]:
    """Who the token belongs to."""
    return {"user": user.get_username()}


@router.get("/items/{item_id}")
async def get_item(item_id: UUID, user: Clerk) -> ItemOut:
    """One item, read through a unit of work."""
    async with CatalogUnitOfWork() as uow:
        item = await uow.items.get(ItemId(item_id))
    return ItemOut(id=item.id, name=item.name, price=item.price)


@router.post("/items", status_code=201)
async def create_item(body: ItemIn, user: Clerk) -> ItemOut:
    """A new item, written through a unit of work."""
    item = Item(id=ItemId(uuid4()), name=body.name, price=body.price)
    async with CatalogUnitOfWork() as uow:
        await uow.items.add(item)
        await uow.commit()
    return ItemOut(id=item.id, name=item.name, price=item.price)


class RenameItem(Command):
    """Rename an item."""

    item_id: UUID
    name: str


class RenameIn(BaseModel):
    """An item's new name."""

    name: str = Field(max_length=100)


async def rename_item(command: RenameItem, uow: CatalogUnitOfWork) -> Item:
    """Rename the item, in the unit of work given."""
    async with uow:
        item = await uow.items.get(ItemId(command.item_id))
        item.name = command.name
        await uow.commit()
    return item


async def priced(request: HTTPConnection) -> CriteriaLike:
    """What a caller reaches of the items: those with a price (all of them)."""
    return F("price") >= 0


resources = ResourceRouter(
    "/resources/items",
    model=Item,
    unit_of_work=CatalogUnitOfWork,
    repository=lambda uow: uow.items,
    schema=ItemOut,
    rule=ModelActions(Item),
    scope=priced,
)
"""The items as a resource: reads and commands go through ``ResourceRouter``."""
resources.command_route(
    RenameItem, service=rename_item, body=RenameIn, path="/rename", action="add"
)
router.include_router(resources)
