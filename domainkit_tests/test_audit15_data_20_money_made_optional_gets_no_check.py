"""DATA-20: a money field made optional gets no both-or-neither check.

SEC-14a-3 made ``makemigrations`` write a nullable money pair's check
(``money_check``) when it adds the pair to a table that exists; Alembic
compares no checks, so ``domainkit.db.migrations._money_checks`` adds it,
but only when the table lacks the field's amount column. A field that
was ``Money`` and becomes ``Money | None`` keeps its columns: the revision
makes both nullable and adds no check, so the database takes an amount
without its currency, a row every read then refuses (``IncompleteMoney``).
"""

import io
from pathlib import Path

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, insert, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from domainkit.conf import override_settings
from domainkit.db import MoneyColumns
from domainkit.db import migrations as migrations_module
from domainkit.db.orm import NAMING_CONVENTION
from domainkit.management import call_command

pytestmark = pytest.mark.anyio
NAME = "framework_test_optional_prices"


@pytest.fixture
def migrated(tmp_path: Path, database_url: str, monkeypatch: pytest.MonkeyPatch) -> str:
    """A price that was required, made optional, and both revisions applied."""
    schema = MetaData(naming_convention=NAMING_CONVENTION)
    required = Table(
        NAME, schema, Column("id", Integer, primary_key=True), *MoneyColumns("price")
    )
    monkeypatch.setattr(migrations_module, "metadata", schema)
    with override_settings(
        MIGRATIONS_DIR=tmp_path / "migrations",
        DATABASES={"default": {"URL": database_url}},
    ):
        call_command("makemigrations", "-m", "prices", stdout=io.StringIO())
        call_command("migrate", stdout=io.StringIO())
        schema.remove(required)
        Table(
            NAME,
            schema,
            Column("id", Integer, primary_key=True),
            *MoneyColumns("price", nullable=True),
        )
        call_command("makemigrations", "-m", "optional price", stdout=io.StringIO())
        call_command("migrate", stdout=io.StringIO())
    return database_url


async def test_half_a_pair_is_refused_once_the_field_is_optional(migrated: str) -> None:
    """An amount without its currency fails the pair's check."""
    engine = create_async_engine(migrated)
    table = Table(
        NAME,
        MetaData(),
        Column("id", Integer, primary_key=True),
        *MoneyColumns("price", nullable=True),
    )
    try:
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    insert(table).values(id=1, price=text("1050"), price_currency=None)
                )
    finally:
        await engine.dispose()
