"""Checks declared on the aggregate (Django's ``Meta.constraints`` checks)."""

import io
from collections.abc import AsyncIterator, Iterator
from datetime import date
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated, NewType

import pytest
from sqlalchemy import CheckConstraint, MetaData, insert
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import Dialect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from domainkit.conf import override_settings
from domainkit.db import (
    SqlAlchemyRepository,
    SqlAlchemyUnitOfWork,
    connections,
    map_imperatively,
    table_for,
)
from domainkit.db import migrations as migrations_module
from domainkit.db.errors import integrity_violation
from domainkit.db.orm import NAMING_CONVENTION
from domainkit.ddd import (
    AggregateRoot,
    CheckFailed,
    Digits,
    Entity,
    F,
    MaxLength,
    Repository,
    UnitOfWork,
)
from domainkit.ddd.query import declared_checks
from domainkit.ddd.stores import in_memory
from domainkit.management import call_command

pytestmark = pytest.mark.anyio

ItemId = NewType("ItemId", int)


class Status(StrEnum):
    """Where an item is."""

    OPEN = "open"
    SHUT = "shut"


class Item(AggregateRoot[ItemId]):
    """Stock that never goes below zero, an optional cap, a status in a set."""

    stock: int
    cap: int | None = None
    status: Status = Status.OPEN
    code: Annotated[str, MaxLength(8)] = "A"

    class Meta:
        """The rules every stored item keeps."""

        checks = {  # noqa: RUF012 - read once, never changed.
            "stock_not_negative": F("stock") >= 0,
            "cap_under_1000": F("cap").isnull() | (F("cap") < 1000),
            "status_known": F("status").in_([Status.OPEN, Status.SHUT]),
            "code_not_blank": F("code") != "",
            "stock_bounded": (F("stock") >= 0) & (F("stock") <= 1_000_000),
            "shut_ones_uncapped": (F("status") == Status.OPEN) | (F("cap") == None),  # noqa: E711
        }


metadata = MetaData(naming_convention=NAMING_CONVENTION)
items = table_for(Item, "framework_test_checked_items", metadata=metadata)
map_imperatively(Item, items)


class Items(Repository[Item, ItemId]):
    """The items."""


class ItemsUnitOfWork(UnitOfWork):
    """One transaction over the items."""

    items: Items


class SqlItems(SqlAlchemyRepository[Item, ItemId]):
    """Items in SQL."""


class SqlItemsUnitOfWork(SqlAlchemyUnitOfWork):
    """One transaction over the items, in SQL."""

    items: SqlItems

    def create_repositories(self, session: AsyncSession) -> None:
        """Bind the repository."""
        self.items = SqlItems(session)


def checks_of(table: object) -> dict[str, CheckConstraint]:
    """The table's check constraints, by name."""
    return {
        str(c.name): c
        for c in table.constraints  # type: ignore[attr-defined]
        if isinstance(c, CheckConstraint)
    }


def test_each_check_becomes_a_named_constraint() -> None:
    """``ck_<table>_<name>``, past any naming convention, the name in ``info``."""
    prefix = "ck_framework_test_checked_items_"

    assert {name: c.info for name, c in checks_of(items).items()} == {
        f"{prefix}{check}": {"declared_check": check}
        for check in (
            "stock_not_negative",
            "cap_under_1000",
            "status_known",
            "code_not_blank",
            "stock_bounded",
            "shut_ones_uncapped",
        )
    }


def test_the_sql_is_the_same_on_every_database() -> None:
    """Literal values, the stored enum values, missing values as queries treat them."""

    def written(dialect: Dialect) -> dict[str, str]:
        return {
            name.removeprefix("ck_framework_test_checked_items_"): str(
                c.sqltext.compile(
                    dialect=dialect,
                    compile_kwargs={"literal_binds": True},
                )
            )
            for name, c in checks_of(items).items()
        }

    expected = {
        "stock_not_negative": "stock >= 0",
        "cap_under_1000": "cap IS NULL OR cap IS NOT NULL AND cap < 1000",
        "status_known": "status IN ('open', 'shut')",
        "code_not_blank": "code != ''",
        "stock_bounded": "stock >= 0 AND stock <= 1000000",
        "shut_ones_uncapped": "status = 'open' OR cap IS NULL",
    }
    assert written(postgresql.dialect()) == expected  # type: ignore[no-untyped-call]
    assert written(sqlite.dialect()) == expected


async def test_a_unit_of_work_refuses_to_commit_a_broken_check() -> None:
    """In memory as in SQL: ``CheckFailed`` names the check; nothing is kept."""
    uow = in_memory(ItemsUnitOfWork)
    async with uow:
        await uow.items.add(Item(id=ItemId(1), stock=2))
        await uow.commit()

    async with uow:
        item = await uow.items.get(ItemId(1))
        item.stock = -1
        with pytest.raises(CheckFailed) as caught:
            await uow.commit()
    async with uow:
        kept = await uow.items.get(ItemId(1))

    assert (caught.value.check, caught.value.code) == (
        "stock_not_negative",
        "check_violation",
    )
    assert kept.stock == 2


async def test_a_missing_value_keeps_only_a_check_that_says_isnull() -> None:
    """``cap`` may be missing; a cap of 1000 may not."""
    uow = in_memory(ItemsUnitOfWork)
    async with uow:
        await uow.items.add(Item(id=ItemId(1), stock=0, cap=None))
        await uow.commit()
    async with uow:
        await uow.items.add(Item(id=ItemId(2), stock=0, cap=1000))
        with pytest.raises(CheckFailed, match="cap_under_1000"):
            await uow.commit()


def test_checks_that_cannot_be_declared_are_refused() -> None:
    """Not a dict of criteria, a child's field, a text pattern, an amount, a date."""

    class Line(Entity[int]):
        sku: str

    class NotADict(AggregateRoot[int]):
        stock: int

        class Meta:
            checks = (F("stock") >= 0,)

    class BadName(AggregateRoot[int]):
        stock: int

        class Meta:
            checks = {"not a name": F("stock") >= 0}  # noqa: RUF012

    class ThroughChildren(AggregateRoot[int]):
        lines: list[Line]

        class Meta:
            checks = {"sku": F("lines.sku") == "A"}  # noqa: RUF012

    class Pattern(AggregateRoot[int]):
        name: str

        class Meta:
            checks = {"named": F("name").startswith("A")}  # noqa: RUF012

    class Amount(AggregateRoot[int]):
        price: Annotated[Decimal, Digits(8, 2)]

        class Meta:
            checks = {"priced": F("price") > Decimal(0)}  # noqa: RUF012

    class Dated(AggregateRoot[int]):
        on: date

        class Meta:
            checks = {"recent": F("on") > date(2000, 1, 1)}  # noqa: RUF012

    declared: tuple[type, ...] = (NotADict, BadName)
    for cls in declared:
        with pytest.raises(TypeError, match="maps names to criteria"):
            declared_checks(cls)
    with pytest.raises(TypeError, match="a column of their own"):
        table_for(ThroughChildren, "framework_test_refused", metadata=MetaData())
    declared = (Pattern, Amount, Dated)
    for cls in declared:
        with pytest.raises(TypeError, match="check it in clean"):
            table_for(cls, "framework_test_refused", metadata=MetaData())


def test_checks_are_read_once() -> None:
    """The same bound criteria each time."""
    assert declared_checks(Item) is declared_checks(Item)


@pytest.fixture
def database(database_url: str) -> Iterator[str]:
    """An empty database, selected in the settings."""
    with override_settings(DATABASES={"default": {"URL": database_url}}):
        yield database_url


@pytest.fixture
async def tables(database: str) -> AsyncIterator[None]:
    """The items table, in the running loop's engine."""
    async with connections.engine().begin() as connection:
        await connection.run_sync(metadata.create_all)
    yield
    await connections.close_all()


async def test_the_database_holds_the_check_for_rows_written_past_the_domain(
    tables: None,
) -> None:
    """A row inserted by hand is refused, and the error names the check.

    The row breaks one check alone: of several, the database names any.
    """
    async with SqlItemsUnitOfWork() as uow:
        await uow.items.add(Item(id=ItemId(1), stock=3))
        await uow.commit()
    async with SqlItemsUnitOfWork() as uow:
        item = await uow.items.get(ItemId(1))
        item.stock = -3
        with pytest.raises(CheckFailed):
            await uow.commit()

    with pytest.raises(IntegrityError) as caught:
        async with connections.engine().begin() as connection:
            await connection.execute(
                insert(items).values(
                    id=2, version=1, stock=1, cap=5000, status="open", code="A"
                )
            )
    violation = integrity_violation(caught.value, metadata)

    assert violation is not None
    assert (violation.kind, violation.constraint) == (
        "check",
        "ck_framework_test_checked_items_cap_under_1000",
    )


async def test_makemigrations_adds_a_check_declared_on_a_table_that_exists(
    tmp_path: Path, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Alembic compares no checks; the revision adds the one declared later."""

    class Before(AggregateRoot[int]):
        stock: int

    class After(AggregateRoot[int]):
        stock: int

        class Meta:
            checks = {"stock_not_negative": F("stock") >= 0}  # noqa: RUF012

    name = "framework_test_checked_later"
    schema = MetaData(naming_convention=NAMING_CONVENTION)
    first = table_for(Before, name, metadata=schema)
    monkeypatch.setattr(migrations_module, "metadata", schema)
    with override_settings(
        MIGRATIONS_DIR=tmp_path / "migrations",
        DATABASES={"default": {"URL": database_url}},
    ):
        call_command("makemigrations", "-m", "stock", stdout=io.StringIO())
        call_command("migrate", stdout=io.StringIO())
        schema.remove(first)
        table_for(After, name, metadata=schema)
        call_command("makemigrations", "-m", "checked", stdout=io.StringIO())
        call_command("migrate", stdout=io.StringIO())

        revisions = "".join(
            path.read_text() for path in (tmp_path / "migrations").rglob("*.py")
        )
        with pytest.raises(IntegrityError):
            async with connections.engine().begin() as connection:
                await connection.execute(
                    insert(schema.tables[name]).values(id=1, version=1, stock=-1)
                )
        await connections.close_all()

    assert "ck_framework_test_checked_later_stock_not_negative" in revisions
