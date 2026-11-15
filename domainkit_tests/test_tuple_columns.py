"""Tuples of text, integers, floats, or booleans: one JSON array column."""

import math
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, NewType

import pytest
from alembic.autogenerate.api import AutogenContext
from alembic.migration import MigrationContext
from sqlalchemy import Column, MetaData, String, create_engine, select
from sqlalchemy.ext.asyncio import AsyncSession

from domainkit.conf import override_settings
from domainkit.db import (
    JSONTuple,
    SqlAlchemyRepository,
    SqlAlchemyUnitOfWork,
    ValueObjectType,
    connections,
    map_imperatively,
    register_column_type,
    table_for,
)
from domainkit.db.derive import _column_types
from domainkit.db.fixtures import _decoded, _encoded
from domainkit.db.migrations import render_item
from domainkit.ddd import AggregateRoot, MaxLength, ValueObject
from domainkit.ddd.fields import fields_of
from domainkit_tests.gadgets import Color

SQLITE = create_engine("sqlite://").dialect
NoteId = NewType("NoteId", int)
Scope = NewType("Scope", str)


class Tags(ValueObject):
    """A value object of one tuple."""

    value: tuple[str, ...]


class Note(AggregateRoot[NoteId]):
    """An aggregate with a tuple of each kind ``table_for`` stores."""

    scopes: tuple[Scope, ...]
    sizes: tuple[int, ...] = ()
    weights: tuple[float, ...] | None = None
    flags: tuple[bool, ...] = ()
    labels: tuple[Annotated[str, MaxLength(10)], ...] = ()
    tags: Tags | None = None


metadata = MetaData()
notes = table_for(Note, "framework_test_tuple_notes", metadata=metadata)
map_imperatively(Note, notes)


class Notes(SqlAlchemyRepository[Note, NoteId]):
    """Notes in SQL."""


class NotesUnitOfWork(SqlAlchemyUnitOfWork):
    """One transaction over the notes."""

    notes: Notes

    def create_repositories(self, session: AsyncSession) -> None:
        """Bind the repository."""
        self.notes = Notes(session)


def test_a_tuple_of_json_scalars_is_a_json_array_column() -> None:
    """``tuple[str, ...]`` and its kin derive ``JSONTuple`` (user report D2).

    A ``NewType`` or ``Annotated`` item counts as what it stands for; a
    value object of one tuple stores it the same way.
    """
    types = {column.name: column.type for column in notes.columns}

    assert repr(types["scopes"]) == "JSONTuple(str)"
    assert repr(types["sizes"]) == "JSONTuple(int)"
    assert repr(types["weights"]) == "JSONTuple(float)"
    assert repr(types["flags"]) == "JSONTuple(bool)"
    assert repr(types["labels"]) == "JSONTuple(str)"
    assert isinstance(types["tags"], ValueObjectType)
    assert repr(types["tags"].impl_instance) == "JSONTuple(str)"
    assert not notes.c.scopes.nullable
    assert notes.c.weights.nullable
    assert fields_of(Note)["scopes"].item is str
    assert fields_of(Note)["weights"].item is float


def test_other_tuples_and_collections_still_need_a_column() -> None:
    """Pairs, enums, and lists have no derived column; the refusal says what does.

    An enum's member would come back as its bare value, so it is not taken
    (user report D2).
    """

    class Pairs(AggregateRoot[int]):
        pair: tuple[str, int]

    class Colors(AggregateRoot[int]):
        colors: tuple[Color, ...]

    class Listed(AggregateRoot[int]):
        names: list[str]

    for cls in (Pairs, Colors):
        with pytest.raises(TypeError, match=r"no column type for tuple \(a tuple\["):
            table_for(cls, "refused", metadata=MetaData())
    with pytest.raises(TypeError, match=r"Listed\.names: no column type for list;"):
        table_for(Listed, "refused", metadata=MetaData())
    with pytest.raises(TypeError, match="not Color"):
        JSONTuple(Color)
    assert fields_of(Pairs)["pair"].item is None


def test_a_registered_or_given_tuple_column_still_wins() -> None:
    """A project that registered ``tuple``, or passed a column, keeps its own (D2)."""

    class Kept(AggregateRoot[int]):
        names: tuple[str, ...]

    given = table_for(
        Kept, "given", Column("names", String(200), nullable=False), metadata=MetaData()
    )
    try:
        register_column_type(tuple, String(100))
        registered = table_for(Kept, "registered", metadata=MetaData())
    finally:
        _column_types.pop(tuple)

    assert isinstance(given.c.names.type, String)
    assert isinstance(registered.c.names.type, String)
    assert registered.c.names.type.length == 100


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        (JSONTuple(str), ["a"], r"JSONTuple\(str\) column takes a tuple, not list"),
        (JSONTuple(str), ("a", 1), r"takes str items, not int\."),
        (JSONTuple(int), (1, True), r"takes int items, not bool\."),
        (JSONTuple(int), (1, 2.0), r"takes int items, not float\."),
        (JSONTuple(bool), (1,), r"takes bool items, not int\."),
        (JSONTuple(str), (Color.RED,), r"takes str items, not Color\."),
        (JSONTuple(float), (1.5, math.nan), r"takes finite floats\."),
        (JSONTuple(float), (math.inf,), r"takes finite floats\."),
    ],
)
def test_items_json_would_not_give_back_are_refused(
    column: JSONTuple, value: object, message: str
) -> None:
    """A value is refused, naming classes and never the value (user report D2)."""
    with pytest.raises(TypeError, match=message) as caught:
        column.process_bind_param(value, SQLITE)  # type: ignore[arg-type]

    assert "'a'" not in str(caught.value)
    assert JSONTuple(float).process_bind_param((1, 2.5), SQLITE) == [1, 2.5]


def test_revisions_write_a_tuple_column_as_json() -> None:
    """A revision names ``sa.JSON``, never domainkit's type (user report D2)."""
    context = AutogenContext(MigrationContext.configure(dialect_name="sqlite"))

    assert render_item("type", JSONTuple(str), context) == "sa.JSON(none_as_null=True)"


def test_fixtures_hold_a_tuple_as_a_json_array() -> None:
    """``dumpdata`` writes the array; ``loaddata`` reads it back a tuple (D2)."""
    assert _encoded(notes.c.scopes, ("read", "write")) == ["read", "write"]
    assert _decoded(notes.c.scopes, ["read", "write"]) == ("read", "write")
    assert _decoded(notes.c.weights, [1, 2.5]) == (1, 2.5)


@pytest.fixture(params=["sqlite", "postgresql"])
async def database(
    request: pytest.FixtureRequest, database_access: None, tmp_path: Path
) -> AsyncIterator[None]:
    """The notes table, on SQLite or (where it is set) PostgreSQL."""
    url = (
        f"sqlite+aiosqlite:///{tmp_path / 'notes.sqlite3'}"
        if request.param == "sqlite"
        else os.environ.get("DOMAINKIT_TEST_POSTGRES_URL")
    )
    if not url:
        pytest.skip("set DOMAINKIT_TEST_POSTGRES_URL")
    with override_settings(DATABASES={"default": {"URL": url}}):
        async with connections.engine().begin() as connection:
            await connection.run_sync(metadata.drop_all)
            await connection.run_sync(metadata.create_all)
        try:
            yield
        finally:
            async with connections.engine().begin() as connection:
                await connection.run_sync(metadata.drop_all)
            await connections.close_all()


@pytest.mark.anyio
async def test_a_tuple_comes_back_a_tuple_on_either_database(database: None) -> None:
    """Saved, loaded, changed, and loaded again, each field is a tuple (D2).

    Integers in a float tuple come back floats; None is SQL ``NULL``, not
    JSON's ``null``.
    """
    note = Note(
        id=NoteId(1),
        scopes=(Scope("read"), Scope("write")),
        sizes=(1, 2**40),
        flags=(True, False),
        labels=("é", "x y"),
        tags=Tags(("a",)),
    )
    async with NotesUnitOfWork() as uow:
        await uow.notes.add(note)
        await uow.commit()
    async with connections.engine().connect() as connection:
        unweighed = await connection.scalar(
            select(notes.c.id).where(notes.c.weights.is_(None))
        )
    async with NotesUnitOfWork() as uow:
        stored = await uow.notes.get(NoteId(1))
        loaded = (stored.scopes, stored.sizes, stored.weights, stored.flags)
        labels, tags = stored.labels, stored.tags
        stored.weights = (1, 2.5)
        stored.scopes = ()
        await uow.commit()
    async with NotesUnitOfWork() as uow:
        changed = await uow.notes.get(NoteId(1))

    assert unweighed == 1
    assert loaded == (("read", "write"), (1, 2**40), None, (True, False))
    assert (labels, tags) == (("é", "x y"), Tags(("a",)))
    assert isinstance(tags.value, tuple)  # type: ignore[union-attr]
    assert changed.weights == (1.0, 2.5)
    assert all(isinstance(each, float) for each in changed.weights or ())
    assert changed.scopes == ()
