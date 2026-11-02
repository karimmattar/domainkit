"""DATA-14: ``dumpdata`` reads each table in a snapshot of its own.

``domainkit.db.fixtures.dump`` reads the tables parents first, one
``SELECT`` each, on a connection with no transaction around them (READ
COMMITTED on PostgreSQL, no ``BEGIN`` on SQLite). A parent and its child
committed while the dump runs land in the dump as the child alone: the
fixture holds a row whose parent it lacks, which ``loaddata`` refuses on
PostgreSQL and loads as an orphan on SQLite. The same tear splits a
ledger's entries from their postings, or an account's balance from them.
The dump should read every table in one snapshot.
"""

import asyncio
import threading
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Column, ForeignKey, Integer, MetaData, Table, insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import create_async_engine

from domainkit.conf import override_settings
from domainkit.db import fixtures

pytestmark = pytest.mark.anyio

schema = MetaData()
parents = Table(
    "framework_test_dump_parents", schema, Column("id", Integer, primary_key=True)
)
children = Table(
    "framework_test_dump_children",
    schema,
    Column("id", Integer, primary_key=True),
    Column("parent_id", ForeignKey(parents.c.id), nullable=False),
)


@pytest.fixture
def database(database_url: str) -> Iterator[str]:
    """An empty database, selected in the settings."""
    with override_settings(DATABASES={"default": {"URL": database_url}}):
        yield database_url


async def test_a_dump_never_holds_a_child_without_its_parent(
    database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parent and child committed mid-dump are both in it, or neither is."""
    writer = create_async_engine(
        database, connect_args={"timeout": 0.2} if "sqlite" in database else {}
    )
    async with writer.begin() as connection:
        await connection.run_sync(schema.create_all)
        await connection.execute(insert(parents), [{"id": 1}])
        await connection.execute(insert(children), [{"id": 1, "parent_id": 1}])

    encoded = fixtures._encoded_row
    written: list[bool] = []

    def encode_then_commit_a_pair(table: Table, row: Any) -> dict[str, Any]:  # noqa: ANN401
        """After the parents are read, another request commits a parent and child."""
        if table is parents and not written:
            written.append(True)

            def commit() -> None:
                async def run() -> None:
                    engine = create_async_engine(
                        database,
                        connect_args={"timeout": 0.2} if "sqlite" in database else {},
                    )
                    try:
                        async with engine.begin() as connection:
                            await connection.execute(insert(parents), [{"id": 2}])
                            await connection.execute(
                                insert(children), [{"id": 2, "parent_id": 2}]
                            )
                    except OperationalError:
                        pass  # The dump's snapshot keeps the writer out: fine too.
                    finally:
                        await engine.dispose()

                asyncio.run(run())

            thread = threading.Thread(target=commit)
            thread.start()
            thread.join()
        return encoded(table, row)

    monkeypatch.setattr(fixtures, "_encoded_row", encode_then_commit_a_pair)
    try:
        document = await fixtures.dump("default", [parents, children])
    finally:
        async with writer.begin() as connection:
            await connection.run_sync(schema.drop_all)
        await writer.dispose()

    rows = {entry["table"]: entry["rows"] for entry in document["tables"]}
    dumped_parents = {row["id"] for row in rows[parents.name]}
    orphans = [
        row for row in rows[children.name] if row["parent_id"] not in dumped_parents
    ]
    assert orphans == [], f"the dump holds children without their parents: {orphans}"
