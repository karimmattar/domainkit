"""Queries compiled to SQL answer as the in-memory repository does."""

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, NewType
from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    select,
)
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import column_property

from domainkit.conf import override_settings
from domainkit.db import SqlAlchemyRepository, connections, map_imperatively
from domainkit.db.query import compile_criteria, compile_ordering, mapper_of
from domainkit.ddd import (
    AggregateRoot,
    DuplicateValue,
    Entity,
    EventBus,
    InvalidQuery,
    LockNotAvailable,
    ReadOnlyError,
    UnknownField,
)
from domainkit.ddd.query import Criteria, F, Ordering, RowLock, Selection
from domainkit.http.errors import install_exception_handlers
from domainkit.http.filters import Filter, FilterSet
from domainkit.testing import AsyncTestClient, assert_num_queries
from domainkit_tests.conftest import POSTGRES_VARIABLE
from domainkit_tests.gadgets import (
    MADE,
    Color,
    Gadget,
    GadgetMade,
    GadgetsUnitOfWork,
    InMemoryGadgets,
    Label,
    Size,
    gadgets,
    make_gadget,
    make_maker,
    make_part,
    metadata,
    parts,
)

pytestmark = pytest.mark.anyio
LATER = datetime(2026, 2, 1, tzinfo=UTC)
ON_POSTGRES = bool(os.environ.get(POSTGRES_VARIABLE))
postgres_only = pytest.mark.skipif(not ON_POSTGRES, reason=f"set {POSTGRES_VARIABLE}")
SQLITE = create_engine("sqlite://").dialect
POSTGRES = create_async_engine("postgresql+asyncpg://app@db/app").dialect


@pytest.fixture
def database(database_url: str) -> Iterator[str]:
    """An empty database, selected in the settings."""
    with override_settings(DATABASES={"default": {"URL": database_url}}):
        yield database_url


@pytest.fixture
async def tables(database: str) -> AsyncIterator[None]:
    """The gadget tables, in the running loop's engine."""
    async with connections.engine().begin() as connection:
        await connection.run_sync(metadata.create_all)
    yield
    await connections.close_all()


def dataset() -> list[Gadget]:
    """Gadgets with pattern characters, ties, missing values, and children."""
    return [
        make_gadget(
            "a%b",
            price="1.50",
            color=Color.RED,
            parts=[make_part("X1", 1)],
            maker=make_maker("Acme"),
        ),
        make_gadget(
            "A_c",
            price="2.00",
            stock=3,
            color=Color.BLUE,
            size=Size.LARGE,
            active=False,
            parts=[make_part("X2", 5), make_part("Y", 2)],
        ),
        make_gadget("b/c", price="-1.25", stock=0, color=Color.GREEN, made_at=LATER),
        make_gadget("é中\\", price="10.00", stock=3, maker=make_maker("bolt")),
        make_gadget("", price="2.00", stock=None, size=Size.LARGE, made_at=LATER),
    ]


async def store(*items: Gadget) -> None:
    """Commit ``items`` in one unit of work."""
    async with GadgetsUnitOfWork() as uow:
        await uow.gadgets.add_all(items)
        await uow.commit()


CASES: list[tuple[Criteria | None, tuple[str, ...]]] = [
    (None, ()),
    (F("name") == "A_c", ("-price",)),
    (F("name") != "A_c", ("name",)),
    (F("name").iexact("a_C"), ()),
    (F("name").contains("%"), ()),
    (F("name").contains("_"), ()),
    (F("name").contains("/"), ()),
    (F("name").contains("\\"), ()),
    (F("name").contains(""), ("name",)),
    (F("name").icontains("A"), ("-name",)),
    (F("name").startswith("a"), ()),
    (F("name").istartswith("A"), ("name",)),
    (F("name").startswith("a%"), ()),
    (F("name").endswith("c"), ()),
    (F("name").iendswith("C"), ()),
    (F("name").endswith("toolong"), ()),
    (F("name").startswith("toolong"), ()),
    (F("label") == Label("A_C"), ()),
    (F("label").icontains("%b"), ()),
    (F("label") > "A", ("label",)),
    (F("price") > Decimal("1.995"), ("price", "-name")),
    (F("price") <= 2, ("-price",)),
    (F("price") < 2, ("-price",)),
    (F("stock") < 3, ("stock",)),
    (F("size") < Size.LARGE, ()),
    (F("price") == Decimal("2.000"), ()),
    (F("price").range(Decimal("-1.25"), Decimal("1.5")), ("price",)),
    (F("price").in_([Decimal("1.5"), Decimal("10")]), ()),
    (F("stock") > 0, ("stock",)),
    (~(F("stock") > 0), ("-stock", "name")),
    (F("stock") == None, ("-stock",)),  # noqa: E711
    (F("stock") != None, ("stock", "-price")),  # noqa: E711
    (F("stock") != 3, ("-stock", "name")),
    (F("active") > False, ()),
    (F("active").range(False, True), ("active",)),
    (F("name").contains("a") | F("name").contains("zz"), ()),
    (~F("name").startswith("a"), ("name",)),
    (F("parts.sku").startswith("x"), ()),
    (F("stock").isnull(), ()),
    (~F("stock").in_([0, 3]), ()),
    (F("stock").in_([]), ()),
    (~F("stock").in_([]), ("stock",)),
    (F("active") == False, ()),  # noqa: E712
    (F("color").in_(["red", "green"]), ("color",)),
    (F("color") >= Color.GREEN, ("-color",)),
    (F("size") > Size.SMALL, ("size", "-name")),
    (F("made_at") > MADE, ("-made_at",)),
    (F("made_at").range(MADE, LATER), ("made_at",)),
    (F("parts.sku") == "X2", ()),
    (F("parts.weight") >= 2, ()),
    (~(F("parts.sku").startswith("X")), ("name",)),
    (F("maker.name").icontains("ACME") | F("maker.name").isnull(False), ()),
    (~(F("maker.name") == "bolt"), ("name",)),
    ((F("stock") >= 0) & ~F("name").contains("%") | (F("size") == 2), ("-size",)),
]


async def test_sql_answers_as_the_fake_does(tables: None) -> None:
    """Every lookup, ordering, and relation gives the fake's answer."""
    items = dataset()
    await store(*items)
    fake = InMemoryGadgets(items)
    async with GadgetsUnitOfWork() as uow:
        for criteria, ordering in CASES:
            query = Selection(criteria, ordering=ordering)
            expected = [each.id for each in await fake.filter(query)]
            found = [each.id for each in await uow.gadgets.filter(query)]
            assert found == expected, (criteria, ordering)
            assert await uow.gadgets.count(criteria) == len(expected), criteria
            assert await uow.gadgets.exists(criteria) == bool(expected), criteria
            first = await uow.gadgets.first(query)
            assert (first and first.id) == (expected[0] if expected else None)
            paged = await uow.gadgets.paginate(query, limit=2, count=True)
            assert [each.id for each in paged.items] == expected[:2]
            assert paged.total == len(expected)


async def test_filters_refuse_amounts_past_the_columns_digits(tables: None) -> None:
    """``1e999999999`` is a 422 before any SQL; the widest amounts compare.

    The driver could not send such an amount to PostgreSQL: a 500.
    """
    await store(*dataset())

    class Priced(FilterSet):
        model = Gadget
        price = Filter("gte", "lte")

    app = FastAPI()
    install_exception_handlers(app)

    @app.get("/gadgets")
    async def listing(filters: Annotated[Priced, Depends()]) -> list[str]:
        async with GadgetsUnitOfWork() as uow:
            return [each.name for each in await uow.gadgets.filter(filters.query())]

    async with AsyncTestClient(app, lifespan=False) as client:
        for amount in ("1e999999999", "1e-999999999", "1e100000", "1e8", "0.001"):
            refused = await client.get("/gadgets", params={"price__gte": amount})
            assert refused.status_code == 422, amount
            assert refused.json()["code"] == "validation_error"
        widest = await client.get(
            "/gadgets",
            params={"price__gte": "-99999999.99", "price__lte": "99999999.99"},
        )

    assert sorted(widest.json()) == sorted(each.name for each in dataset())


async def test_results_are_tracked_and_relations_loaded(tables: None) -> None:
    """Loaded aggregates are tracked, with their children, so events publish."""
    events = EventBus()
    published: list[object] = []

    async def note(event: GadgetMade) -> None:
        published.append(event)

    events.subscribe(GadgetMade, note)
    await store(*dataset())
    async with GadgetsUnitOfWork(events=events) as uow:
        (found,) = await uow.gadgets.filter(F("parts.sku") == "X1")
        assert [part.sku for part in found.parts] == ["X1"]
        assert found.maker is not None
        assert found.maker.name == "Acme"
        assert uow.gadgets.seen == (found,)
        found.rename("renamed")
        await uow.commit()
    assert [event.gadget_id for event in published] == [found.id]  # type: ignore[attr-defined]
    async with GadgetsUnitOfWork() as uow:
        assert await uow.gadgets.count(F("name") == "renamed") == 1


async def test_offsets_skip_in_sql(tables: None) -> None:
    """``offset`` and ``limit`` page through the total order."""
    items = dataset()
    await store(*items)
    fake = InMemoryGadgets(items)
    query = Selection(ordering=["-price"])
    async with GadgetsUnitOfWork() as uow:
        for offset in range(6):
            page = await uow.gadgets.paginate(query, limit=2, offset=offset)
            expected = await fake.paginate(query, limit=2, offset=offset)
            assert [each.id for each in page.items] == [
                each.id for each in expected.items
            ]
            assert page.has_more == expected.has_more


class Loose(AggregateRoot[int]):
    """An aggregate whose dataclass has more than its table maps."""

    total: int
    unmapped: int = 0
    child: "Leaf | None" = None


LeafId = NewType("LeafId", int)


class Leaf(Entity[LeafId]):
    """A child entity nothing maps as a relationship."""

    size: int


loose_metadata = MetaData()
loose = Table(
    "framework_test_loose",
    loose_metadata,
    Column("id", Integer, primary_key=True),
    Column("a", Integer),
    Column("b", Integer),
    Column("version", Integer, nullable=False),
)
map_imperatively(
    Loose, loose, properties={"total": column_property(loose.c.a + loose.c.b)}
)


def test_names_resolve_only_to_mapped_columns_and_relationships() -> None:
    """A field the table does not map fails as loudly as an unknown one."""
    dialect = "sqlite"
    with pytest.raises(UnknownField, match=r"Loose\.unmapped is not a mapped column"):
        compile_criteria(F("unmapped") == 1, Loose, dialect)
    with pytest.raises(
        UnknownField, match=r"Loose\.child is not a mapped relationship"
    ):
        compile_criteria(F("child.size") == 1, Loose, dialect)
    with pytest.raises(InvalidQuery, match=r"Loose\.total is not a table column"):
        compile_criteria(F("total") == 1, Loose, dialect)
    with pytest.raises(InvalidQuery, match="Leaf is not mapped to a table"):
        mapper_of(Leaf)
    with pytest.raises(TypeError, match="Cannot compile Strange"):
        compile_criteria(Strange(), Gadget, dialect)
    assert mapper_of(Gadget).class_ is Gadget


class Strange(Criteria):
    """Criteria the compiler does not know."""

    def bind(self, model: type) -> Criteria:
        """Itself."""
        return self

    def evaluate(self, candidate: object) -> bool:
        """Never."""
        return False

    def conditions(self) -> Iterator[Any]:
        """None."""
        return iter(())


def test_text_is_matched_without_patterns() -> None:
    """PostgreSQL escapes LIKE's characters; SQLite uses instr and substr."""
    criteria = (F("name").contains("50%_off/") & F("name").istartswith("A")).bind(
        Gadget
    )
    on_postgres = compile_criteria(criteria, Gadget, "postgresql").compile(
        dialect=POSTGRES, compile_kwargs={"render_postcompile": True}
    )
    assert "LIKE" in str(on_postgres)
    assert "ESCAPE '/'" in str(on_postgres)
    assert "%50/%/_off//%" in on_postgres.params.values()
    assert "A%" in on_postgres.params.values()
    assert "lower(" in str(on_postgres)
    on_sqlite = str(
        compile_criteria(criteria, Gadget, "sqlite").compile(dialect=SQLITE)
    )
    assert "LIKE" not in on_sqlite
    assert "instr(" in on_sqlite
    assert "substr(" in on_sqlite
    nullable = compile_criteria(F("stock") > 1, Gadget, "sqlite")
    assert "IS NOT NULL" in str(nullable)
    assert "IS NOT NULL" not in str(compile_criteria(F("price") > 1, Gadget, "sqlite"))
    assert "IS NOT NULL" not in str(
        compile_criteria(F("stock").isnull(), Gadget, "sqlite")
    )
    ordering = compile_ordering(
        (Ordering("stock"), Ordering("stock", True), Ordering("name")), Gadget
    )
    assert [str(term).split()[-2:] for term in ordering[:2]] == [
        ["NULLS", "LAST"],
        ["NULLS", "FIRST"],
    ]
    assert "NULLS" not in str(ordering[2])


async def test_unknown_fields_fail_before_any_sql(tables: None) -> None:
    """The query is checked against the aggregate first."""
    async with GadgetsUnitOfWork() as uow:
        with pytest.raises(UnknownField):
            await uow.gadgets.filter(F("nope") == 1)
        with pytest.raises(UnknownField):
            await uow.gadgets.count(F("nope") == 1)
        with pytest.raises(InvalidQuery):
            await uow.gadgets.filter(Selection(ordering=["parts"]))


async def test_soft_deleted_rows_stay_but_are_left_out(tables: None) -> None:
    """Removing marks the row; reads skip it unless asked; restore undoes it."""
    kept, gone = make_gadget("kept"), make_gadget("gone")
    await store(kept, gone)
    async with GadgetsUnitOfWork() as uow:
        await uow.gadgets.remove(await uow.gadgets.get(gone.id))
        await uow.commit()
    async with GadgetsUnitOfWork() as uow:
        assert await uow.gadgets.find(gone.id) is None
        assert await uow.gadgets.count() == 1
        assert await uow.gadgets.count(include_deleted=True) == 2
        found = await uow.gadgets.filter(
            Selection(include_deleted=True, ordering=["name"])
        )
        assert [each.name for each in found] == ["gone", "kept"]
        restored = await uow.gadgets.restore(gone.id)
        assert restored.deleted_at is None
        await uow.commit()
    async with GadgetsUnitOfWork() as uow:
        assert await uow.gadgets.count() == 2
        await uow.gadgets.remove_all(await uow.gadgets.filter(), hard=True)
        await uow.commit()
    async with connections.engine().connect() as connection:
        assert (await connection.execute(select(gadgets.c.id))).all() == []
        assert (await connection.execute(select(parts.c.id))).all() == []


async def test_bulk_updates_bypass_aggregates_but_bump_versions(tables: None) -> None:
    """update_rows changes rows in one statement, without events."""
    events = EventBus()
    published: list[object] = []

    async def note(event: GadgetMade) -> None:
        published.append(event)

    events.subscribe(GadgetMade, note)
    red, blue = make_gadget("red"), make_gadget("blue", color=Color.BLUE)
    await store(red, blue)
    async with GadgetsUnitOfWork(events=events) as uow:
        loaded = await uow.gadgets.get(red.id)
        assert loaded.version == 1
        changed = await uow.gadgets.update_rows(F("color") == "red", stock=7, size=10)
        assert changed == 1
        assert (loaded.stock, loaded.size, loaded.version) == (7, Size.LARGE, 2)
        assert await uow.gadgets.update_rows(None, active=False) == 2
        await uow.commit()
    assert published == []
    async with GadgetsUnitOfWork() as uow:
        found = await uow.gadgets.filter(Selection(ordering=["name"]))
        assert [(each.name, each.active, each.version) for each in found] == [
            ("blue", False, 2),
            ("red", False, 3),
        ]


class Plain(AggregateRoot[int]):
    """An aggregate whose table has no version column, on purpose."""

    text: str


plain = Table(
    "framework_test_plain",
    loose_metadata,
    Column("id", Integer, primary_key=True),
    Column("text", String(10), nullable=False),
)
map_imperatively(Plain, plain, versioned=False)


class PlainRepository(SqlAlchemyRepository[Plain, int]):
    """Plain rows."""


async def test_bulk_updates_without_a_version_column(database: str) -> None:
    """Without optimistic locking, nothing more is set."""
    async with connections.engine().begin() as connection:
        await connection.run_sync(loose_metadata.create_all)
    async with connections.session() as session:
        repository = PlainRepository(session)
        await repository.add_all([Plain(id=1, text="a"), Plain(id=2, text="b")])
        assert await repository.update_rows(F("text") == "a", text="c") == 1
        await session.commit()
        assert [each.text for each in await repository.filter()] == ["c", "b"]
    await connections.close_all()


async def test_bulk_updates_check_what_they_set(tables: None) -> None:
    """Only mapped fields other than the id and version, with fitting values."""
    cases: list[tuple[dict[str, Any], str]] = [
        ({"id": uuid4()}, "cannot set 'id'"),
        ({"version": 3}, "cannot set 'version'"),
        ({"nope": 3}, "cannot set 'nope'"),
        ({}, "at least one value"),
        ({"stock": "many"}, "holds int, not str"),
        ({"parts": []}, "is not a mapped column"),
    ]
    async with GadgetsUnitOfWork() as uow:
        for values, message in cases:
            with pytest.raises(InvalidQuery, match=message):
                await uow.gadgets.update_rows(None, **values)


async def test_bulk_deletes_remove_rows_for_good(tables: None) -> None:
    """delete_rows deletes soft-deletable rows too, live ones unless asked."""
    first, second = make_gadget("first"), make_gadget("second")
    second.mark_deleted()
    await store(first, second)
    async with GadgetsUnitOfWork() as uow:
        assert await uow.gadgets.delete_rows(F("name").startswith("s")) == 0
        assert await uow.gadgets.delete_rows(None, include_deleted=True) == 2
        await uow.commit()
    async with GadgetsUnitOfWork() as uow:
        assert await uow.gadgets.count(include_deleted=True) == 0


async def test_a_failed_savepoint_reloads_what_it_changed(tables: None) -> None:
    """ROLLBACK TO SAVEPOINT; changed aggregates read again, added ones gone."""
    kept = make_gadget("kept", stock=1)
    await store(kept)
    added = make_gadget("added")

    async def change(uow: GadgetsUnitOfWork, loaded: Gadget) -> None:
        async with uow.savepoint():
            loaded.rename("changed")
            loaded.stock = 99
            await uow.gadgets.add(added)
            await uow.gadgets.count()  # Flushes both inside the savepoint.
            raise RuntimeError("undo")

    async with GadgetsUnitOfWork() as uow:
        loaded = await uow.gadgets.get(kept.id)
        with pytest.raises(RuntimeError, match="undo"):
            await change(uow, loaded)
        assert (loaded.name, loaded.stock) == ("kept", 1)
        assert loaded.pending_events == ()
        assert uow.gadgets.seen == (loaded,)
        assert await uow.gadgets.count() == 1
        async with uow.savepoint():
            loaded.stock = 2
        await uow.commit()
    async with GadgetsUnitOfWork() as uow:
        assert (await uow.gadgets.get(kept.id)).stock == 2


async def test_a_duplicate_inside_a_savepoint_is_a_conflict(tables: None) -> None:
    """The flush at release fails; the savepoint is undone, the rest commits."""
    first = make_gadget("first")
    await store(first)
    twin = make_gadget("twin")
    twin.id = first.id

    async def duplicate(uow: GadgetsUnitOfWork) -> None:
        async with uow.savepoint():
            await uow.gadgets.add(twin)

    async with GadgetsUnitOfWork() as uow:
        other = make_gadget("other")
        await uow.gadgets.add(other)
        with pytest.raises(DuplicateValue) as caught:
            await duplicate(uow)
        assert caught.value.code == "duplicate_value"
        await uow.commit()
    async with GadgetsUnitOfWork() as uow:
        assert sorted(each.name for each in await uow.gadgets.filter()) == [
            "first",
            "other",
        ]


async def test_other_errors_in_a_savepoint_stay_as_they_are(tables: None) -> None:
    """Only unique violations are translated."""

    async def fail(uow: GadgetsUnitOfWork) -> None:
        async with uow.savepoint():
            raise KeyError("x")

    async with GadgetsUnitOfWork() as uow:
        with pytest.raises(KeyError):
            await fail(uow)


async def test_read_only_work_refuses_to_commit_in_sql(tables: None) -> None:
    """Read-only units of work read, and refuse to commit."""
    await store(make_gadget("a"))
    async with GadgetsUnitOfWork(readonly=True) as uow:
        assert await uow.gadgets.count() == 1
        with pytest.raises(ReadOnlyError):
            await uow.commit()


@postgres_only
async def test_read_only_transactions_refuse_writes_on_postgresql(tables: None) -> None:
    """Even a write that skips the unit of work is refused by the server."""
    async with GadgetsUnitOfWork(readonly=True) as uow:
        uow.session.add(make_gadget("sneaky"))
        with pytest.raises(DBAPIError, match="read-only transaction"):
            await uow.session.flush()


@pytest.fixture
async def sqlite_tables(database_access: None, tmp_path: Path) -> AsyncIterator[None]:
    """The gadget tables in a SQLite file, whatever server the suite targets."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'gadgets.sqlite3'}"
    with override_settings(DATABASES={"default": {"URL": url}}):
        async with connections.engine().begin() as connection:
            await connection.run_sync(metadata.create_all)
        yield
        await connections.close_all()


async def test_sqlite_reads_without_row_locks(sqlite_tables: None) -> None:
    """SQLite has no row locks: a second transaction reads at once."""
    gadget = make_gadget("a")
    await store(gadget)
    async with GadgetsUnitOfWork() as one, GadgetsUnitOfWork() as two:
        assert (await one.gadgets.get_for_update(gadget.id)).name == "a"
        found = await two.gadgets.get_for_update(gadget.id, nowait=True)
        assert found.name == "a"
        assert await two.gadgets.get_for_update(gadget.id, skip_locked=True) is not None


@postgres_only
async def test_rows_lock_until_the_transaction_ends(tables: None) -> None:
    """Waiting, nowait, and skip_locked, as on the server."""
    gadget, other = make_gadget("a", stock=1), make_gadget("b", stock=1)
    await store(gadget, other)
    async with GadgetsUnitOfWork() as one, GadgetsUnitOfWork() as two:
        locked = await one.gadgets.get_for_update(gadget.id)
        with pytest.raises(LockNotAvailable):
            await two.gadgets.get_for_update(gadget.id, nowait=True)
        assert await two.gadgets.count() == 2  # The transaction lives on.
        assert await two.gadgets.get_for_update(gadget.id, skip_locked=True) is None
        skipped = await two.gadgets.filter(
            Selection(ordering=["name"], limit=1, lock=RowLock(skip_locked=True))
        )
        assert [each.name for each in skipped] == ["b"]
        waiting = asyncio.create_task(two.gadgets.get_for_update(gadget.id))
        done, _ = await asyncio.wait({waiting}, timeout=0.3)
        assert not done
        locked.stock = 5
        await one.commit()
        async with asyncio.timeout(5):
            assert (await waiting).stock == 5


async def test_nowait_passes_other_database_errors_on(
    tables: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only lock_not_available becomes LockNotAvailable."""
    gadget = make_gadget("a")
    await store(gadget)

    class Refused(Exception):
        sqlstate = "42501"

    async with GadgetsUnitOfWork() as uow:

        async def refuse(*args: object, **kwargs: object) -> None:
            raise DBAPIError("SELECT", {}, Refused())

        monkeypatch.setattr(uow.session, "scalars", refuse)
        with pytest.raises(DBAPIError):
            await uow.gadgets.get_for_update(gadget.id, nowait=True)

        class Locked(Exception):
            pgcode = "55P03"

        async def locked(*args: object, **kwargs: object) -> None:
            raise DBAPIError("SELECT", {}, Locked())

        monkeypatch.setattr(uow.session, "scalars", locked)
        with pytest.raises(LockNotAvailable):
            await uow.gadgets.get_for_update(gadget.id, nowait=True)


def test_values_bind_through_their_column_types() -> None:
    """Amounts and value objects reach SQL as bound parameters."""
    criteria = ((F("price") > Decimal("1.995")) & (F("label") == "X")).bind(Gadget)
    compiled = compile_criteria(criteria, Gadget, "sqlite").compile(dialect=SQLITE)

    assert "1.995" not in str(compiled)
    assert set(compiled.params.values()) == {Decimal("1.995"), Label("X")}


async def test_a_page_costs_its_query_and_its_childrens(tables: None) -> None:
    """One SELECT, one per child relationship (selectin), and one to count."""
    await store(*dataset())
    async with GadgetsUnitOfWork() as uow:
        with assert_num_queries(3):  # Gadgets, then their parts and makers.
            page = await uow.gadgets.paginate(F("stock") >= 0, limit=2)
        with assert_num_queries(4):
            await uow.gadgets.paginate(limit=2, after=page.next_position, count=True)
        with assert_num_queries(1):
            await uow.gadgets.exists(F("parts.sku") == "X1")
