"""SEC-16c-1: on SQLite, a check written as SQL compares amounts as floats.

DATA-15's fix rewrites each amount column in a SQL check as
``(name / 100.0)``: a REAL. Two amounts of the same scale were compared as
exact integers (minor units) before; they are now compared as binary
floats, so a check refuses rows PostgreSQL keeps (0.10 + 0.20 = 0.30), and
past 2**53 minor units lets an amount one minor unit over its bound pass.
"""

from collections.abc import Iterator
from decimal import Decimal

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, insert
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CheckConstraint

from domainkit.db import ExactDecimal


@pytest.fixture
def engine() -> Iterator[Engine]:
    """An in-memory SQLite engine."""
    engine = create_engine("sqlite://")
    yield engine
    engine.dispose()


def test_a_check_that_sums_amounts_keeps_an_exact_sum(engine: Engine) -> None:
    """0.10 + 0.20 = 0.30 holds on PostgreSQL; on SQLite the floats differ."""
    metadata = MetaData()
    lines = Table(
        "sec16c_1_lines",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("subtotal", ExactDecimal(12, 2), nullable=False),
        Column("tax", ExactDecimal(12, 2), nullable=False),
        Column("total", ExactDecimal(12, 2), nullable=False),
        CheckConstraint("subtotal + tax = total", name="sec16c_1_balanced"),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        # 0.10 + 0.20 is 0.30: PostgreSQL keeps the row, as SQLite did on
        # minor units (10 + 20 = 30) before the rewrite.
        connection.execute(
            insert(lines).values(
                id=1,
                subtotal=Decimal("0.10"),
                tax=Decimal("0.20"),
                total=Decimal("0.30"),
            )
        )


def test_a_check_between_amounts_refuses_one_minor_unit_over(engine: Engine) -> None:
    """Past 2**53 minor units, one more is the same float: the check passes."""
    metadata = MetaData()
    refunds = Table(
        "sec16c_1_refunds",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("paid", ExactDecimal(18, 2), nullable=False),
        Column("refunded", ExactDecimal(18, 2), nullable=False),
        CheckConstraint("refunded <= paid", name="sec16c_1_not_over"),
    )
    metadata.create_all(engine)
    paid = Decimal(2**53).scaleb(-2)  # 90071992547409.92
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            insert(refunds).values(id=1, paid=paid, refunded=paid + Decimal("0.01"))
        )
