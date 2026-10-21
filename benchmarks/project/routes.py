"""The routes the rungs call, after the ones the init template mounts.

Routes are matched in order, so ``/health``, ``/auth`` and ``/admin/auth``
come first, as in a new project's ``config/urls.py``.
"""

from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from domainkit.contrib.auth import AnyUser, ModelPermissions, Public, Require
from domainkit.contrib.auth.admin import router as admin_router
from domainkit.contrib.auth.api import router as auth_router
from domainkit.http.health import router as health_router

from .catalog.models import Item, ItemId
from .catalog.uow import CatalogUnitOfWork

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
