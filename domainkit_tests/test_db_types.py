"""``ExactDecimal`` keeps amounts exact on SQLite and PostgreSQL alike."""

import re
from collections.abc import AsyncIterator, Callable, Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    bindparam,
    func,
    insert,
    literal,
    select,
    update,
)
from sqlalchemy.dialects import sqlite
from sqlalchemy.engine import make_url
from sqlalchemy.exc import StatementError
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.schema import CreateTable
from sqlalchemy.sql.elements import BindParameter

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
    # Products keep the places PostgreSQL gives them, whole factors included.
    forms = [
        (amount * 2, "19.90"),
        (3 * amount, "29.85"),
        (amount * prices.c.quantity, "29.85"),
        (amount * Decimal("1.5"), "14.925"),
    ]
    for expression, form in forms:
        assert [
            str(value)
            for value in await scalars(engine, select(expression).where(first))
        ] == [form]
    assert await scalars(
        engine, select(func.sum(amount * prices.c.quantity)).where(prices.c.id < 3)
    ) == [Decimal("30.55")]
    assert await scalars(engine, select(func.min(amount)).where(prices.c.id < 3)) == [
        Decimal("0.10")
    ]
    assert await scalars(engine, select(func.max(amount)).where(prices.c.id < 3)) == [
        Decimal("9.95")
    ]


async def test_fractional_products_come_back_in_their_shortest_form(
    engine: AsyncEngine,
) -> None:
    """3.33 * 0.7 is 2.331; SQLite computes 2.33099999..., read back as 2.331."""
    await stored(engine, {"id": 1, "amount": Decimal("3.33")})
    amount = prices.c.amount

    products = select(amount * Decimal("0.7"), Decimal("0.7") * amount)
    async with engine.connect() as connection:
        found = (await connection.execute(products)).one()

    assert tuple(found) == (Decimal("2.331"), Decimal("2.331"))


async def test_factors_are_numbers_however_they_are_given(engine: AsyncEngine) -> None:
    """A bound, literal, or integer-column factor or divisor is never an amount.

    A bound parameter took the amount's type, and so was counted in minor
    units on SQLite: 9.95 times a bound 0.1 was 9950.0000 there (TEST-07).
    """
    await stored(
        engine,
        {"id": 1, "amount": Decimal("9.95"), "quantity": 4, "rate": Decimal("0.0500")},
        {"id": 2, "amount": Decimal("0.03"), "quantity": 3, "rate": Decimal("0.0001")},
    )
    amount, quantity, rate = prices.c.amount, prices.c.quantity, prices.c.rate
    factor: BindParameter[Any] = bindparam("factor")
    cases: list[tuple[Any, dict[str, Any], Decimal]] = [
        (amount * factor, {"factor": Decimal("0.1")}, Decimal("0.995")),
        (Decimal("0.1") * amount, {}, Decimal("0.995")),
        (amount * factor, {"factor": 3}, Decimal("29.85")),
        (amount / factor, {"factor": Decimal("4")}, Decimal("2.4875")),
        (amount / factor, {"factor": Decimal("0.5")}, Decimal("19.9")),
        (amount * Decimal("0.1"), {}, Decimal("0.995")),
        (amount / Decimal("0.25"), {}, Decimal("39.8")),
        (amount * quantity, {}, Decimal("39.80")),
        (amount / quantity, {}, Decimal("2.4875")),
        (amount + rate, {}, Decimal("10.0000")),
        (amount - rate, {}, Decimal("9.9000")),
        (amount + factor, {"factor": Decimal("0.05")}, Decimal("10.00")),
        (amount - factor, {"factor": Decimal("0.05")}, Decimal("9.90")),
    ]
    async with engine.connect() as connection:
        for expression, parameters, expected in cases:
            found = await connection.scalar(
                select(expression).where(prices.c.id == 1), parameters
            )
            assert type(found) is Decimal, expression
            assert found == expected, expression
        # Three cents times 0.1: no binary fraction of 0.1 shows (0.0030000...4).
        tenth = await connection.scalar(
            select(amount * factor).where(prices.c.id == 2), {"factor": Decimal("0.1")}
        )
        third = await connection.scalar(
            select(amount / quantity).where(prices.c.id == 2)
        )
        given = await connection.scalar(
            select(amount * Decimal("0.1")).where(prices.c.id == 2)
        )
    assert (type(tenth), tenth) == (Decimal, Decimal("0.003"))
    assert (type(given), given) == (Decimal, Decimal("0.003"))
    assert type(third) is Decimal
    assert abs(third - Decimal("0.01")) < Decimal("1E-15")


async def test_an_amount_times_or_over_an_amount_is_refused_as_it_is_built(
    engine: AsyncEngine,
) -> None:
    """Two amounts multiplied or divided mean nothing for money: a ``TypeError``.

    On SQLite both sides counted in minor units: 9.95 times a parameter
    typed as the amount, of 2, was 1990.00, and 19.90 on PostgreSQL
    (TEST-11). Refused when the expression is built, it is the same on
    both. Sums, differences, and factors given as numbers still work.
    """
    await stored(engine, {"id": 1, "amount": Decimal("9.95"), "quantity": 2})
    amount, rate, units = prices.c.amount, prices.c.rate, prices.c.units
    typed: BindParameter[Any] = bindparam("x", type_=amount.type)
    builds: list[Callable[[], object]] = [
        lambda: amount * typed,
        lambda: amount / typed,
        lambda: typed * amount,
        lambda: amount * rate,
        lambda: rate / amount,
        lambda: amount * units,
        lambda: amount * amount,
        lambda: amount * literal(Decimal(2), ExactDecimal(18, 2)),
    ]

    for build in builds:
        with pytest.raises(TypeError, match="a plain Decimal or an untyped bindparam"):
            build()
    sums = select(
        amount + typed,
        amount - typed,
        amount * bindparam("y"),
        amount * 2,
        amount * prices.c.quantity,
        amount / Decimal(2),
    ).where(prices.c.id == 1)
    async with engine.connect() as connection:
        found = (await connection.execute(sums, {"x": Decimal("0.05"), "y": 2})).one()
    assert tuple(found) == (
        Decimal("10.00"),
        Decimal("9.90"),
        Decimal("19.90"),
        Decimal("19.90"),
        Decimal("19.90"),
        Decimal("4.975"),
    )


async def test_a_factor_has_at_most_nine_places(engine: AsyncEngine) -> None:
    """A finer factor is refused rather than rounded, a float rather than taken."""
    await stored(engine, {"id": 1, "amount": Decimal("9.95")})
    product = select(prices.c.amount * bindparam("factor"))

    for factor, error, message in (
        (
            Decimal("0.0000000001"),
            ValueError,
            (
                "A factor of an amount has at most 9 decimal places in SQL; round "
                "it first, or compute in Python."
            ),
        ),
        (0.1, TypeError, "ExactDecimal takes a Decimal or an int, not float"),
    ):
        with pytest.raises(StatementError) as refused:
            await scalars_with(engine, product, {"factor": factor})
        assert isinstance(refused.value.orig, error)
        assert str(refused.value.orig).startswith(message)
    assert await scalars_with(engine, product, {"factor": Decimal("0.000000001")}) == [
        Decimal("0.00000000995")
    ]


async def scalars_with(
    engine: AsyncEngine,
    query: Any,  # noqa: ANN401 - a query.
    parameters: dict[str, object],
) -> list[object]:
    """The first column of every row ``query`` returns, with ``parameters``."""
    async with engine.connect() as connection:
        return list((await connection.execute(query, parameters)).scalars())


async def test_a_missing_factor_gives_a_missing_amount(engine: AsyncEngine) -> None:
    """``amount * None`` and ``amount / None`` are NULL, as in SQL."""
    await stored(engine, {"id": 1, "amount": Decimal("9.95")})
    amount = prices.c.amount

    found = await scalars(engine, select(amount * None))
    divided = await scalars(engine, select(amount / None))

    assert (found, divided) == ([None], [None])


async def test_updates_go_through_the_type(engine: AsyncEngine) -> None:
    """Adding an amount and multiplying by a whole number stay exact."""
    await stored(engine, {"id": 1, "amount": Decimal("10.05")})

    async with engine.begin() as connection:
        await connection.execute(
            update(prices).values(amount=prices.c.amount + Decimal("0.95"))
        )
    raised = await scalars(engine, select(prices.c.amount))
    async with engine.begin() as connection:
        await connection.execute(update(prices).values(amount=prices.c.amount * 3))

    assert raised == [Decimal("11.00")]
    assert [str(value) for value in await scalars(engine, select(prices.c.amount))] == [
        "33.00"
    ]


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
    """Nothing is rounded or truncated on the way in; queries refuse non-numbers."""
    with pytest.raises(StatementError) as written:
        await stored(engine, {"id": 1, "amount": value})

    assert isinstance(written.value.orig, error)
    assert re.search(message, str(written.value.orig))
    if isinstance(value, Decimal) and value.is_finite():
        return  # Compared exactly instead: see the next test.
    with pytest.raises(StatementError) as compared:
        await scalars(engine, select(prices.c.id).where(prices.c.amount == value))
    assert isinstance(compared.value.orig, error)
    assert re.search(message, str(compared.value.orig))


async def test_comparisons_take_values_of_any_places(engine: AsyncEngine) -> None:
    """A value the column could not hold is compared exactly, not refused."""
    await stored(
        engine,
        {"id": 1, "amount": Decimal("9.95")},
        {"id": 2, "amount": Decimal("9.96")},
        {"id": 3, "amount": -LARGEST},
    )
    amount = prices.c.amount
    beyond = Decimal("1E+30")
    queries = [
        (amount > Decimal("9.955"), [2]),
        (amount >= Decimal("9.955"), [2]),
        (amount < Decimal("9.955"), [1, 3]),
        (amount <= Decimal("9.955"), [1, 3]),
        (amount > Decimal("9.949"), [1, 2]),
        (amount == Decimal("9.950"), [1]),
        (amount == Decimal("9.9500001"), []),
        (amount != Decimal("9.9500001"), [1, 2, 3]),
        (amount.between(Decimal("9.9450"), Decimal("9.9599")), [1]),
        (amount.in_([Decimal("9.951"), Decimal("9.96")]), [2]),
        (amount.not_in([Decimal("9.951")]), [1, 2, 3]),
        (amount.in_([None, Decimal("9.96")]), [2]),  # None matches no amount.
        (amount < beyond, [1, 2, 3]),
        (amount > -beyond, [1, 2, 3]),
        (amount == beyond, []),
        (amount > Decimal(2**70), []),
        (amount == -LARGEST, [3]),  # Eighteen digits, sent whole.
        (amount < Decimal(2**63).scaleb(-2), [1, 2, 3]),  # Just past 64 bits.
    ]
    for condition, expected in queries:
        found = await scalars(
            engine, select(prices.c.id).where(condition).order_by("id")
        )
        assert found == expected, condition
    nearly = Decimal("9.95000000000000000001")
    if engine.dialect.name == "sqlite":
        with pytest.raises(StatementError, match="too close to a whole number"):
            await scalars(engine, select(prices.c.id).where(amount > nearly))
    else:
        assert await scalars(engine, select(prices.c.id).where(amount > nearly)) == [2]


async def test_amounts_meet_other_scales_and_integers_exactly(
    engine: AsyncEngine,
) -> None:
    """Both sides count the same units, whatever scale each column has."""
    await stored(
        engine,
        {
            "id": 1,
            "amount": Decimal("2.50"),
            "rate": Decimal("0.1234"),
            "units": Decimal("10"),
            "quantity": 3,
        },
    )
    amount, rate, units, quantity = (
        prices.c.amount,
        prices.c.rate,
        prices.c.units,
        prices.c.quantity,
    )
    computed = [
        (amount + rate, "2.6234"),
        (rate + amount, "2.6234"),
        (amount - units, "-7.50"),
        (units - amount, "7.50"),
        (amount + quantity, "5.50"),
        (amount - quantity, "-0.50"),
        (amount % quantity, "2.50"),
        (units % amount, "0.00"),
    ]
    for expression, form in computed:
        found = await scalars(engine, select(expression))
        assert [str(value) for value in found] == [form], expression
    conditions = [
        (amount > rate, True),
        (rate > amount, False),
        (amount < units, True),
        (amount < quantity, True),
        (amount >= quantity, False),
        (amount == rate, False),
        (amount != units, True),
    ]
    for condition, expected in conditions:
        assert await scalars(engine, select(condition)) == [expected], condition


def test_only_amounts_and_integers_are_rescaled() -> None:
    """Other columns meet the amount as they are, as before."""
    other = Table(
        "framework_test_other",
        MetaData(),
        Column("ratio", Numeric(5, 2)),
        Column("label", String(10)),
    )
    amount = prices.c.amount
    dialect = sqlite.dialect()

    for expression, sql in [
        (
            amount > other.c.ratio,
            "framework_test_prices.amount > framework_test_other.ratio",
        ),
        (
            amount == other.c.label,
            "framework_test_prices.amount = framework_test_other.label",
        ),
        (
            amount > prices.c.quantity,
            "framework_test_prices.amount > (framework_test_prices.quantity * 100)",
        ),
    ]:
        assert str(expression.compile(dialect=dialect)) == sql


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
    refused = (
        r"^Floor-divide ExactDecimal amounts in Python: on SQLite the column "
        r"holds minor units, and the result would differ\.$"
    )
    with pytest.raises(TypeError, match=refused):
        _ = prices.c.amount // 2


def test_tables_declare_numeric_on_every_database() -> None:
    """``create_all`` declares the column's digits on SQLite and PostgreSQL."""
    postgres = make_url("postgresql+asyncpg://").get_dialect()()
    for dialect in (sqlite.dialect(), postgres):
        ddl = str(CreateTable(prices).compile(dialect=dialect))
        assert "amount NUMERIC(18, 2)" in ddl
        assert "units NUMERIC(10, 0)" in ddl
        assert "rate NUMERIC(4, 4)" in ddl


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
