"""DATA-18: on SQLite, no foreign key is ever enforced.

SQLite checks foreign keys only on a connection that ran ``PRAGMA
foreign_keys = ON``; ``domainkit.db.connections.create_engine`` never
runs it, so every ``ForeignKey`` a project declares, the children
``children()`` maps and the ``ON DELETE CASCADE`` ``delete_rows`` relies on
included, is decoration on the development and test database. A child
row naming no parent, or a parent deleted under its children, commits on
SQLite and is refused (or cascades) on PostgreSQL: a project's suite on
the SQLite settings ``startproject`` writes passes code that fails in
production, and ``delete_rows`` leaves orphans there. ``docs/databases.md``
notes the pragma, but nothing turns it on.
"""

from collections.abc import Iterator

import pytest
from sqlalchemy import Column, ForeignKey, Integer, MetaData, Table, delete, insert
from sqlalchemy.exc import IntegrityError

from domainkit.conf import override_settings
from domainkit.db import connections

pytestmark = pytest.mark.anyio

schema = MetaData()
orders = Table(
    "framework_test_fk_orders", schema, Column("id", Integer, primary_key=True)
)
lines = Table(
    "framework_test_fk_lines",
    schema,
    Column("id", Integer, primary_key=True),
    Column("order_id", ForeignKey(orders.c.id, ondelete="CASCADE"), nullable=False),
)


@pytest.fixture
def database(database_url: str) -> Iterator[str]:
    """An empty database, selected in the settings."""
    with override_settings(DATABASES={"default": {"URL": database_url}}):
        yield database_url


async def test_a_child_without_its_parent_is_refused(database: str) -> None:
    """A line of an order that does not exist fails to commit, as on PostgreSQL."""
    try:
        async with connections.engine().begin() as connection:
            await connection.run_sync(schema.create_all)
        with pytest.raises(IntegrityError):
            async with connections.session() as session, session.begin():
                await session.execute(insert(lines).values(id=1, order_id=404))
    finally:
        async with connections.engine().begin() as connection:
            await connection.run_sync(schema.drop_all)
        await connections.close_all()


async def test_deleting_a_parent_cascades_to_its_children(database: str) -> None:
    """``ON DELETE CASCADE`` takes the lines with their order."""
    try:
        async with connections.engine().begin() as connection:
            await connection.run_sync(schema.create_all)
        async with connections.session() as session, session.begin():
            await session.execute(insert(orders).values(id=1))
            await session.execute(insert(lines).values(id=1, order_id=1))
        async with connections.session() as session, session.begin():
            await session.execute(delete(orders))
        async with connections.session() as session:
            left = (await session.execute(lines.select())).all()
        assert left == []
    finally:
        async with connections.engine().begin() as connection:
            await connection.run_sync(schema.drop_all)
        await connections.close_all()
