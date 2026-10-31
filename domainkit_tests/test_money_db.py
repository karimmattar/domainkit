"""Money in tables: two exact columns, one attribute, queries on both (ADR 0115)."""

import io
import os
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, NewType
from uuid import UUID, uuid4

import pytest
from sqlalchemy import (
    CheckConstraint,
    Column,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    bindparam,
    func,
    insert,
    select,
    text,
)
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import class_mapper, composite

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.db import (
    CurrencyCode,
    ExactDecimal,
    MoneyColumns,
    SqlAlchemyRepository,
    SqlAlchemyUnitOfWork,
    connections,
    map_imperatively,
    table_for,
)
from domainkit.db import migrations as migrations_module
from domainkit.db.fixtures import FixtureError, _decoded_row
from domainkit.db.money import IncompleteMoney, money_properties
from domainkit.db.orm import NAMING_CONVENTION
from domainkit.db.query import compile_ordering
from domainkit.ddd import (
    AggregateRoot,
    Digits,
    InMemoryRepository,
    InvalidQuery,
    MaxLength,
    UnknownField,
)
from domainkit.ddd.query import Criteria, F, Ordering, Selection
from domainkit.exceptions import ImproperlyConfigured
from domainkit.management import call_command
from domainkit.money import Currency, Money, UnknownCurrency, currency
from domainkit_tests.conftest import ALPHA, POSTGRES_VARIABLE

pytestmark = pytest.mark.anyio
ON_POSTGRES = bool(os.environ.get(POSTGRES_VARIABLE))
WalletId = NewType("WalletId", UUID)


class Wallet(AggregateRoot[WalletId]):
    """An aggregate of money: a balance, a limit that may be missing, a unit."""

    name: Annotated[str, MaxLength(20)]
    balance: Money
    limit: Annotated[Money, Digits(12, 2)] | None = None
    unit: Currency | None = None


metadata = MetaData()
wallets = table_for(
    Wallet,
    "framework_test_money_wallets",
    Column("name", String(20, collation="C").with_variant(String(20), "sqlite")),
    Index("ix_framework_test_money_wallets_balance", "balance_currency", "balance"),
    metadata=metadata,
)
map_imperatively(Wallet, wallets)


class Wallets(SqlAlchemyRepository[Wallet, WalletId]):
    """Wallets in SQL."""


class InMemoryWallets(InMemoryRepository[Wallet, WalletId]):
    """Wallets in memory."""


class WalletsUnitOfWork(SqlAlchemyUnitOfWork):
    """One transaction over the wallets."""

    wallets: Wallets

    def create_repositories(self, session: AsyncSession) -> None:
        """Bind the repository."""
        self.wallets = Wallets(session)


def money(amount: str, code: str) -> Money:
    """``amount`` of ``code``."""
    return Money.of(amount, code)


def wallet(
    name: str, balance: Money, limit: Money | None = None, unit: str | None = None
) -> Wallet:
    """A new wallet."""
    return Wallet(
        id=WalletId(uuid4()),
        name=name,
        balance=balance,
        limit=limit,
        unit=None if unit is None else currency(unit),
    )


def dataset() -> list[Wallet]:
    """Wallets in four currencies, with ties, places of three, and missing limits."""
    return [
        wallet("a", money("10.00", "USD"), money("100.00", "USD"), "USD"),
        wallet("b", money("9.99", "USD"), None, "EUR"),
        wallet("c", money("10.00", "EUR"), money("5.00", "EUR")),
        wallet("d", money("-2.50", "USD"), money("100.00", "EUR"), "USD"),
        wallet("e", money("1.234", "KWD"), money("0.10", "USD")),
        wallet("f", money("1000", "JPY")),
        wallet("g", money("0.00", "EUR"), money("100.00", "USD"), "KWD"),
        wallet("h", money("99999999999999.99", "USD"), None, "JPY"),
    ]


@pytest.fixture
def database(database_url: str) -> Iterator[str]:
    """An empty database, selected in the settings."""
    with override_settings(DATABASES={"default": {"URL": database_url}}):
        yield database_url


@pytest.fixture
async def tables(database: str) -> AsyncIterator[None]:
    """The wallet table, in the running loop's engine."""
    async with connections.engine().begin() as connection:
        await connection.run_sync(metadata.create_all)
    yield
    await connections.close_all()


async def store(*items: Wallet) -> None:
    """Commit ``items`` in one unit of work."""
    async with WalletsUnitOfWork() as uow:
        await uow.wallets.add_all(items)
        await uow.commit()


def names(found: list[Wallet]) -> list[str]:
    """The names of ``found``, in their order."""
    return [each.name for each in found]


def test_a_money_field_is_two_columns_and_a_check_if_it_may_be_missing() -> None:
    """``<name>`` holds the amount exactly, ``<name>_currency`` the code."""
    columns = {column.name: column for column in wallets.columns}

    assert list(columns) == [
        "id",
        "version",
        "name",
        "balance",
        "balance_currency",
        "limit",
        "limit_currency",
        "unit",
    ]
    balance, limit = columns["balance"].type, columns["limit"].type
    assert isinstance(balance, ExactDecimal)
    assert (balance.precision, balance.scale) == (18, 4)
    assert isinstance(limit, ExactDecimal)
    assert (limit.precision, limit.scale) == (12, 2)
    assert isinstance(columns["balance_currency"].type, CurrencyCode)
    assert isinstance(columns["unit"].type, CurrencyCode)
    assert not columns["balance"].nullable
    assert not columns["balance_currency"].nullable
    assert columns["limit"].nullable
    assert columns["limit_currency"].nullable
    checks = [each.name for each in wallets.constraints if "money" in str(each.name)]
    assert checks == ["limit_money"]
    assert wallets.c.balance.info == {"money": ("balance", "amount")}


def test_given_money_columns_replace_the_derived_ones() -> None:
    """``*MoneyColumns(...)`` sizes them; their own check is kept, once."""
    table = table_for(
        Wallet,
        "framework_test_money_sized",
        *MoneyColumns("limit", 8, 3, nullable=True),
        Column("balance", ExactDecimal(10, 2), info={"money": ("balance", "amount")}),
        metadata=MetaData(),
    )

    assert table.c.limit.type.scale == 3  # type: ignore[attr-defined]
    assert table.c.balance.type.precision == 10  # type: ignore[attr-defined]
    assert isinstance(table.c.balance_currency.type, CurrencyCode)
    assert sum("money" in str(each.name) for each in table.constraints) == 1


def test_a_lone_money_column_is_refused() -> None:
    """Both columns of a pair, or neither."""
    table = Table(
        "framework_test_money_lone",
        MetaData(),
        Column("id", Integer, primary_key=True),
        MoneyColumns("total").amount,
    )

    with pytest.raises(ImproperlyConfigured, match="one money column of two"):
        money_properties(table, {})
    assert money_properties(table, {"total": object()}) == {}


async def test_money_round_trips_exactly(tables: None) -> None:
    """Every currency's places, 16 digits, and a missing limit come back as stored."""
    items = dataset()
    await store(*items)

    async with WalletsUnitOfWork() as uow:
        loaded = {each.name: each for each in await uow.wallets.filter()}

    for item in items:
        found = loaded[item.name]
        assert found.balance == item.balance
        assert str(found.balance.amount) == str(item.balance.amount)
        assert found.balance.currency is item.balance.currency
        assert found.limit == item.limit
        assert found.unit == item.unit
    assert loaded["h"].balance.amount == Decimal("99999999999999.99")
    assert loaded["f"].balance.amount == Decimal(1000)
    assert loaded["b"].limit is None


async def test_the_columns_hold_the_amount_and_the_code(tables: None) -> None:
    """Reports and other tools read an amount and a code, never a blob."""
    await store(wallet("a", money("12.34", "KWD"), money("5.00", "USD")))

    async with connections.engine().connect() as connection:
        row = (await connection.execute(select(wallets))).mappings().one()

    assert row["balance"] == Decimal("12.34")
    assert row["balance_currency"] is currency("KWD")
    assert row["limit"] == Decimal("5.00")
    assert row["limit_currency"] is currency("USD")


async def test_changes_and_removals_are_saved(tables: None) -> None:
    """A new Money replaces the old; None clears a missing-able one."""
    item = wallet("a", money("10.00", "USD"), money("1.00", "USD"))
    await store(item)

    async with WalletsUnitOfWork() as uow:
        found = await uow.wallets.get(item.id)
        found.balance = found.balance + money("0.01", "USD")
        found.limit = None
        await uow.commit()
    async with WalletsUnitOfWork() as uow:
        found = await uow.wallets.get(item.id)

    assert (found.balance, found.limit, found.version) == (
        money("10.01", "USD"),
        None,
        2,
    )


async def test_half_of_a_missing_amount_is_refused(tables: None) -> None:
    """The check keeps an amount and its currency together."""
    async with connections.engine().connect() as connection:
        with pytest.raises(IntegrityError):
            await connection.execute(
                insert(wallets).values(
                    id=uuid4(),
                    version=1,
                    name="x",
                    balance=Decimal(1),
                    balance_currency="USD",
                    limit=Decimal(1),
                    limit_currency=None,
                )
            )


async def test_an_unknown_code_is_refused_on_the_way_in(tables: None) -> None:
    """Codes are checked against the known currencies."""
    async with connections.engine().connect() as connection:
        with pytest.raises(StatementError) as caught:
            await connection.execute(
                insert(wallets).values(
                    id=uuid4(),
                    version=1,
                    name="x",
                    balance=Decimal(1),
                    balance_currency="XYZ",
                )
            )

    assert isinstance(caught.value.orig, UnknownCurrency)


async def test_sums_per_currency_in_one_query(tables: None) -> None:
    """GROUP BY the currency: exact totals, on SQLite too."""
    await store(*dataset())

    async with connections.engine().connect() as connection:
        rows = await connection.execute(
            select(wallets.c.balance_currency, func.sum(wallets.c.balance))
            .group_by(wallets.c.balance_currency)
            .order_by(wallets.c.balance_currency)
        )
        totals = {code.code: total for code, total in rows}

    assert totals == {
        "EUR": Decimal("10.00"),
        "JPY": Decimal(1000),
        "KWD": Decimal("1.234"),
        "USD": Decimal("100000000000017.48"),
    }


@pytest.mark.skipif(not ON_POSTGRES, reason=f"set {POSTGRES_VARIABLE}")
async def test_a_filter_on_currency_and_amount_uses_the_index(tables: None) -> None:
    """``(currency, amount)`` serves ``WHERE currency = ? AND amount >= ?``."""
    async with connections.engine().begin() as connection:
        await connection.execute(text("SET LOCAL enable_seqscan = off"))
        plan = await connection.execute(
            text(
                "EXPLAIN SELECT id FROM framework_test_money_wallets "
                "WHERE balance_currency = 'USD' AND balance >= 10 ORDER BY balance"
            )
        )
        lines = " ".join(row[0] for row in plan)

    assert "ix_framework_test_money_wallets_balance" in lines


CASES: list[tuple[Criteria, tuple[str, ...]]] = [
    (F("balance") == money("10", "USD"), ()),
    (F("balance") != money("10", "USD"), ("name",)),
    (F("balance") >= money("10", "USD"), ()),
    (F("balance") > money("9.99", "USD"), ()),
    (F("balance") < money("10", "EUR"), ()),
    (F("balance") <= money("10", "EUR"), ("-balance",)),
    (~(F("balance") > money("0", "USD")), ("balance",)),
    (F("balance").range(money("0", "USD"), money("10", "USD")), ("balance",)),
    (F("balance").in_([money("10", "USD"), money("10", "EUR")]), ("name",)),
    (F("balance").in_([]), ()),
    (F("balance.currency") == "USD", ("-balance",)),
    (F("balance.currency").in_(["EUR", currency("JPY")]), ("balance",)),
    (F("balance.currency") > "EUR", ("balance",)),
    (F("balance.amount") > Decimal("9.995"), ("balance",)),
    (F("balance.amount") >= 10, ("-balance", "name")),
    (F("limit") >= money("5", "USD"), ("limit",)),
    (F("limit") == None, ("name",)),  # noqa: E711 - a query, not a comparison.
    (F("limit").isnull(), ("-limit",)),
    (F("limit").isnull(False), ("limit", "-name")),
    (~(F("limit") > money("50", "USD")), ("limit",)),
    (F("limit.currency") == "EUR", ()),
    (~(F("limit.amount") > 1), ("-limit",)),
    (F("unit") == "USD", ("unit", "name")),
    (F("unit") > currency("EUR"), ("-unit",)),
    (F("unit").isnull(), ("unit",)),
]


@pytest.mark.parametrize(("criteria", "ordering"), CASES)
async def test_sql_answers_as_memory_does(
    tables: None, criteria: Criteria, ordering: tuple[str, ...]
) -> None:
    """Each condition and ordering, in the same order, in SQL and in memory."""
    items = dataset()
    await store(*items)
    memory = InMemoryWallets(items)
    query = Selection(criteria, ordering=ordering)

    async with WalletsUnitOfWork() as uow:
        found = await uow.wallets.filter(query)
        counted = await uow.wallets.count(criteria)

    expected = await memory.filter(query)
    assert names(found) == names(expected)
    assert counted == len(expected)


async def test_money_sorts_by_currency_then_amount(tables: None) -> None:
    """Ascending and descending; missing limits last, then first."""
    items = dataset()
    await store(*items)
    memory = InMemoryWallets(items)

    async with WalletsUnitOfWork() as uow:
        up = names(await uow.wallets.filter(Selection(ordering=["balance"])))
        down = names(await uow.wallets.filter(Selection(ordering=["-limit", "name"])))

    assert up == ["g", "c", "f", "e", "d", "b", "a", "h"]
    assert down == ["b", "f", "h", "a", "g", "e", "d", "c"]
    assert names(await memory.filter(Selection(ordering=["balance"]))) == up
    assert names(await memory.filter(Selection(ordering=["-limit", "name"]))) == down


async def walk(repository: Wallets | InMemoryWallets, query: Selection) -> list[str]:
    """The names of every page of three, from the first to the last."""
    walked: list[str] = []
    after: list[object] | None = None
    while True:
        page = await repository.paginate(query, limit=3, after=after)
        walked += names(page.items)
        if page.next_position is None:
            return walked
        after = list(page.next_position)


@pytest.mark.parametrize(
    "ordering", [("balance",), ("-balance",), ("limit",), ("-limit",)]
)
async def test_pages_follow_money_orderings(tables: None, ordering: tuple[str]) -> None:
    """Keyset pages of three walk the whole ordering, in SQL and in memory."""
    items = dataset()
    await store(*items)
    memory = InMemoryWallets(items)
    query = Selection(ordering=ordering)

    async with WalletsUnitOfWork() as uow:
        whole = names(await uow.wallets.filter(query))
        walked = await walk(uow.wallets, query)
    walked_in_memory = await walk(memory, query)

    assert walked == walked_in_memory == whole
    assert len(whole) == len(items)


async def test_update_rows_sets_money(tables: None) -> None:
    """Both columns change in the one statement; None clears a missing-able one."""
    items = dataset()
    await store(*items)

    async with WalletsUnitOfWork() as uow:
        held = await uow.wallets.get(items[0].id)
        changed = await uow.wallets.update_rows(
            F("balance.currency") == "USD", balance=money("1.00", "EUR"), limit=None
        )
        assert (held.balance, held.limit) == (money("1.00", "EUR"), None)
        await uow.commit()
    async with WalletsUnitOfWork() as uow:
        euros = await uow.wallets.filter(
            Selection(F("balance") == money("1", "EUR"), ordering=["name"])
        )

    assert changed == 4
    assert names(euros) == ["a", "b", "d", "h"]
    assert {each.limit for each in euros} == {None}
    assert {each.version for each in euros} == {2}


async def test_update_rows_refuses_what_is_not_money(tables: None) -> None:
    """The value is checked against the field."""
    async with WalletsUnitOfWork() as uow:
        with pytest.raises(InvalidQuery, match="holds Money"):
            await uow.wallets.update_rows(None, balance=Decimal(1))


@pytest.mark.parametrize(
    ("criteria", "error", "message"),
    [
        (F("balance") > Decimal(1), InvalidQuery, "holds Money, not Decimal"),
        (F("balance").contains("1"), InvalidQuery, "not text"),
        (
            F("balance").range(money("1", "USD"), money("2", "EUR")),
            InvalidQuery,
            "one currency",
        ),
        (F("balance.cents") == 1, InvalidQuery, "no child entities"),
        (F("balance.amount.x") == 1, InvalidQuery, "no child entities"),
        (F("balance.currency") == "XYZ", InvalidQuery, "known currency"),
        (F("balance.currency") == 1, InvalidQuery, "holds Currency, not int"),
        (F("balance.currency").contains("U"), InvalidQuery, "not text"),
        (F("unit") == "usd", InvalidQuery, "known currency"),
        (F("nothing") == 1, UnknownField, "no field"),
    ],
)
async def test_queries_that_do_not_fit_money_are_refused(
    criteria: Criteria, error: type[Exception], message: str
) -> None:
    """Values, lookups, and parts are checked against the field, before any SQL."""
    with pytest.raises(error, match=message):
        await InMemoryWallets().filter(criteria)


@dataclass
class Point:
    """A composite of a project's own: not money."""

    x: int
    y: int


class Plotted:
    """A class with a point."""

    id: int
    at: Point


def test_composites_of_other_classes_are_not_money() -> None:
    """Their attribute is no column to order by, as before money."""
    table = Table(
        "framework_test_money_plotted",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("x", Integer),
        Column("y", Integer),
    )
    map_imperatively(
        Plotted, table, properties={"at": composite(Point, table.c.x, table.c.y)}
    )

    with pytest.raises(UnknownField, match=r"Plotted\.at is not a mapped column"):
        compile_ordering((Ordering("at"),), Plotted)


@pytest.mark.usefixtures("database_access")
def test_makemigrations_writes_money_columns_that_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A revision of plain types: NUMERIC, a code of eight, and the check."""
    schema = MetaData(naming_convention=NAMING_CONVENTION)
    table_for(Wallet, "framework_test_money_purses", metadata=schema)
    monkeypatch.setattr(migrations_module, "metadata", schema)
    registry.clear()
    database = tmp_path / "purses.sqlite3"
    with override_settings(
        INSTALLED_DOMAINS=[ALPHA],
        DATABASES={"default": {"URL": f"sqlite+aiosqlite:///{database}"}},
        MIGRATIONS_DIR=tmp_path / "migrations",
    ):
        setup()
        call_command("makemigrations", "-m", "purses", stdout=io.StringIO())
        (revision,) = (tmp_path / "migrations").glob("*.py")
        call_command("migrate", stdout=io.StringIO())
        pending = migrations_module.pending_changes()
    registry.clear()
    assert pending is None  # The check of a pair made with its table is no change.
    text = revision.read_text()
    upgrade = text.split("def upgrade", 1)[1]
    for column in (
        '"balance", sa.Numeric(precision=18, scale=4), nullable=False',
        '"balance_currency", sa.String(length=8), nullable=False',
        '"limit", sa.Numeric(precision=12, scale=2), nullable=True',
        '"limit_currency", sa.String(length=8), nullable=True',
        '"unit", sa.String(length=8), nullable=True',
    ):
        assert f"sa.Column({column})" in upgrade
    assert "ck_framework_test_money_purses_limit_money" in text
    assert "domainkit" not in upgrade  # Revisions never import domain code.


def test_money_columns_name_their_parts_and_check_the_pair() -> None:
    """Made as a test runs: the names, the parts, nullability, and the check."""
    required = MoneyColumns("total")
    missing = MoneyColumns("tip", 12, 2, nullable=True)

    assert required.name == "total"
    assert (required.amount.name, required.currency.name) == ("total", "total_currency")
    assert required.amount.info == {"money": ("total", "amount")}
    assert required.currency.info == {"money": ("total", "currency")}
    assert (required.amount.nullable, required.currency.nullable) == (False, False)
    assert required.constraints == ()
    assert list(required) == [required.amount, required.currency]
    assert (missing.amount.nullable, missing.currency.nullable) == (True, True)
    assert missing.amount.type.scale == 2  # type: ignore[attr-defined]
    (check,) = missing.constraints
    assert check.name == "tip_money"
    assert str(check.sqltext.compile(compile_kwargs={"literal_binds": True})) == (
        "tip IS NULL AND tip_currency IS NULL OR "
        "tip IS NOT NULL AND tip_currency IS NOT NULL"
    )


class Till(AggregateRoot[int]):
    """An aggregate mapped while a test runs, so its mapping can be checked."""

    cash: Money
    change: Money | None = None


async def test_money_is_mapped_when_the_class_is(database: str) -> None:
    """Each pair is one attribute, its columns under private names."""
    schema = MetaData()
    tills = table_for(Till, "framework_test_money_tills", metadata=schema)
    map_imperatively(Till, tills)
    mapper = class_mapper(Till)

    assert list(mapper.composites.keys()) == ["cash", "change"]
    assert mapper.get_property_by_column(tills.c.cash).key == "_cash_amount"
    assert mapper.get_property_by_column(tills.c.change_currency).key == (
        "_change_currency"
    )
    async with connections.engine().begin() as connection:
        await connection.run_sync(schema.create_all)
    async with connections.session() as session:
        session.add(Till(id=1, cash=money("1.234", "KWD")))
        await session.commit()
    async with connections.session() as session:
        till = await session.get(Till, 1)
    await connections.close_all()

    assert till is not None
    assert (till.cash, till.change) == (money("1.234", "KWD"), None)


def test_fixtures_check_every_pair_of_a_row() -> None:
    """A pair left out does not stop the check of the next."""
    table = Table(
        "framework_test_money_rows",
        MetaData(),
        Column("id", Integer, primary_key=True),
        *MoneyColumns("tip", nullable=True),
        *MoneyColumns("total"),
    )
    row = {"id": 1, "tip": None, "total": "1.2345", "total_currency": "USD"}

    with pytest.raises(FixtureError) as caught:
        _decoded_row(table, row)

    assert str(caught.value) == (
        "framework_test_money_rows.total and total_currency hold no amount of "
        "money: both are needed, the amount with at most its currency's places."
    )


@pytest.mark.parametrize(
    ("criteria", "found"),
    [
        (F("balance") >= money("10", "USD"), ["a", "h"]),
        (F("balance") < money("10", "EUR"), ["g"]),
        (F("balance").in_([money("10", "USD"), money("10", "EUR")]), ["a", "c"]),
        (F("balance").range(money("0", "USD"), money("10", "USD")), ["a", "b"]),
        (F("balance").range(money("0", "EUR"), money("5", "EUR")), ["g"]),
        (~(F("balance") > money("0", "USD")), ["c", "d", "e", "f", "g"]),
        (F("limit") <= money("5", "EUR"), ["c"]),
    ],
)
async def test_money_conditions_select_these_wallets(
    criteria: Criteria, found: list[str]
) -> None:
    """What each condition on money selects, by name: its currency's amounts only."""
    memory = InMemoryWallets(dataset())

    assert names(await memory.filter(Selection(criteria, ordering=["name"]))) == found


class Purse(AggregateRoot[int]):
    """An aggregate whose mapping names one money attribute itself."""

    cash: Decimal
    coins: Money


def test_a_money_attribute_the_mapping_names_is_left_to_it() -> None:
    """Given properties win; the other pairs are mapped all the same."""
    purses = Table(
        "framework_test_money_purses_named",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("version", Integer, nullable=False),
        *MoneyColumns("cash"),
        *MoneyColumns("coins"),
    )
    map_imperatively(Purse, purses, properties={"cash": purses.c.cash})
    mapper = class_mapper(Purse)

    assert list(mapper.composites.keys()) == ["coins"]
    assert mapper.get_property_by_column(purses.c.cash).key == "cash"
    assert set(money_properties(purses, {"cash": None})) == {
        "_coins_amount",
        "_coins_currency",
        "coins",
    }


async def test_money_times_a_bound_rate_is_exact(tables: None) -> None:
    """``total * rate`` in SQL: the rate is a factor, not an amount (TEST-07)."""
    await store(wallet("a", money("9.95", "USD")))
    rate: Any = bindparam("rate")

    async with connections.engine().connect() as connection:
        times = await connection.scalar(
            select(wallets.c.balance * rate), {"rate": Decimal("0.1")}
        )
        over = await connection.scalar(
            select(wallets.c.balance / rate), {"rate": Decimal("4")}
        )

    assert (type(times), times, over) == (Decimal, Decimal("0.995"), Decimal("2.4875"))


@pytest.mark.parametrize(
    ("condition", "found"),
    [
        (lambda: Wallet.balance == money("10", "USD"), ["a"]),
        (lambda: Wallet.balance != money("10", "USD"), list("bcdefgh")),
        (lambda: Wallet.balance < money("10", "USD"), ["b", "d"]),
        (lambda: Wallet.balance <= money("10", "EUR"), ["c", "g"]),
        (lambda: Wallet.balance > money("1", "KWD"), ["e"]),
        (lambda: Wallet.balance >= money("1000", "JPY"), ["f"]),
        (lambda: Wallet.limit == None, ["b", "f", "h"]),  # noqa: E711 - IS NULL.
        (lambda: Wallet.limit != None, list("acdeg")),  # noqa: E711 - IS NOT NULL.
        (lambda: Wallet.limit != money("100", "USD"), list("bcdefh")),
    ],
)
async def test_the_money_attribute_compares_as_money(
    tables: None, condition: Callable[[], Any], found: list[str]
) -> None:
    """``Wallet.balance < m`` selects in m's currency, as Money compares (SEC-14a-2).

    Each condition is built in the test, not when the module loads.
    """
    await store(*dataset())

    async with WalletsUnitOfWork() as uow:
        rows = await uow.session.scalars(select(Wallet).where(condition()))
        assert sorted(each.name for each in rows) == found


def test_the_money_attribute_compares_only_with_money() -> None:
    """A number, or None in an ordering, is refused while the query is built."""
    with pytest.raises(TypeError, match="not int"):
        Wallet.balance < 10  # noqa: B015 - it raises.
    with pytest.raises(TypeError, match="not NoneType"):
        Wallet.limit < None  # type: ignore[operator]  # noqa: B015 - it raises.


def test_half_a_pair_loads_as_a_clear_error() -> None:
    """An amount without its currency, or the reverse, is no money, nor None."""
    load = class_mapper(Wallet).composites["limit"].composite_class

    assert load(None, None) is None
    assert load(Decimal("1.50"), currency("USD")) == money("1.50", "USD")
    with pytest.raises(IncompleteMoney, match=r"^A row holds an amount without its"):
        load(Decimal("1.50"), None)
    with pytest.raises(IncompleteMoney, match="money_check"):
        load(None, currency("USD"))


def test_a_given_amount_column_gets_the_pair_check() -> None:
    """An amount column given alone keeps the pair's check; one given is kept once."""
    amount: Column[Decimal] = Column(
        "limit", ExactDecimal(8, 2), nullable=True, info={"money": ("limit", "amount")}
    )
    table = table_for(
        Wallet, "framework_test_money_amount", amount, metadata=MetaData()
    )
    checks = [each for each in table.constraints if "money" in str(each.name)]
    given = MoneyColumns("limit", nullable=True)
    kept = table_for(
        Wallet, "framework_test_money_amount_checked", *given, metadata=MetaData()
    )
    (mine,) = given.constraints
    other = CheckConstraint("version >= 0", name="version_counts")
    sized = table_for(
        Wallet,
        "framework_test_money_amount_other",
        Column(
            "limit",
            ExactDecimal(8, 2),
            nullable=True,
            info={"money": ("limit", "amount")},
        ),
        other,
        metadata=MetaData(),
    )

    assert [each.name for each in checks] == ["limit_money"]
    assert "limit_currency IS NULL" in str(checks[0].sqltext)  # type: ignore[attr-defined]
    assert [each for each in kept.constraints if "money" in str(each.name)] == [mine]
    assert {str(each.name) for each in sized.constraints} >= {
        "version_counts",
        "limit_money",
    }
