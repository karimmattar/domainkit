"""SEC-14a-1: a money condition through child entities mixes currencies.

``F("lines.price") >= Money.of("100", "USD")`` is bound into two
conditions, on ``lines.price.currency`` and on ``lines.price.amount``,
and each one goes through the collection on its own (two ``EXISTS``, two
``any()``). An order whose only line of 100 or more is in EUR, and which
has some line in USD, matches "a line of at least 100 USD": the amount of
one line is compared under the currency of another (docs/money.md: money
compares only within its currency, "for no total in another currency").
"""

from collections.abc import AsyncIterator, Iterator
from dataclasses import field
from typing import NewType
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Column, ForeignKey, MetaData
from sqlalchemy.ext.asyncio import AsyncSession

from domainkit.conf import override_settings
from domainkit.db import (
    SqlAlchemyRepository,
    SqlAlchemyUnitOfWork,
    children,
    connections,
    map_imperatively,
    table_for,
)
from domainkit.ddd import AggregateRoot, Entity, InMemoryRepository
from domainkit.ddd.query import F
from domainkit.http.filters import Filter, FilterSet
from domainkit.money import Money

pytestmark = pytest.mark.anyio

OrderId = NewType("OrderId", UUID)
LineId = NewType("LineId", UUID)


class Line(Entity[LineId]):
    """A line of an order, priced in money."""

    price: Money


class Order(AggregateRoot[OrderId]):
    """An order of lines, which may be in several currencies."""

    lines: list[Line] = field(default_factory=list)


metadata = MetaData()
orders = table_for(Order, "framework_test_sec14a_orders", metadata=metadata)
lines = table_for(
    Line,
    "framework_test_sec14a_lines",
    Column(
        "order_id",
        ForeignKey("framework_test_sec14a_orders.id", ondelete="CASCADE"),
        nullable=False,
    ),
    metadata=metadata,
)
map_imperatively(Line, lines)
map_imperatively(Order, orders, properties={"lines": children(Line)})


class Orders(SqlAlchemyRepository[Order, OrderId]):
    """Orders in SQL."""


class InMemoryOrders(InMemoryRepository[Order, OrderId]):
    """Orders in memory."""


class OrdersUnitOfWork(SqlAlchemyUnitOfWork):
    """One transaction over the orders."""

    orders: Orders

    def create_repositories(self, session: AsyncSession) -> None:
        """Bind the repository."""
        self.orders = Orders(session)


def mixed() -> Order:
    """An order of 1.00 USD and 1000.00 EUR: no line of 100 USD or more."""
    return Order(
        id=OrderId(uuid4()),
        lines=[
            Line(id=LineId(uuid4()), price=Money.of("1", "USD")),
            Line(id=LineId(uuid4()), price=Money.of("1000", "EUR")),
        ],
    )


AT_LEAST_100_USD = F("lines.price") >= Money.of("100", "USD")
UNDER_10_EUR = F("lines.price") < Money.of("10", "EUR")


@pytest.fixture
def database(database_url: str) -> Iterator[str]:
    """An empty database, selected in the settings."""
    with override_settings(DATABASES={"default": {"URL": database_url}}):
        yield database_url


@pytest.fixture
async def stored(database: str) -> AsyncIterator[Order]:
    """The mixed order, committed."""
    async with connections.engine().begin() as connection:
        await connection.run_sync(metadata.create_all)
    order = mixed()
    async with OrdersUnitOfWork() as uow:
        await uow.orders.add(order)
        await uow.commit()
    yield order
    await connections.close_all()


async def test_in_memory_a_line_in_eur_is_no_line_of_100_usd() -> None:
    """No line is 100 USD or more, nor under 10 EUR: neither condition holds."""
    repository = InMemoryOrders()
    await repository.add(mixed())

    assert await repository.filter(AT_LEAST_100_USD) == []
    assert await repository.filter(UNDER_10_EUR) == []


async def test_in_sql_a_line_in_eur_is_no_line_of_100_usd(stored: Order) -> None:
    """The same in SQL: the amount and its currency are of one line."""
    async with OrdersUnitOfWork() as uow:
        found = await uow.orders.filter(AT_LEAST_100_USD)
        cheap = await uow.orders.filter(UNDER_10_EUR)

    assert found == []
    assert cheap == []


async def test_a_filter_set_compares_one_lines_amount_in_its_currency() -> None:
    """``?line_price__gte=100&line_price_currency=USD`` finds no line of 100 USD."""

    class OrderFilters(FilterSet):
        """A money filter through the lines."""

        model = Order
        line_price = Filter("gte", field="lines.price")

    repository = InMemoryOrders()
    await repository.add(mixed())
    filters = OrderFilters(line_price__gte=100, line_price_currency="USD")

    assert await repository.filter(filters.query()) == []
