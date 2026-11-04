"""The compared service on FastAPI and SQLAlchemy Core, as its docs teach it."""

import hmac
import os
from decimal import Decimal
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
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


class ItemIn(BaseModel):
    """A new item."""

    name: str = Field(min_length=1, max_length=50)
    price: Decimal = Field(ge=0, max_digits=10, decimal_places=2)


class ItemOut(BaseModel):
    """An item."""

    id: UUID
    name: str
    price: Decimal


async def api_key(x_api_key: Annotated[str, Header()] = "") -> None:
    """Refuse a request without the service's key."""
    if not hmac.compare_digest(x_api_key, API_KEY):
        raise HTTPException(401, "A valid X-API-Key is required.")


async def connection() -> AsyncConnection:  # type: ignore[misc]
    """A pooled connection for the request."""
    async with engine.connect() as connection:
        yield connection  # type: ignore[misc]


Conn = Annotated[AsyncConnection, Depends(connection)]
app = FastAPI(dependencies=[Depends(api_key)])


@app.get("/items/{item_id}")
async def read(item_id: UUID, conn: Conn) -> ItemOut:
    """One item."""
    row = (await conn.execute(select(items).where(items.c.id == item_id))).first()
    if row is None:
        raise HTTPException(404, "No item has that id.")
    return ItemOut(id=row.id, name=row.name, price=row.price)


@app.get("/items")
async def page(conn: Conn) -> dict[str, list[ItemOut]]:
    """The 20 newest items."""
    rows = await conn.execute(
        select(items).order_by(items.c.created_at.desc(), items.c.id).limit(20)
    )
    return {"items": [ItemOut(id=r.id, name=r.name, price=r.price) for r in rows]}


@app.post("/items", status_code=201)
async def create(body: ItemIn, conn: Conn) -> ItemOut:
    """A new item."""
    item_id = uuid4()
    await conn.execute(
        insert(items).values(
            id=item_id, name=body.name, price=body.price, created_at=func.now()
        )
    )
    await conn.commit()
    return ItemOut(id=item_id, name=body.name, price=body.price)
