"""``affected_rows``: the rows a change counted, without a type checker's waiver (E4).

``AsyncSession.execute`` is typed to return any ``Result``, so the stores
read ``result.rowcount`` under ``# type: ignore[attr-defined]``. The
helper says what they meant, and refuses a result that counts nothing.
"""

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, delete, update
from sqlalchemy.engine import IteratorResult
from sqlalchemy.engine.result import SimpleResultMetaData
from sqlalchemy.ext.asyncio import AsyncSession

from domainkit.conf import override_settings
from domainkit.db import affected_rows, connections
from domainkit_tests.conftest import MakeDatabase

pytestmark = pytest.mark.anyio
counters = Table(
    "affected_counters", MetaData(), Column("id", Integer, primary_key=True)
)


async def test_a_change_says_how_many_rows_it_changed(
    make_database: MakeDatabase,
) -> None:
    """An ``UPDATE`` or ``DELETE`` through a session; none matched is 0."""
    with override_settings(DATABASES={"default": {"URL": make_database("rows")}}):
        async with connections.engine().begin() as connection:
            await connection.run_sync(counters.metadata.create_all)
            await connection.execute(counters.insert(), [{"id": n} for n in (1, 2, 3)])
        async with AsyncSession(connections.engine()) as session:
            moved = await session.execute(
                update(counters).where(counters.c.id > 1).values(id=counters.c.id + 10)
            )
            none = await session.execute(delete(counters).where(counters.c.id == 99))
            gone = await session.execute(delete(counters))
            await session.rollback()
        await connections.close_all()

    assert (affected_rows(moved), affected_rows(none), affected_rows(gone)) == (2, 0, 3)
    with pytest.raises(TypeError, match="IteratorResult counts no changed rows"):
        affected_rows(IteratorResult(SimpleResultMetaData(["id"]), iter([])))
