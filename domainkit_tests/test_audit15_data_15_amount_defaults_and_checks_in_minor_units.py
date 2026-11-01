"""DATA-15: on SQLite, SQL written around an amount column sees minor units.

``ExactDecimal`` stores whole minor units on SQLite (``domainkit.db.types``);
only the expressions its comparator builds are scaled. A server default
or a ``CHECK`` written as SQL, the usual way to declare them and the way
``makemigrations`` copies them into a revision, is not: ``server_default
= text("1.50")`` on an ``ExactDecimal(12, 2)`` stores 1.5 minor units and
reads back as 0.015, and ``CHECK (amount <= 100)`` caps the column at
1.00 instead of 100.00. PostgreSQL gives 1.50 and 100.00. Neither is in
the docs' list of what to keep in Python ("Exact amounts" in
``docs/databases.md``), which names only casts to text.
"""

from collections.abc import Iterator
from decimal import Decimal

import pytest
from sqlalchemy import CheckConstraint, Column, Integer, MetaData, Table, insert, select
from sqlalchemy import text as sql
from sqlalchemy.exc import IntegrityError

from domainkit.conf import override_settings
from domainkit.db import ExactDecimal, connections

pytestmark = pytest.mark.anyio

schema = MetaData()
fees = Table(
    "framework_test_audit_fees",
    schema,
    Column("id", Integer, primary_key=True),
    Column("fee", ExactDecimal(12, 2), nullable=False, server_default=sql("1.50")),
    Column("cap", ExactDecimal(12, 2), nullable=True),
    CheckConstraint("cap <= 100", name="cap_at_most_100"),
)


@pytest.fixture
def tables(database_url: str) -> Iterator[None]:
    """The fees table, in a database of its own."""
    with override_settings(DATABASES={"default": {"URL": database_url}}):
        yield


async def test_a_server_default_reads_back_as_declared(tables: None) -> None:
    """A fee left to its default of 1.50 is 1.50, not 0.015."""
    try:
        async with connections.engine().begin() as connection:
            await connection.run_sync(schema.create_all)
            await connection.execute(insert(fees).values(id=1))
            fee = (await connection.execute(select(fees.c.fee))).scalar_one()
            await connection.run_sync(schema.drop_all)
    finally:
        await connections.close_all()

    assert fee == Decimal("1.50")


async def test_a_check_compares_the_amount_not_its_minor_units(tables: None) -> None:
    """``cap <= 100`` takes 5.00."""
    try:
        async with connections.engine().begin() as connection:
            await connection.run_sync(schema.create_all)
        try:
            async with connections.engine().begin() as connection:
                await connection.execute(
                    insert(fees).values(id=2, fee=Decimal("0"), cap=Decimal("5.00"))
                )
        except IntegrityError:
            pytest.fail("CHECK (cap <= 100) refused an amount of 5.00")
        finally:
            async with connections.engine().begin() as connection:
                await connection.run_sync(schema.drop_all)
    finally:
        await connections.close_all()
