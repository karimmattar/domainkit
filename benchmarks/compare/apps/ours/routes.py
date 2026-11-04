"""The three routes, through a unit of work; a dependency checks the key."""

import hmac
import os
from decimal import Decimal
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel

from domainkit import clock
from domainkit.ddd import Digits, MaxLength, MinLength, Range, Selection
from domainkit.http.access import NotAuthenticated, Public

from .catalog.models import CatalogUnitOfWork, Item, ItemId

API_KEY = os.environ["COMPARE_API_KEY"]


async def api_key(x_api_key: Annotated[str, Header()] = "") -> None:
    """Refuse a request without the service's key."""
    if not hmac.compare_digest(x_api_key, API_KEY):
        raise NotAuthenticated()


router = APIRouter(dependencies=[Public, Depends(api_key)])


class ItemIn(BaseModel):
    """A new item."""

    name: Annotated[str, MinLength(1), MaxLength(50)]
    price: Annotated[Decimal, Digits(10, 2), Range(min=0)]


class ItemOut(BaseModel):
    """An item."""

    id: UUID
    name: str
    price: Decimal


def out(item: Item) -> ItemOut:
    """An item as the API shows it."""
    return ItemOut(id=item.id, name=item.name, price=item.price)


@router.get("/items/{item_id}")
async def read(item_id: UUID) -> ItemOut:
    """One item (404 ``item_not_found`` without it)."""
    async with CatalogUnitOfWork() as uow:
        return out(await uow.items.get(ItemId(item_id)))


@router.get("/items")
async def page() -> dict[str, list[ItemOut]]:
    """The 20 newest items."""
    async with CatalogUnitOfWork() as uow:
        found = await uow.items.filter(Selection(ordering=["-created_at"], limit=20))
    return {"items": [out(item) for item in found]}


@router.post("/items", status_code=201)
async def create(body: ItemIn) -> ItemOut:
    """A new item."""
    item = Item(
        id=ItemId(uuid4()), name=body.name, price=body.price, created_at=clock.now()
    )
    async with CatalogUnitOfWork() as uow:
        await uow.items.add(item)
        await uow.commit()
    return out(item)
