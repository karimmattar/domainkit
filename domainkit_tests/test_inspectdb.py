"""``inspectdb``: aggregates to start from, read from an existing database."""

import ast
import asyncio
import io
import os
from collections.abc import Iterator

import pytest
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    Integer,
    Interval,
    LargeBinary,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    Time,
    Uuid,
)
from sqlalchemy.types import NullType

from domainkit.conf import override_settings
from domainkit.db import connections
from domainkit.management import call_command
from domainkit.management.base import CommandError
from domainkit.management.commands.inspectdb import render
from domainkit_tests.conftest import POSTGRES_VARIABLE

ON_POSTGRES = bool(os.environ.get(POSTGRES_VARIABLE))
legacy = MetaData()
Table(
    "shop_orders",
    legacy,
    Column("id", Uuid, primary_key=True),
    Column("number", String(20), nullable=False),
    Column("total", Numeric(12, 2), nullable=False),
    Column("note", Text),
    Column("placed_at", DateTime(timezone=True), nullable=False),
    Column("paid", Boolean, nullable=False),
    Column("version", Integer, nullable=False),
)
Table(
    "order_links",
    legacy,
    Column("order_id", Integer, primary_key=True),
    Column("other_id", Integer, primary_key=True),
)


@pytest.fixture
def database(database_url: str) -> Iterator[str]:
    """A database holding the legacy tables."""
    with override_settings(DATABASES={"default": {"URL": database_url}}):

        async def create() -> None:
            async with connections.engine().begin() as connection:
                await connection.run_sync(legacy.create_all)
            await connections.close_all()

        asyncio.run(create())
        yield database_url


def run(*args: str) -> str:
    """Run ``inspectdb`` and return what it printed."""
    output = io.StringIO()
    call_command("inspectdb", *args, stdout=output)
    return output.getvalue()


def test_each_table_becomes_an_aggregate_with_its_mapping(database: str) -> None:
    """Hints from the column types; required fields first; valid Python."""
    module = run("shop_orders")

    ast.parse(module)
    assert "from decimal import Decimal" in module
    assert "from domainkit.ddd import AggregateRoot, Digits, MaxLength" in module
    # PostgreSQL keeps a UUID type; SQLite stores it as 32 characters.
    assert ("class ShopOrder(AggregateRoot[UUID]):" in module) is ON_POSTGRES
    assert "    number: Annotated[str, MaxLength(20)]" in module
    assert "    total: Annotated[Decimal, Digits(12, 2)]" in module
    assert module.index("    paid: bool") < module.index("    note: str | None = None")
    assert "version:" not in module
    assert 'shop_orders = table_for(ShopOrder, "shop_orders")' in module


def test_a_table_without_one_id_says_what_it_needs(database: str) -> None:
    """A composite key is named; the module still parses."""
    module = run()

    ast.parse(module)
    assert "class OrderLink(AggregateRoot[int]):" in module
    assert "the primary key is ['order_id', 'other_id']" in module


def test_a_missing_table_is_refused(database: str) -> None:
    """Named tables must exist."""
    with pytest.raises(CommandError, match="No table named \\['nowhere'\\]"):
        run("nowhere")


def test_every_column_type_has_a_hint() -> None:
    """Without a database: the hints, and a marked guess for unknown types."""
    table = Table(
        "1st kind",
        MetaData(),
        Column("id", BigInteger, primary_key=True),
        Column("ratio", Float, nullable=False),
        Column("born", Date, nullable=False),
        Column("opens", Time, nullable=False),
        Column("lasts", Interval, nullable=False),
        Column("data", JSON, nullable=False),
        Column("blob", LargeBinary, nullable=False),
        Column("amount", Numeric, nullable=False),
        Column("label", String, nullable=False),
        Column("odd", NullType, nullable=False),
        Column("two words", Integer),
    )
    module = render([table])

    ast.parse(module)
    for line in (
        "class T1stKind(AggregateRoot[int]):",
        "    ratio: float",
        "    born: date",
        "    opens: time",
        "    lasts: timedelta",
        "    data: dict[str, Any]",
        "    blob: bytes",
        "    amount: Decimal",
        "    label: str",
        "    odd: Any  # inspectdb: NullType() has no known hint",
        "    two_words: int | None = None",
        't_1st_kind = table_for(T1stKind, "1st kind")',
    ):
        assert line in module
