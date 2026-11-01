"""SQL written around an amount column, on SQLite: in the amount, not minor units.

``ExactDecimal`` stores minor units on SQLite. A plain-number server default
is written in them, and a check written as SQL compares each amount column
as a real number of its own units (DATA-15), whether the table is made by
``create_all`` or by a revision that declares the column as plain ``NUMERIC``.
On PostgreSQL nothing changes. A row naming no parent is refused on SQLite as
on PostgreSQL (DATA-18), and ``migrate`` checks the rows already stored.
"""

import io
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest
from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    insert,
    make_url,
    select,
    text,
)
from sqlalchemy.dialects import sqlite
from sqlalchemy.engine import Dialect
from sqlalchemy.exc import CompileError, IntegrityError
from sqlalchemy.schema import CreateTable

from domainkit.conf import override_settings
from domainkit.db import ExactDecimal, connections, orm
from domainkit.db import migrations as migrations_module
from domainkit.db.migrations import ForeignKeyViolation
from domainkit.db.orm import NAMING_CONVENTION
from domainkit.management import call_command

pytestmark = pytest.mark.anyio
POSTGRES: Final = make_url("postgresql+asyncpg://").get_dialect()()


def ddl(table: Table, dialect: Dialect | None = None) -> str:
    """``CREATE TABLE`` of ``table`` on SQLite (or ``dialect``)."""
    return str(CreateTable(table).compile(dialect=dialect or sqlite.dialect()))


def test_a_number_default_is_written_in_minor_units_on_sqlite_only() -> None:
    """``'1.50'`` and ``(2)`` become 150 and 200; PostgreSQL keeps them."""
    table = Table(
        "fees",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("fee", ExactDecimal(12, 2), server_default="1.50"),
        Column("cap", ExactDecimal(12, 2), server_default=text("(2)")),
        Column("count", Integer, server_default=text("3")),
        Column("whole", ExactDecimal(12, 0), server_default=text("4")),
    )

    written = ddl(table)

    assert "fee NUMERIC(12, 2) DEFAULT 150" in written
    assert "cap NUMERIC(12, 2) DEFAULT 200" in written
    assert "count INTEGER DEFAULT 3" in written
    assert "whole NUMERIC(12, 0) DEFAULT 4" in written
    assert "DEFAULT '1.50'" in ddl(table, POSTGRES)


def test_a_default_that_is_not_a_number_is_left_alone() -> None:
    """An expression is SQL the column type cannot read: it stays as written."""
    table = Table(
        "fees",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("fee", ExactDecimal(12, 2), server_default=text("abs(-1)")),
    )

    assert "DEFAULT (abs(-1))" in ddl(table)


def test_a_default_with_more_places_than_the_column_is_refused() -> None:
    """``1.005`` in a scale of 2 is no amount the column holds."""
    table = Table(
        "fees",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("fee", ExactDecimal(12, 2), server_default="1.005"),
    )

    with pytest.raises(CompileError, match="more than 2 decimal places"):
        ddl(table)


def test_a_check_compares_amounts_in_their_own_units() -> None:
    """Bare, quoted, and qualified names of amounts are divided; the rest stay."""
    table = Table(
        "fees",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("fee", ExactDecimal(12, 2), CheckConstraint("fee >= 0")),
        Column("Cap", ExactDecimal(12, 3)),
        Column("note", Integer),
        CheckConstraint(
            """"Cap" <= 100 AND fees.fee < 5 AND note > 0 AND 'fee' <> 'x'""",
            name="caps",
        ),
        CheckConstraint(text("note < 10"), name="notes"),
    )

    written = ddl(table)

    assert "CHECK ((fee / 100.0) >= 0)" in written
    assert (
        """CHECK (("Cap" / 1000.0) <= 100 AND (fees.fee / 100.0) < 5 """
        """AND note > 0 AND 'fee' <> 'x')"""
    ) in written
    assert "CHECK (note < 10)" in written
    assert "CHECK (fee >= 0)" in ddl(table, POSTGRES)


def test_a_check_built_as_an_expression_binds_its_value() -> None:
    """``fee <= 100`` built from the column is bound through its type already."""
    metadata = MetaData()
    fee: Column[Decimal] = Column("fee", ExactDecimal(12, 2))
    table = Table("fees", metadata, Column("id", Integer, primary_key=True), fee)
    table.append_constraint(CheckConstraint(fee <= Decimal(100), name="cap"))

    assert "CHECK (fee <= 10000)" in ddl(table)


@pytest.fixture
def project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, database_access: None
) -> Iterator[tuple[MetaData, str]]:
    """A SQLite database, its revisions, and the models' metadata."""
    schema = MetaData(naming_convention=NAMING_CONVENTION)
    monkeypatch.setattr(migrations_module, "metadata", schema)
    monkeypatch.setattr(orm, "metadata", schema)  # Where columns are looked up.
    url = f"sqlite+aiosqlite:///{tmp_path / 'amounts.sqlite3'}"
    with override_settings(
        MIGRATIONS_DIR=tmp_path / "migrations", DATABASES={"default": {"URL": url}}
    ):
        yield schema, str(tmp_path / "amounts.sqlite3")


def migrate(message: str) -> None:
    """Write the next revision and apply it."""
    call_command("makemigrations", "-m", message, stdout=io.StringIO())
    call_command("migrate", stdout=io.StringIO())


async def test_revisions_write_defaults_and_checks_in_minor_units(
    project: tuple[MetaData, str],
) -> None:
    """A created table and an added column read back their defaults as declared."""
    schema, _ = project
    first = Table(
        "framework_test_fees",
        schema,
        Column("id", Integer, primary_key=True),
        Column("fee", ExactDecimal(12, 2), nullable=False, server_default="1.50"),
        CheckConstraint("fee <= 100", name="fee_cap"),
    )
    migrate("fees")
    schema.remove(first)
    fees = Table(
        "framework_test_fees",
        schema,
        Column("id", Integer, primary_key=True),
        Column("fee", ExactDecimal(12, 2), nullable=False, server_default="1.50"),
        Column("tip", ExactDecimal(12, 2), nullable=False, server_default="0.25"),
        Column("count", Integer, nullable=False, server_default="3"),
        CheckConstraint("fee <= 100", name="fee_cap"),
    )
    migrate("tips")
    try:
        async with connections.engine().begin() as connection:
            await connection.execute(insert(fees).values(id=1))
            await connection.execute(insert(fees).values(id=2, fee=Decimal("99.00")))
            stored = (
                await connection.execute(select(fees.c.fee, fees.c.tip, fees.c.count))
            ).all()
        assert stored == [
            (Decimal("1.50"), Decimal("0.25"), 3),  # Not an amount: as written.
            (Decimal("99.00"), Decimal("0.25"), 3),
        ]
        with pytest.raises(IntegrityError):
            async with connections.engine().begin() as connection:
                await connection.execute(
                    insert(fees).values(id=3, fee=Decimal("100.01"))
                )
    finally:
        await connections.close_all()


def test_migrate_refuses_rows_that_name_no_parent(
    project: tuple[MetaData, str],
) -> None:
    """Orphans stored while SQLite checked nothing fail ``migrate``, by table."""
    schema, path = project
    parents = Table(
        "framework_test_parents", schema, Column("id", Integer, primary_key=True)
    )
    Table(
        "framework_test_children",
        schema,
        Column("id", Integer, primary_key=True),
        Column("parent_id", ForeignKey(parents.c.id), nullable=False),
    )
    migrate("parents")
    with closing(sqlite3.connect(path)) as raw:  # As before 0.15: nothing checked.
        raw.execute("INSERT INTO framework_test_children VALUES (1, 404)")
        raw.commit()
    Table("framework_test_more", schema, Column("id", Integer, primary_key=True))
    call_command("makemigrations", "-m", "more", stdout=io.StringIO())

    with pytest.raises(ForeignKeyViolation, match="framework_test_children"):
        call_command("migrate", stdout=io.StringIO())


async def test_a_parent_recreated_by_a_revision_keeps_its_children(
    project: tuple[MetaData, str],
) -> None:
    """Batch mode recreates a parent on SQLite; foreign keys wait until it ran."""
    schema, _ = project
    parents = Table(
        "framework_test_shelves",
        schema,
        Column("id", Integer, primary_key=True),
        Column("name", String(20), nullable=False),
    )
    books = Table(
        "framework_test_books",
        schema,
        Column("id", Integer, primary_key=True),
        Column("shelf_id", ForeignKey(parents.c.id, ondelete="CASCADE")),
    )
    migrate("shelves")
    try:
        async with connections.engine().begin() as connection:
            await connection.execute(insert(parents).values(id=1, name="a"))
            await connection.execute(insert(books).values(id=1, shelf_id=1))
        schema.remove(parents)
        Table(
            "framework_test_shelves",
            schema,
            Column("id", Integer, primary_key=True),
            Column("name", String(40), nullable=False),
        )
        migrate("longer names")
        async with connections.engine().connect() as connection:
            kept = (await connection.execute(select(books.c.shelf_id))).all()
        assert kept == [(1,)]
    finally:
        await connections.close_all()


def test_a_column_the_models_no_longer_have_is_written_as_it_is(
    project: tuple[MetaData, str], tmp_path: Path
) -> None:
    """A revision's column dropped later is no amount the models know of."""
    schema, path = project
    first = Table(
        "framework_test_dropped",
        schema,
        Column("id", Integer, primary_key=True),
        Column("old", ExactDecimal(12, 2), server_default="1.50"),
    )
    migrate("with old")
    schema.remove(first)
    Table("framework_test_dropped", schema, Column("id", Integer, primary_key=True))
    migrate("without old")
    Path(path).unlink()

    call_command("migrate", stdout=io.StringIO())  # Afresh: "old" made, then gone.
