"""Queries compiled to SQL answer as the in-memory repository does."""

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, date, datetime
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
    false,
    select,
)
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import column_property
from sqlalchemy.sql.elements import ColumnElement

from domainkit.conf import override_settings
from domainkit.db import SqlAlchemyRepository, connections, map_imperatively
from domainkit.db import repository as db_repository
from domainkit.db.query import compile_criteria, compile_ordering, mapper_of
from domainkit.ddd import (
    AggregateRoot,
    DuplicateValue,
    Encrypted,
    Entity,
    EventBus,
    InvalidQuery,
    LockNotAvailable,
    MultipleFound,
    ReadOnlyError,
    UnknownField,
)
from domainkit.ddd.query import (
    ID,
    Avg,
    Count,
    Criteria,
    F,
    Max,
    Min,
    Ordering,
    Period,
    RowLock,
    Selection,
    Sum,
    Within,
)
from domainkit.http.errors import install_exception_handlers
from domainkit.http.filters import Filter, FilterSet
from domainkit.money import Money
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
    (Within("parts", F("sku").startswith("x")), ()),  # Case-sensitive on SQLite too.
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


def test_a_compiler_of_ones_own_criteria_is_told_the_dialect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``register_compiler``'s compilers get the dialect's name, inside others too."""
    from domainkit.db import query as query_module

    class Odd(Criteria):
        def bind(self, model: type) -> Criteria:
            return self

        def evaluate(self, candidate: object) -> bool:
            return False

        def conditions(self) -> Iterator[Any]:
            return iter(())

    told: list[str] = []

    def compile_odd(
        criteria: Any,  # noqa: ANN401 - as register_compiler gives it.
        model: type,
        dialect: str,
    ) -> ColumnElement[bool]:
        told.append(dialect)
        return false()

    monkeypatch.setattr(query_module, "_COMPILERS", dict(query_module._COMPILERS))
    query_module.register_compiler(Odd, compile_odd)
    compile_criteria(Odd(), Gadget, "sqlite")
    compile_criteria(Within("parts", Odd()), Gadget, "postgresql")

    assert told == ["sqlite", "postgresql"]


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
        ({}, r"^update_rows needs at least one value to set\.$"),
        ({"stock": "many"}, "holds int, not str"),
        ({"parts": []}, "is not a mapped column"),
    ]
    async with GadgetsUnitOfWork() as uow:
        for values, message in cases:
            with pytest.raises(InvalidQuery, match=message):
                await uow.gadgets.update_rows(None, **values)


async def test_bulk_updates_leave_soft_deleted_rows_unless_asked(tables: None) -> None:
    """update_rows sets live rows; soft-deleted ones too with include_deleted."""
    live, gone = make_gadget("live"), make_gadget("gone")
    gone.mark_deleted()
    await store(live, gone)
    async with GadgetsUnitOfWork() as uow:
        assert await uow.gadgets.update_rows(None, stock=1) == 1
        assert await uow.gadgets.update_rows(None, include_deleted=True, stock=2) == 2
        await uow.commit()


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


async def test_a_locking_read_shows_what_was_committed_since(tables: None) -> None:
    """``get_for_update`` reads the row again: not the copy loaded before."""
    gadget = make_gadget("g", stock=1)
    await store(gadget)
    async with GadgetsUnitOfWork() as uow:
        await uow.gadgets.get(gadget.id)
        async with GadgetsUnitOfWork() as other:
            await other.gadgets.update_rows(F("name") == "g", stock=5)
            await other.commit()

        locked = await uow.gadgets.get_for_update(gadget.id)

    assert locked.stock == 5


async def test_bulk_updates_leave_deleted_rows_unless_asked(tables: None) -> None:
    """update_rows sets live rows only; ``include_deleted`` reaches the others."""
    live, deleted = make_gadget("live"), make_gadget("deleted")
    deleted.mark_deleted()
    await store(live, deleted)
    async with GadgetsUnitOfWork() as uow:
        assert await uow.gadgets.update_rows(None, stock=1) == 1
        assert await uow.gadgets.update_rows(None, include_deleted=True, size=10) == 2
        await uow.commit()
    async with GadgetsUnitOfWork() as uow:
        found = await uow.gadgets.filter(
            Selection(ordering=["name"], include_deleted=True)
        )
    assert [(each.name, each.stock, each.size) for each in found] == [
        ("deleted", None, Size.LARGE),
        ("live", 1, Size.LARGE),
    ]


async def test_bulk_deletes_drop_the_aggregates_loaded_before(tables: None) -> None:
    """An aggregate read before delete_rows is gone from the same unit of work."""
    gadget = make_gadget("doomed")
    await store(gadget)
    async with GadgetsUnitOfWork() as uow:
        await uow.gadgets.get(gadget.id)
        assert await uow.gadgets.delete_rows(F("name") == "doomed") == 1

        assert await uow.gadgets.find(gadget.id) is None


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
        refused = r"^The data is being changed by another request; try again\.$"
        with pytest.raises(LockNotAvailable, match=refused):
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


async def test_streams_read_everything_in_batches(tables: None) -> None:
    """``stream`` yields what ``filter`` does, ``batch`` rows a statement."""
    items = dataset()
    await store(*items)
    fake = InMemoryGadgets(items)
    query = Selection(F("price") > Decimal(0), ordering=["-price"])
    expected = [each.id for each in await fake.filter(query)]
    async with GadgetsUnitOfWork() as uow:
        streamed = [each.id async for each in uow.gadgets.stream(query, batch=2)]
        assert [each.id for each in uow.gadgets.seen] == streamed
        everything = [each.id async for each in uow.gadgets.stream(batch=10)]
    assert streamed == expected == [each.id async for each in fake.stream(query)]
    assert sorted(everything) == sorted(each.id for each in items)


async def test_a_stream_is_not_a_page(tables: None) -> None:
    """A limit, an offset, or an empty batch is refused before any read."""
    async with GadgetsUnitOfWork() as uow:
        with pytest.raises(ValueError, match="at least one aggregate"):
            await anext(uow.gadgets.stream(batch=0))
        with pytest.raises(ValueError, match="paginate"):
            await anext(uow.gadgets.stream(Selection(limit=3)))
        with pytest.raises(ValueError, match="paginate"):
            await anext(uow.gadgets.stream(Selection(offset=1)))


async def test_explain_gives_the_plan_with_values_bound(tables: None) -> None:
    """The database's plan for a query; nothing is loaded or tracked."""
    await store(*dataset())
    query = Selection(F("name") == "x' OR 1=1 --", ordering=["-price"], lock=RowLock())
    async with GadgetsUnitOfWork() as uow:
        plan = await uow.gadgets.explain(query)
        assert uow.gadgets.seen == ()
        if ON_POSTGRES:
            assert "gadgets" in plan
            assert "actual time" in await uow.gadgets.explain(query, analyze=True)
        else:
            assert "gadgets" in plan.lower()
            with pytest.raises(ValueError, match="PostgreSQL's"):
                await uow.gadgets.explain(query, analyze=True)


async def test_get_or_create_reads_or_stores_one(tables: None) -> None:
    """The one that matches, or a new one stored at once and tracked."""
    kept = make_gadget("kept")
    await store(kept, make_gadget("twin", stock=7), make_gadget("twin", stock=8))
    async with GadgetsUnitOfWork() as uow:
        found, created = await uow.gadgets.get_or_create(
            F("name") == "kept", lambda: make_gadget("never")
        )
        assert (found.id, created) == (kept.id, False)
        made, created = await uow.gadgets.get_or_create(
            F("name") == "new", lambda: make_gadget("new")
        )
        assert created
        assert made in uow.gadgets.seen
        with pytest.raises(MultipleFound) as caught:
            await uow.gadgets.get_or_create(F("name") == "twin", make_gadget)
        assert caught.value.code == "multiple_found"
        await uow.commit()
    async with GadgetsUnitOfWork() as uow:
        assert await uow.gadgets.count(F("name") == "new") == 1


async def test_get_or_create_returns_what_won_the_race(
    tables: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stored first by another transaction: the unique constraint answers."""
    winner = make_gadget("winner")
    await store(winner)

    def loser() -> Gadget:
        twin = make_gadget("winner")
        twin.id = winner.id
        return twin

    async with GadgetsUnitOfWork() as uow:
        one = uow.gadgets._one
        calls: list[object] = []

        async def late(criteria: Any, **options: Any) -> Gadget | None:  # noqa: ANN401
            calls.append(criteria)
            return None if len(calls) == 1 else await one(criteria, **options)

        monkeypatch.setattr(uow.gadgets, "_one", late)
        found, created = await uow.gadgets.get_or_create(F(ID) == winner.id, loser)
        assert (found.id, found.name, created) == (winner.id, "winner", False)
        other = make_gadget("other")
        await uow.gadgets.add(other)
        await uow.commit()
    async with GadgetsUnitOfWork() as uow:
        with pytest.raises(DuplicateValue):  # Taken by one the criteria miss.
            await uow.gadgets.get_or_create(F("name") == "nobody", loser)
        assert await uow.gadgets.count() == 2


async def test_other_refusals_of_a_new_aggregate_stay_as_they_are(
    tables: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a unique violation reads the winner; any other refusal is raised."""
    winner = make_gadget("winner")
    await store(winner)
    monkeypatch.setattr(db_repository, "duplicate_value", lambda *_: None)

    def loser() -> Gadget:
        twin = make_gadget("x")
        twin.id = winner.id
        return twin

    async with GadgetsUnitOfWork() as uow:
        with pytest.raises(IntegrityError):
            await uow.gadgets.get_or_create(F("name") == "x", loser)


async def test_update_or_create_changes_or_stores(
    tables: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one found is changed through its methods; else a new one is stored."""
    kept = make_gadget("kept")
    await store(kept)

    def rename(gadget: Gadget) -> None:
        gadget.rename("renamed")

    async with GadgetsUnitOfWork() as uow:
        found, created = await uow.gadgets.update_or_create(
            F(ID) == kept.id, rename, lambda: make_gadget("never")
        )
        assert (found.name, created) == ("renamed", False)
        made, created = await uow.gadgets.update_or_create(
            F("name") == "fresh", rename, lambda: make_gadget("fresh")
        )
        assert (made.name, created) == ("fresh", True)
        await uow.commit()
    async with GadgetsUnitOfWork() as uow:
        assert {each.name for each in await uow.gadgets.filter()} == {
            "renamed",
            "fresh",
        }

    async with GadgetsUnitOfWork() as uow:
        repository = uow.gadgets
        answers: list[Gadget | None] = [None, None]
        one = repository._one

        async def raced(criteria: Any, **options: Any) -> Gadget | None:  # noqa: ANN401
            return answers.pop(0) if answers else await one(criteria, **options)

        monkeypatch.setattr(repository, "_one", raced)
        # Missing, then found by get_or_create's insert race, then locked.
        winner = await repository.get(kept.id)
        answers[:] = [None, winner, winner]
        found, created = await repository.update_or_create(
            F(ID) == kept.id, rename, lambda: make_gadget("never")
        )
        assert (found is winner, created) == (True, False)
        # Removed again between the race and the lock: created after all.
        answers[:] = [None, winner, None, None]
        made, created = await repository.update_or_create(
            F("name") == "third", rename, lambda: make_gadget("third")
        )
        assert (made.name, created) == ("third", True)


async def test_get_many_leaves_out_what_is_missing(tables: None) -> None:
    """Django's ``in_bulk``: one statement, by ascending id, none locked."""
    first, second = make_gadget("a"), make_gadget("b")
    await store(first, second)
    async with GadgetsUnitOfWork() as uow:
        assert await uow.gadgets.get_many([]) == {}
        with assert_num_queries(3):  # The gadgets, then their parts and makers.
            found = await uow.gadgets.get_many(
                [second.id, make_gadget().id, first.id, second.id]
            )
    assert set(found) == {first.id, second.id}
    assert list(found) == sorted(found, key=str)


async def test_the_conveniences_work_in_memory_too() -> None:
    """The in-memory repository answers as the SQL one does."""
    kept = make_gadget("kept")
    fake = InMemoryGadgets([kept])

    found, created = await fake.get_or_create(F("name") == "kept", make_gadget)
    made, made_now = await fake.get_or_create(
        F("name") == "new", lambda: make_gadget("new")
    )
    changed, _ = await fake.update_or_create(
        F(ID) == kept.id, lambda gadget: gadget.rename("renamed"), make_gadget
    )

    assert (found is kept, created, made_now) == (True, False, True)
    assert changed.name == "renamed"
    assert set(await fake.get_many([made.id, kept.id])) == {made.id, kept.id}
    assert [each.name async for each in fake.stream(batch=1)] in (
        ["renamed", "new"],
        ["new", "renamed"],
    )


async def test_aggregate_measures_in_one_statement(tables: None) -> None:
    """Counts, totals, means, and extremes, as the in-memory repository has them."""
    items = dataset()
    await store(*items)
    fake = InMemoryGadgets(items)
    measures: dict[str, Any] = {
        "gadgets": Count(),
        "stocked": Count("stock"),
        "stock": Sum("stock"),
        "revenue": Sum("price"),
        "mean": Avg("price"),
        "mean_stock": Avg("stock"),
        "cheapest": Min("price"),
        "first": Min("made_at"),
        "last_name": Max("name"),
        "label": Max("label"),
    }
    expected = {
        "gadgets": 5,
        "stocked": 3,
        "stock": 6,
        "revenue": Decimal("14.25"),
        "mean": Decimal("2.85"),
        "mean_stock": 2.0,
        "cheapest": Decimal("-1.25"),
        "first": MADE,
        "last_name": "é中\\",
        "label": Label("É中\\"),
    }
    async with GadgetsUnitOfWork() as uow:
        with assert_num_queries(1):
            measured = await uow.gadgets.aggregate(**measures)
        none = await uow.gadgets.aggregate(
            F("name") == "nothing", n=Count(), total=Sum("price"), mean=Avg("stock")
        )
        assert uow.gadgets.seen == ()
    assert measured == expected == await fake.aggregate(**measures)
    assert none == {"n": 0, "total": None, "mean": None}
    assert await fake.aggregate(F("name") == "nothing", mean=Avg("stock")) == {
        "mean": None
    }


async def test_measures_that_do_not_fit_are_refused() -> None:
    """A measure needs a field it fits, and a call needs a measure."""
    fake = InMemoryGadgets([])
    for measure, message in [
        (Sum(None), "measures a field"),
        (Sum("name"), "not a number"),
        (Avg("active"), "not a number"),
        (Max("parts"), "child entities"),
        (Min("nowhere"), "nowhere"),
    ]:
        with pytest.raises(InvalidQuery, match=message):
            await fake.aggregate(measured=measure)
    with pytest.raises(TypeError, match="takes measures"):
        await fake.aggregate()

    class Purse(AggregateRoot[int]):
        cash: Money
        secret: Annotated[str, Encrypted(subject="id")]

    with pytest.raises(InvalidQuery, match="per currency"):
        Sum("cash").bind(Purse)
    with pytest.raises(InvalidQuery, match="cannot be measured"):
        Max("secret").bind(Purse)
    assert Count("cash").bind(Purse) == Count("cash")


async def test_regular_expressions_match_as_in_memory(tables: None) -> None:
    """``regex`` and ``iregex``, in SQL as in Python, on both databases."""
    items = dataset()
    await store(*items)
    fake = InMemoryGadgets(items)
    queries = [
        F("name").regex(r"^[ab]"),
        F("name").iregex(r"^[ab]"),
        F("name").regex("c$"),
    ]

    async with GadgetsUnitOfWork() as uow:
        for criteria in queries:
            found = sorted(each.name for each in await uow.gadgets.filter(criteria))
            expected = sorted(each.name for each in await fake.filter(criteria))
            assert found == expected
        assert sorted(each.name for each in await uow.gadgets.filter(queries[1])) == [
            "A_c",
            "a%b",
            "b/c",
        ]


async def test_patterns_that_cannot_apply_are_refused() -> None:
    """A pattern that does not compile, or a field that is not text."""
    fake = InMemoryGadgets([])

    with pytest.raises(InvalidQuery, match="is not a regular expression"):
        await fake.filter(F("name").regex("(unclosed"))
    with pytest.raises(InvalidQuery, match="stock is not text"):
        await fake.filter(F("stock").iregex("1"))


async def test_periods_are_ranges_on_dates_and_datetimes(tables: None) -> None:
    """``year``, ``month``, ``on``: ranges in UTC, as in memory."""
    items = dataset()
    await store(*items)
    fake = InMemoryGadgets(items)
    later = sorted(each.name for each in items if each.made_at == LATER)
    periods = {
        "year": F("made_at").year(LATER.year),
        "month": F("made_at").month(LATER.year, LATER.month),
        "day": F("made_at").on(LATER.date()),
        "december": F("made_at").month(LATER.year - 1, 12),
    }

    async with GadgetsUnitOfWork() as uow:
        found = {
            name: sorted(each.name for each in await uow.gadgets.filter(criteria))
            for name, criteria in periods.items()
        }
    expected = {
        name: sorted(each.name for each in await fake.filter(criteria))
        for name, criteria in periods.items()
    }

    assert found == expected
    assert found["day"] == found["month"] == later
    assert F("made_at").month(2026, 12) == Period(
        "made_at", date(2026, 12, 1), date(2027, 1, 1)
    )


def test_a_period_needs_a_field_of_dates() -> None:
    """Not a number; and a period is evaluated only once bound."""
    period = F("stock").year(2026)

    with pytest.raises(InvalidQuery, match="holds no dates"):
        period.bind(Gadget)
    with pytest.raises(InvalidQuery, match="Bind the criteria"):
        period.evaluate(make_gadget())
    assert list(period.conditions()) == [
        F("stock") >= date(2026, 1, 1),
        F("stock") < date(2027, 1, 1),
    ]


    class Invoice(AggregateRoot[int]):
        due: date

    bound = F("due").on(date(2026, 3, 9)).bind(Invoice)
    assert [each.value for each in bound.conditions()] == [
        date(2026, 3, 9),
        date(2026, 3, 10),
    ]
    assert bound.evaluate(Invoice(id=1, due=date(2026, 3, 9)))
    assert not bound.evaluate(Invoice(id=2, due=date(2026, 3, 10)))
