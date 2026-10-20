"""Property tests: the in-memory repository and the database answer alike.

Hypothesis makes gadgets (ties, missing values, pattern characters, child
entities, soft-deleted ones) and queries (nested criteria on every kind of
field, orderings, page sizes); the fake's answer must be the database's,
row for row and page for page, on SQLite and on PostgreSQL.
"""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import delete

from domainkit.conf import override_settings
from domainkit.db import connections
from domainkit.ddd.query import TEXT_LOOKUPS, Criteria, F, Ordering, Query
from domainkit_tests.gadgets import (
    MADE,
    Color,
    Gadget,
    GadgetsUnitOfWork,
    InMemoryGadgets,
    Size,
    make_gadget,
    make_maker,
    make_part,
    makers,
    metadata,
    parts,
)
from domainkit_tests.gadgets import gadgets as gadget_table

LATER = datetime(2026, 2, 1, tzinfo=UTC)
TEXT = st.text(alphabet="aAbB%_/\\ 中€", max_size=3)
"""Letters with case only in ASCII: lower() folds no others on SQLite, nor
under PostgreSQL's C collation, which the text columns use to order as
Python does (see docs/databases.md)."""
PRICES = st.decimals(min_value=-20, max_value=20, places=2)
QUERY_PRICES = st.decimals(min_value=-21, max_value=21, places=3)
STOCK = st.one_of(st.none(), st.integers(-2, 3))
MOMENTS = st.sampled_from([MADE, LATER, LATER + timedelta(microseconds=1)])
COMPARISONS = st.sampled_from(["exact", "gt", "gte", "lt", "lte"])
ORDERABLE = ("name", "label", "price", "stock", "active", "color", "size", "made_at")


def build(
    name: str,
    price: Decimal,
    stock: int | None,
    active: bool,
    color: Color,
    size: Size,
    made_at: datetime,
    skus: list[tuple[str, int]],
    maker: str | None,
    deleted: bool,
) -> Gadget:
    """A gadget from generated values."""
    gadget = make_gadget(
        name,
        price=str(price),
        stock=stock,
        active=active,
        color=color,
        size=size,
        made_at=made_at,
        parts=[make_part(sku, weight) for sku, weight in skus],
        maker=None if maker is None else make_maker(maker),
    )
    if deleted:
        gadget.mark_deleted(MADE)
    return gadget


gadget_values = st.builds(
    build,
    TEXT,
    PRICES,
    STOCK,
    st.booleans(),
    st.sampled_from(Color),
    st.sampled_from(Size),
    MOMENTS,
    st.lists(st.tuples(TEXT, st.integers(0, 3)), max_size=2),
    st.one_of(st.none(), TEXT),
    st.booleans(),
)
leaves = st.one_of(
    st.builds(F("name").lookup, st.sampled_from(sorted(TEXT_LOOKUPS)), TEXT),
    st.builds(F("name").__eq__, TEXT),
    st.builds(F("label").lookup, st.sampled_from(sorted(TEXT_LOOKUPS)), TEXT),
    st.builds(F("price").lookup, COMPARISONS, QUERY_PRICES),
    st.builds(F("price").range, QUERY_PRICES, QUERY_PRICES),
    st.builds(F("price").in_, st.lists(QUERY_PRICES, max_size=3)),
    st.builds(F("stock").lookup, COMPARISONS, st.integers(-2, 3)),
    st.builds(F("stock").__eq__, STOCK),
    st.builds(F("stock").isnull, st.booleans()),
    st.builds(F("active").__eq__, st.booleans()),
    st.builds(F("color").lookup, COMPARISONS, st.sampled_from(Color)),
    st.builds(F("color").in_, st.lists(st.sampled_from(Color), max_size=2)),
    st.builds(F("size").lookup, COMPARISONS, st.sampled_from(Size)),
    st.builds(F("made_at").lookup, COMPARISONS, MOMENTS),
    st.builds(F("parts.sku").lookup, st.sampled_from(["exact", "icontains"]), TEXT),
    st.builds(F("parts.weight").lookup, COMPARISONS, st.integers(0, 3)),
    st.builds(F("maker.name").lookup, st.sampled_from(["iexact", "endswith"]), TEXT),
    st.builds(F("maker.name").isnull, st.booleans()),
)
criteria_trees: st.SearchStrategy[Criteria] = st.recursive(
    leaves,
    lambda inner: st.one_of(
        st.builds(lambda a, b: a & b, inner, inner),
        st.builds(lambda a, b: a | b, inner, inner),
        st.builds(lambda a: ~a, inner),
    ),
    max_leaves=5,
)
orderings = st.lists(
    st.builds(Ordering, st.sampled_from(ORDERABLE), st.booleans()),
    max_size=3,
    unique_by=lambda each: each.field,
)


@pytest.fixture
def loop(database_url: str) -> Iterator[asyncio.Runner]:
    """One event loop for every example, on an empty database with the tables.

    Engines belong to their loop, so every example shares this one's.
    """
    with (
        override_settings(DATABASES={"default": {"URL": database_url}}),
        asyncio.Runner() as runner,
    ):
        runner.run(create_tables())
        yield runner
        runner.run(connections.close_all())


async def create_tables() -> None:
    """The gadget tables, in the running loop's engine."""
    async with connections.engine().begin() as connection:
        await connection.run_sync(metadata.create_all)


async def replace_rows(items: list[Gadget]) -> None:
    """Empty the tables, then store ``items``."""
    async with connections.engine().begin() as connection:
        for table in (parts, makers, gadget_table):
            await connection.execute(delete(table))
    async with GadgetsUnitOfWork() as uow:
        await uow.gadgets.add_all(items)
        await uow.commit()


EXAMPLES: Final = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)


@EXAMPLES
@given(
    items=st.lists(gadget_values, max_size=8),
    criteria=st.one_of(st.none(), criteria_trees),
    ordering=orderings,
    include_deleted=st.booleans(),
)
def test_queries_select_alike(
    loop: asyncio.Runner,
    items: list[Gadget],
    criteria: Criteria | None,
    ordering: list[Ordering],
    include_deleted: bool,
) -> None:
    """Same rows, same order, same count; and each row meets the criteria in Python."""
    query = Query(criteria, ordering=ordering, include_deleted=include_deleted)
    found = loop.run(select_alike(items, query))
    live = [each for each in items if include_deleted or not each.is_deleted]
    matched = {each.id for each in live if criteria is None or criteria.matches(each)}
    assert matched == set(found)


async def select_alike(items: list[Gadget], query: Query) -> list[object]:
    """The ids the database selects, checked against the fake's."""
    await replace_rows(items)
    fake = InMemoryGadgets(items)
    criteria, deleted = query.criteria, query.include_deleted
    async with GadgetsUnitOfWork(readonly=True) as uow:
        found: list[object] = [each.id for each in await uow.gadgets.filter(query)]
        assert found == [each.id for each in await fake.filter(query)]
        counted = await uow.gadgets.count(criteria, include_deleted=deleted)
        assert counted == len(found)
        exists = await uow.gadgets.exists(criteria, include_deleted=deleted)
        assert exists == (counted > 0)
    return found


@EXAMPLES
@given(
    items=st.lists(gadget_values, max_size=8),
    criteria=st.one_of(st.none(), criteria_trees),
    ordering=orderings,
    limit=st.integers(1, 4),
)
def test_pages_follow_alike(
    loop: asyncio.Runner,
    items: list[Gadget],
    criteria: Criteria | None,
    ordering: list[Ordering],
    limit: int,
) -> None:
    """Keyset pages from either repository match, and cover everything once."""
    loop.run(page_alike(items, Query(criteria, ordering=ordering), limit))


async def page_alike(items: list[Gadget], query: Query, limit: int) -> None:
    """Page through both repositories at once."""
    await replace_rows(items)
    fake = InMemoryGadgets(items)
    seen: list[object] = []
    after = None
    async with GadgetsUnitOfWork(readonly=True) as uow:
        everything = [each.id for each in await uow.gadgets.filter(query)]
        while True:
            page = await uow.gadgets.paginate(query, limit=limit, after=after)
            expected = await fake.paginate(query, limit=limit, after=after)
            assert [each.id for each in page.items] == [
                each.id for each in expected.items
            ]
            assert page.next_position == expected.next_position
            seen += [each.id for each in page.items]
            if not page.has_more:
                break
            after = page.next_position
    assert seen == everything
