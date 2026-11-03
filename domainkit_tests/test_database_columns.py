"""``DatabaseDefault`` and ``Generated``: columns the database fills."""

import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, NewType
from uuid import UUID, uuid4

import pytest
from sqlalchemy import MetaData, insert
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.schema import CreateTable

from domainkit.conf import override_settings
from domainkit.db import (
    SqlAlchemyRepository,
    SqlAlchemyUnitOfWork,
    connections,
    map_imperatively,
    table_for,
)
from domainkit.ddd import AggregateRoot, DatabaseDefault, Generated, MaxLength

pytestmark = pytest.mark.anyio

LineId = NewType("LineId", UUID)


class Line(AggregateRoot[LineId]):
    """An order line: its total the database works out, its state it defaults."""

    price_cents: int
    quantity: int
    total_cents: Annotated[int, Generated("price_cents * quantity")] = 0
    state: Annotated[str, MaxLength(10), DatabaseDefault("'open'")] = "open"


metadata = MetaData()
lines = table_for(Line, "framework_test_lines", metadata=metadata)
map_imperatively(Line, lines)


class Lines(SqlAlchemyRepository[Line, LineId]):
    """Lines in SQL."""


class LinesUnitOfWork(SqlAlchemyUnitOfWork):
    """One transaction over the lines."""

    lines: Lines

    def create_repositories(self, session: AsyncSession) -> None:
        """Bind the repository."""
        self.lines = Lines(session)


def test_the_columns_say_what_the_database_does() -> None:
    """A server default, and a stored generated column, on either database."""
    for dialect in (sqlite.dialect(), postgresql.dialect()):  # type: ignore[no-untyped-call]
        ddl = str(CreateTable(lines).compile(dialect=dialect))
        assert "GENERATED ALWAYS AS (price_cents * quantity) STORED" in ddl
        assert "state VARCHAR(10) DEFAULT 'open' NOT NULL" in ddl


@pytest.fixture(params=["sqlite", "postgresql"])
async def database(
    request: pytest.FixtureRequest, database_access: None, tmp_path: Path
) -> AsyncIterator[None]:
    """The lines table, on SQLite or (where it is set) PostgreSQL."""
    url = (
        f"sqlite+aiosqlite:///{tmp_path / 'lines.sqlite3'}"
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


async def test_the_database_fills_what_the_aggregate_leaves(database: None) -> None:
    """The total is computed on write and read back; a row written bare is open."""
    line = Line(id=LineId(uuid4()), price_cents=250, quantity=3)
    async with LinesUnitOfWork() as uow:
        await uow.lines.add(line)
        await uow.commit()
    bare = uuid4()
    async with connections.engine().begin() as connection:
        await connection.execute(
            insert(lines).values(id=bare, version=1, price_cents=100, quantity=2)
        )

    async with LinesUnitOfWork() as uow:
        stored = await uow.lines.get(line.id)
        other = await uow.lines.get(LineId(bare))
        first = stored.total_cents
        stored.quantity = 4
        await uow.commit()
        # Read back as it was written (RETURNING): no load after the commit.
        updated = stored.total_cents
    async with LinesUnitOfWork() as uow:
        changed = await uow.lines.get(line.id)

    assert line.total_cents == 750  # Set on the aggregate as it was added.
    assert (first, other.total_cents, other.state) == (750, 200, "open")
    assert updated == changed.total_cents == 1000
