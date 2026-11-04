"""The compared service on Litestar and SQLAlchemy Core, as its docs teach it."""

import hmac
import os
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Annotated
from uuid import UUID, uuid4

import msgspec
from litestar import Litestar, get, post
from litestar.connection import ASGIConnection
from litestar.di import Provide
from litestar.exceptions import NotAuthorizedException, NotFoundException
from litestar.handlers.base import BaseRouteHandler
from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Uuid,
    func,
    insert,
    select,
)
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

API_KEY = os.environ["COMPARE_API_KEY"]
engine = create_async_engine(os.environ["COMPARE_DATABASE_URL"], pool_size=10)
items = Table(
    "compare_items",
    MetaData(),
    Column("id", Uuid, primary_key=True),
    Column("version", Integer),
    Column("name", String(50)),
    Column("price", Numeric(10, 2)),
    Column("created_at", DateTime(timezone=True)),
)


class ItemIn(msgspec.Struct):
    """A new item."""

    name: Annotated[str, msgspec.Meta(min_length=1, max_length=50)]
    price: Decimal

    def __post_init__(self) -> None:
        """At least 0, and at most 10 digits, 2 of them after the point."""
        exponent = self.price.as_tuple().exponent
        if (
            not isinstance(exponent, int)
            or exponent < -2
            or not 0 <= self.price < 10**8
        ):
            raise ValueError("A price is 0 or more, with at most 2 decimal places.")


class ItemOut(msgspec.Struct):
    """An item."""

    id: UUID
    name: str
    price: Decimal


def api_key(connection: ASGIConnection, _: BaseRouteHandler) -> None:  # type: ignore[type-arg]
    """Refuse a request without the service's key."""
    if not hmac.compare_digest(connection.headers.get("x-api-key", ""), API_KEY):
        raise NotAuthorizedException("A valid X-API-Key is required.")


async def provide_connection() -> AsyncIterator[AsyncConnection]:
    """A pooled connection for the request."""
    async with engine.connect() as connection:
        yield connection


@get("/items/{item_id:uuid}")
async def read(item_id: UUID, conn: AsyncConnection) -> ItemOut:
    """One item."""
    row = (await conn.execute(select(items).where(items.c.id == item_id))).first()
    if row is None:
        raise NotFoundException("No item has that id.")
    return ItemOut(id=row.id, name=row.name, price=row.price)


@get("/items")
async def page(conn: AsyncConnection) -> dict[str, list[ItemOut]]:
    """The 20 newest items."""
    rows = await conn.execute(
        select(items).order_by(items.c.created_at.desc(), items.c.id).limit(20)
    )
    return {"items": [ItemOut(id=r.id, name=r.name, price=r.price) for r in rows]}


@post("/items")
async def create(data: ItemIn, conn: AsyncConnection) -> ItemOut:
    """A new item (201)."""
    item_id = uuid4()
    await conn.execute(
        insert(items).values(
            id=item_id, name=data.name, price=data.price, created_at=func.now()
        )
    )
    await conn.commit()
    return ItemOut(id=item_id, name=data.name, price=data.price)


app = Litestar(
    route_handlers=[read, page, create],
    guards=[api_key],
    dependencies={"conn": Provide(provide_connection)},
)
