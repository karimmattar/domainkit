"""Relationships never load lazily by accident: unloaded access raises."""

from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import Column, ForeignKey, Integer, MetaData, Table, insert, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import backref, column_property, relationship, selectinload

from domainkit.db import map_imperatively

pytestmark = pytest.mark.anyio

shelf_metadata = MetaData()
shelves = Table(
    "framework_test_shelves", shelf_metadata, Column("id", Integer, primary_key=True)
)
books = Table(
    "framework_test_books",
    shelf_metadata,
    Column("id", Integer, primary_key=True),
    Column("shelf_id", ForeignKey("framework_test_shelves.id")),
)
LAZY = "lazy='raise'"


class Shelf:
    """Holds books through relationships of every kind of loading."""

    id: int
    books: Any
    eager: Any
    on_purpose: Any


class Book:
    """On a shelf; its way back is a backref."""

    id: int
    twice: int
    shelf: Any
    eager_shelf: Any
    selected_shelf: Any


# Properties other than relationships are left as they are.
map_imperatively(Book, books, properties={"twice": column_property(books.c.id * 2)})
map_imperatively(
    Shelf,
    shelves,
    properties={
        "books": relationship(Book, backref="shelf"),
        "eager": relationship(
            Book, lazy="selectin", viewonly=True, backref=backref("eager_shelf")
        ),
        "on_purpose": relationship(
            Book,
            lazy=True,
            viewonly=True,
            backref=backref("selected_shelf", lazy="selectin", viewonly=True),
        ),
    },
)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A session on an SQLite database holding one shelf with one book."""
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(shelf_metadata.create_all)
        await connection.execute(insert(shelves).values(id=1))
        await connection.execute(insert(books).values(id=7, shelf_id=1))
    async with AsyncSession(engine, expire_on_commit=False) as opened:
        yield opened
    await engine.dispose()


async def test_default_relationships_raise_when_not_loaded(
    session: AsyncSession,
) -> None:
    """Left at SQLAlchemy's default, access raises; loaded in the query, it works."""
    shelf = (await session.execute(select(Shelf))).scalar_one()

    with pytest.raises(InvalidRequestError, match=LAZY):
        _ = shelf.books
    loaded = (
        await session.execute(
            select(Shelf)
            .options(selectinload(Shelf.books))
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert [book.id for book in loaded.books] == [7]


async def test_explicit_loading_is_kept(session: AsyncSession) -> None:
    """``lazy="selectin"`` loads with the query; ``lazy=True`` loads on purpose."""
    shelf = (await session.execute(select(Shelf))).scalar_one()

    assert [book.id for book in shelf.eager] == [7]
    on_purpose = await session.run_sync(lambda _: list(shelf.on_purpose))
    assert [book.id for book in on_purpose] == [7]
    mapper = sa_inspect(Shelf)
    assert mapper is not None
    assert mapper.relationships["on_purpose"].lazy is True


async def test_backrefs_follow_the_same_rule(session: AsyncSession) -> None:
    """A backref without ``lazy`` raises; one that names its loading keeps it."""
    book = (await session.execute(select(Book))).scalar_one()

    assert book.twice == 14
    with pytest.raises(InvalidRequestError, match=LAZY):
        _ = book.shelf
    with pytest.raises(InvalidRequestError, match=LAZY):
        _ = book.eager_shelf
    assert book.selected_shelf.id == 1


def test_relationships_are_set_to_raise_as_they_are_mapped() -> None:
    """``map_imperatively`` changes the relationships it maps, and their backrefs.

    The classes above are mapped on import; these are mapped here, so the
    change itself is seen.
    """
    metadata = MetaData()
    parents = Table(
        "framework_test_parents", metadata, Column("id", Integer, primary_key=True)
    )
    kids = Table(
        "framework_test_kids",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("parent_id", ForeignKey("framework_test_parents.id")),
    )

    class Parent:
        """Has kids, three ways."""

    class Kid:
        """Belongs to a parent."""

    default = relationship(Kid, backref="parent")
    viewed = relationship(Kid, viewonly=True, backref=backref("viewer", viewonly=True))
    kept = relationship(
        Kid,
        lazy="selectin",
        viewonly=True,
        backref=backref("keeper", lazy="selectin", viewonly=True),
    )
    map_imperatively(Kid, kids)
    map_imperatively(
        Parent, parents, properties={"kids": default, "viewed": viewed, "kept": kept}
    )

    assert (default.lazy, default.strategy_key) == ("raise", (("lazy", "raise"),))
    assert default.backref == ("parent", {"lazy": "raise"})
    assert viewed.lazy == "raise"
    assert viewed.backref == ("viewer", {"viewonly": True, "lazy": "raise"})
    assert (kept.lazy, kept.backref) == (
        "selectin",
        ("keeper", {"lazy": "selectin", "viewonly": True}),
    )
