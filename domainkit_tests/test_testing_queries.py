"""``assert_num_queries`` and ``assert_max_queries``: SQL statements counted."""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, event, insert, text
from sqlalchemy.engine import Engine

from domainkit.conf import override_settings
from domainkit.db import connections
from domainkit.testing import CapturedQueries, assert_max_queries, assert_num_queries
from domainkit.testing import queries as queries_module
from domainkit.testing.queries import EVENT
from domainkit_tests.conftest import MakeDatabase

pytestmark = pytest.mark.anyio

schema = MetaData()
numbers = Table("framework_test_numbers", schema, Column("n", Integer))


@pytest.fixture
async def databases(make_database: MakeDatabase) -> AsyncIterator[None]:
    """Two empty databases, ``default`` and ``other``, with a table each."""
    with override_settings(
        DATABASES={
            "default": {"URL": make_database("default")},
            "other": {"URL": make_database("other")},
        }
    ):
        for alias in ("default", "other"):
            async with connections.engine(alias).begin() as connection:
                await connection.run_sync(schema.create_all)
        yield
        await connections.close_all()


async def select(*values: int, alias: str = "default") -> None:
    """Run ``SELECT <value>`` for each value, one statement each."""
    async with connections.engine(alias).connect() as connection:
        for value in values:
            await connection.execute(text(f"SELECT {value}"))


async def test_every_statement_in_the_block_counts(databases: None) -> None:
    """Statements are kept in order; an ``executemany`` counts once."""
    with assert_num_queries(3) as queries:
        await select(1, 2)
        async with connections.engine().begin() as connection:
            await connection.execute(insert(numbers), [{"n": 1}, {"n": 2}])
    await select(3)

    assert queries.statements[:2] == ["SELECT 1", "SELECT 2"]
    assert queries.statements[2].startswith("INSERT INTO framework_test_numbers")
    assert len(queries) == 3
    assert queries not in queries_module._capturing


async def test_blocks_never_change_sqlalchemys_listeners(databases: None) -> None:
    """One listener, put on ``Engine`` on import, serves every block, nested or not.

    Other threads read SQLAlchemy's listener lists as they run statements;
    a block that added or removed a listener, the first one included,
    would fail theirs.
    """
    engine = connections.engine().sync_engine
    listeners = list(engine.dispatch.before_cursor_execute)
    assert event.contains(Engine, EVENT, queries_module._dispatch)

    with assert_num_queries(2) as outer:
        await select(1)
        with assert_num_queries(1) as inner:
            await select(2)
            assert list(engine.dispatch.before_cursor_execute) == listeners
        assert queries_module._capturing == (outer,)

    assert list(engine.dispatch.before_cursor_execute) == listeners
    assert event.contains(Engine, EVENT, queries_module._dispatch)
    assert (outer.statements, inner.statements) == (
        ["SELECT 1", "SELECT 2"],
        ["SELECT 2"],
    )
    assert not queries_module._capturing


async def test_a_wrong_count_fails_and_lists_the_statements(databases: None) -> None:
    """The message says what was expected, and lists what ran."""
    with pytest.raises(AssertionError) as failure, assert_num_queries(1):
        await select(1, 2)
    assert str(failure.value) == "Expected 1 query, 2 ran:\n1. SELECT 1\n2. SELECT 2"

    with pytest.raises(AssertionError) as failure, assert_num_queries(2):
        pass
    assert str(failure.value) == "Expected 2 queries, 0 ran."

    with pytest.raises(AssertionError) as failure, assert_num_queries(2):
        await select(1, 2, 3)
    assert str(failure.value).startswith("Expected 2 queries, 3 ran:\n")


async def test_at_most_allows_fewer(databases: None) -> None:
    """``assert_max_queries`` takes up to its limit, and no more."""
    with assert_max_queries(2):
        await select(1)
    with assert_max_queries(2):
        await select(1, 2)
    with pytest.raises(AssertionError) as failure, assert_max_queries(2):
        await select(1, 2, 3)
    assert str(failure.value).startswith("Expected at most 2 queries, 3 ran:\n")


async def test_using_counts_one_database(databases: None) -> None:
    """``using`` counts the statements on that alias only."""
    with assert_num_queries(1, using="other") as queries:
        await select(1)
        await select(2, alias="other")
    assert queries.statements == ["SELECT 2"]

    with pytest.raises(AssertionError) as failure, assert_max_queries(0, using="other"):
        await select(3, alias="other")
    assert (
        str(failure.value)
        == "Expected at most 0 queries on 'other', 1 ran:\n1. SELECT 3"
    )


async def test_errors_in_the_block_are_not_hidden(databases: None) -> None:
    """A block that raises fails with its own error, not with a count."""

    async def failing() -> None:
        await select(1)
        raise LookupError

    with pytest.raises(LookupError), assert_num_queries(5):
        await failing()
    with pytest.raises(LookupError), CapturedQueries():
        await failing()


async def test_bound_values_are_never_kept(databases: None) -> None:
    """Statements keep their placeholders; what was bound stays out."""
    with assert_num_queries(0, using="other") as nothing, CapturedQueries() as queries:
        async with connections.engine().connect() as connection:
            await connection.execute(text("SELECT :secret"), {"secret": "hunter2"})

    assert len(queries) == 1
    assert "hunter2" not in queries.report()
    assert nothing.statements == []
    assert queries.holds()
