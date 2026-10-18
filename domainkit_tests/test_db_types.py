"""``ExactDecimal`` keeps amounts exact on SQLite and PostgreSQL alike."""

import re
from collections.abc import AsyncIterator, Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    Table,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.exc import StatementError
from sqlalchemy.ext.asyncio import AsyncEngine

from domainkit.conf import override_settings
from domainkit.db import ExactDecimal, connections
from domainkit.db import migrations as migrations_module
from domainkit.db.migrations import (
    make_migration,
    pending_changes,
    render_item,
    upgrade,
)

pytestmark = pytest.mark.anyio

metadata = MetaData()
prices = Table(
    "framework_test_prices",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("amount", ExactDecimal(18, 2)),
    Column("quantity", Integer),
    Column("units", ExactDecimal(10, 0)),
    Column("rate", ExactDecimal(4, 4)),
)
LARGEST = Decimal("9999999999999999.99")
"""The largest ``ExactDecimal(18, 2)``: SQLite's floats keep 15 to 17 digits."""


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    """An engine on an empty database, with the prices table created."""
    with override_settings(DATABASES={"default": {"URL": database_url}}):
        engine = connections.engine()
        async with engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
        yield engine
        await connections.close_all()


async def stored(engine: AsyncEngine, *rows: dict[str, object]) -> None:
    """Insert ``rows`` into the prices table."""
    async with engine.begin() as connection:
        await connection.execute(insert(prices), list(rows))


async def scalars(engine: AsyncEngine, query: object) -> list[object]:
    """The first column of every row ``query`` returns."""
    async with engine.connect() as connection:
        return list((await connection.execute(query)).scalars())  # type: ignore[call-overload]


async def test_amounts_come_back_exactly(engine: AsyncEngine) -> None:
    """Eighteen digits, negatives, zero, and whole numbers, with their places."""
    await stored(
        engine,
        {"id": 1, "amount": LARGEST, "units": Decimal("9999999999"), "rate": None},
        {"id": 2, "amount": -LARGEST, "units": -1, "rate": Decimal("0.9999")},
        {"id": 3, "amount": Decimal("-0.01"), "units": 0, "rate": Decimal("0.0001")},
        {"id": 4, "amount": 5, "units": None, "rate": 0},
        {"id": 5, "amount": Decimal("1.500"), "units": None, "rate": None},
        {"id": 6, "amount": None, "units": None, "rate": None},
    )

    query = select(prices.c.amount, prices.c.units, prices.c.rate)
    async with engine.connect() as connection:
        rows = [tuple(row) for row in await connection.execute(query.order_by("id"))]

    assert rows == [
        (LARGEST, Decimal("9999999999"), None),
        (-LARGEST, Decimal("-1"), Decimal("0.9999")),
        (Decimal("-0.01"), Decimal("0"), Decimal("0.0001")),
        (Decimal("5.00"), None, Decimal("0.0000")),
        (Decimal("1.50"), None, None),
        (None, None, None),
    ]
    missing = select(prices.c.amount * Decimal("1.5"), prices.c.amount / 2)
    async with engine.connect() as connection:
        computed = (await connection.execute(missing.where(prices.c.id == 6))).one()
    assert tuple(computed) == (None, None)
    assert [str(row[0]) for row in rows[:5]] == [
        "9999999999999999.99",
        "-9999999999999999.99",
        "-0.01",
        "5.00",
        "1.50",
    ]


async def test_queries_compute_the_same_on_every_database(engine: AsyncEngine) -> None:
    """Comparisons, order, sums, and arithmetic with plain numbers are exact.

    On SQLite the column holds cents, so a factor or divisor must stay a
    plain number, and a quotient must not be truncated.
    """
    await stored(
        engine,
        {"id": 1, "amount": Decimal("9.95"), "quantity": 3},
        {"id": 2, "amount": Decimal("0.10"), "quantity": 7},
        {"id": 3, "amount": LARGEST, "quantity": 1},
        {"id": 4, "amount": Decimal("-2.50"), "quantity": 2},
    )
    amount = prices.c.amount
    first = prices.c.id == 1

    assert await scalars(engine, select(amount).order_by(amount)) == [
        Decimal("-2.50"),
        Decimal("0.10"),
        Decimal("9.95"),
        LARGEST,
    ]
    assert await scalars(
        engine, select(prices.c.id).where(amount > Decimal("9.94")).order_by("id")
    ) == [1, 3]
    assert await scalars(
        engine, select(prices.c.id).where(amount.between(0, Decimal("9.95")))
    ) == [1, 2]
    assert await scalars(
        engine, select(prices.c.id).where(amount.in_([Decimal("0.1"), 10]))
    ) == [2]
    assert await scalars(engine, select(prices.c.id).where(amount == 0)) == []
    computed = [
        (amount * 2, Decimal("19.90")),
        (3 * amount, Decimal("29.85")),
        (amount * Decimal("1.5"), Decimal("14.925")),
        (Decimal("0.5") * amount, Decimal("4.975")),
        (amount / 2, Decimal("4.975")),
        (amount / Decimal("4"), Decimal("2.4875")),
        (amount + 1, Decimal("10.95")),
        (amount - Decimal("0.05"), Decimal("9.90")),
        (amount % 2, Decimal("1.95")),
        (amount * prices.c.quantity, Decimal("29.85")),
        (prices.c.quantity * amount, Decimal("29.85")),
        (-amount, Decimal("-9.95")),
        (func.avg(amount, type_=amount.type), Decimal("9.95")),
    ]
    for expression, expected in computed:
        assert await scalars(engine, select(expression).where(first)) == [expected]
    assert await scalars(
        engine, select(func.sum(amount * prices.c.quantity)).where(prices.c.id < 3)
    ) == [Decimal("30.55")]
    assert await scalars(engine, select(func.min(amount)).where(prices.c.id < 3)) == [
        Decimal("0.10")
    ]
    assert await scalars(engine, select(func.max(amount)).where(prices.c.id < 3)) == [
        Decimal("9.95")
    ]


async def test_updates_go_through_the_type(engine: AsyncEngine) -> None:
    """A raise by a fixed amount, and one by a percentage, stay exact."""
    await stored(engine, {"id": 1, "amount": Decimal("10.00")})

    async with engine.begin() as connection:
        await connection.execute(update(prices).values(amount=prices.c.amount + 1))
    raised = await scalars(engine, select(prices.c.amount))
    async with engine.begin() as connection:
        await connection.execute(
            update(prices).values(amount=prices.c.amount * Decimal("1.1"))
        )

    assert raised == [Decimal("11.00")]
    assert await scalars(engine, select(prices.c.amount)) == [Decimal("12.10")]


@pytest.mark.parametrize(
    ("value", "error", "message"),
    [
        (0.1, TypeError, r"not float \(floats are inexact\)"),
        (True, TypeError, r"not bool"),
        ("1.00", TypeError, r"not str"),
        (Decimal("NaN"), ValueError, r"cannot hold NaN"),
        (Decimal("-Infinity"), ValueError, r"cannot hold -Infinity"),
        (Decimal("1.005"), ValueError, r"^1\.005 has more than 2 decimal places"),
        (Decimal("1E-30"), ValueError, r"has more than 2 decimal places"),
        (
            Decimal("10000000000000000"),
            ValueError,
            r"^10000000000000000 has more than 16 digits before the point",
        ),
        (-(LARGEST + Decimal("0.01")), ValueError, r"more than 16 digits"),
    ],
)
async def test_values_that_would_round_are_refused(
    engine: AsyncEngine, value: object, error: type[Exception], message: str
) -> None:
    """Nothing is rounded or truncated on the way in, in writes or in queries."""
    with pytest.raises(StatementError) as written:
        await stored(engine, {"id": 1, "amount": value})
    with pytest.raises(StatementError) as compared:
        await scalars(engine, select(prices.c.id).where(prices.c.amount == value))

    for caught in (written.value, compared.value):
        assert isinstance(caught.orig, error)
        assert re.search(message, str(caught.orig))


async def test_the_bounds_of_a_column_are_inclusive(engine: AsyncEngine) -> None:
    """``ExactDecimal(4, 4)`` holds 0.9999 and zero, but not 1."""
    await stored(engine, {"id": 1, "rate": Decimal("0.9999")})

    with pytest.raises(StatementError) as caught:
        await stored(engine, {"id": 2, "rate": 1})

    assert str(caught.value.orig) == "1 has more than 0 digits before the point."


@pytest.mark.parametrize(("precision", "scale"), [(19, 2), (0, 0), (2, 3), (5, -1)])
def test_columns_must_fit_sqlite_integers(precision: int, scale: int) -> None:
    """At most 18 digits, any 18 of which fit in a 64-bit integer."""
    with pytest.raises(ValueError, match=r"^ExactDecimal needs 1 <= precision <= 18"):
        ExactDecimal(precision, scale)

    assert ExactDecimal(18, 18).scale == 18
    assert ExactDecimal(1, 0).precision == 1


def test_floor_division_is_refused() -> None:
    """Its result is a count, not an amount; SQLite would give cents."""
    with pytest.raises(
        TypeError, match=r"^Floor-divide ExactDecimal amounts in Python"
    ):
        _ = prices.c.amount // 2


def test_revisions_store_amounts_as_numeric() -> None:
    """A revision names the storage type, not domainkit's."""
    rendered = render_item("type", ExactDecimal(18, 2), None)  # type: ignore[arg-type]

    assert rendered == "sa.Numeric(precision=18, scale=2)"


@pytest.fixture
def prices_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, database_url: str
) -> Iterator[None]:
    """Migrations of the prices table into a temporary directory and database."""
    monkeypatch.setattr(migrations_module, "metadata", metadata)
    with override_settings(
        DATABASES={"default": {"URL": database_url}},
        MIGRATIONS_DIR=tmp_path / "migrations",
    ):
        yield


def test_migrated_columns_match_the_models(prices_project: None) -> None:
    """After migrating, the columns need no further revision on either database."""
    path = make_migration("create prices")
    upgrade()

    assert path is not None
    assert "sa.Numeric(precision=18, scale=2)" in path.read_text(encoding="utf-8")
    assert pending_changes() is None
