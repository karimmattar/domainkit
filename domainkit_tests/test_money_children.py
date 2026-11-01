"""Money through child entities: the amount and its currency are of one child.

``F("items.price") >= Money.of("100", "USD")`` holds when one item is
priced in USD at 100 or more (``Within``), in memory and in SQL alike, and
so does a filter set's money filter through the items (SEC-14a-1).
"""

from collections.abc import AsyncIterator, Iterator
from dataclasses import field
from decimal import Decimal
from typing import Annotated, Any, NewType
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Column, ForeignKey, MetaData
from sqlalchemy.ext.asyncio import AsyncSession

from domainkit.conf import override_settings
from domainkit.db import (
    SqlAlchemyRepository,
    SqlAlchemyUnitOfWork,
    child,
    children,
    connections,
    map_imperatively,
    table_for,
)
from domainkit.ddd import (
    AggregateRoot,
    Entity,
    InMemoryRepository,
    InvalidQuery,
    MaxLength,
    UnknownField,
)
from domainkit.ddd.query import Criteria, F, Selection, Within
from domainkit.http.filters import Filter, FilterSet, InvalidFilter
from domainkit.money import Money, currency

pytestmark = pytest.mark.anyio

BasketId = NewType("BasketId", UUID)
ItemId = NewType("ItemId", UUID)
ExtraId = NewType("ExtraId", UUID)
CourierId = NewType("CourierId", UUID)


class Extra(Entity[ExtraId]):
    """Something added to an item, at a cost."""

    cost: Money


class Item(Entity[ItemId]):
    """An item of a basket, priced in money."""

    price: Money
    extras: list[Extra] = field(default_factory=list)


class Courier(Entity[CourierId]):
    """Who delivers a basket, for a fee."""

    fee: Money


class Basket(AggregateRoot[BasketId]):
    """Items in any currencies, and maybe a courier."""

    name: Annotated[str, MaxLength(10)]
    items: list[Item] = field(default_factory=list)
    courier: Courier | None = None


def _owned(name: str, owner: str) -> Column[UUID]:
    """The column of a child's owner."""
    return Column(name, ForeignKey(f"{owner}.id", ondelete="CASCADE"), nullable=False)


metadata = MetaData()
BASKETS = "framework_test_money_baskets"
ITEMS = "framework_test_money_basket_items"
baskets = table_for(Basket, BASKETS, metadata=metadata)
items = table_for(Item, ITEMS, _owned("basket_id", BASKETS), metadata=metadata)
extras = table_for(
    Extra,
    "framework_test_money_basket_extras",
    _owned("item_id", ITEMS),
    metadata=metadata,
)
couriers = table_for(
    Courier,
    "framework_test_money_basket_couriers",
    _owned("basket_id", BASKETS),
    metadata=metadata,
)
map_imperatively(Extra, extras)
map_imperatively(Item, items, properties={"extras": children(Extra)})
map_imperatively(Courier, couriers)
map_imperatively(
    Basket,
    baskets,
    properties={"items": children(Item), "courier": child(Courier)},
)


class Baskets(SqlAlchemyRepository[Basket, BasketId]):
    """Baskets in SQL."""


class BasketsUnitOfWork(SqlAlchemyUnitOfWork):
    """One transaction over the baskets."""

    baskets: Baskets

    def create_repositories(self, session: AsyncSession) -> None:
        """Bind the repository."""
        self.baskets = Baskets(session)


def money(amount: str, code: str) -> Money:
    """``amount`` of ``code``."""
    return Money.of(amount, code)


def item(price: Money, *costs: Money) -> Item:
    """An item at ``price``, with an extra at each of ``costs``."""
    added = [Extra(id=ExtraId(uuid4()), cost=each) for each in costs]
    return Item(id=ItemId(uuid4()), price=price, extras=added)


def basket(name: str, *contents: Item, fee: Money | None = None) -> Basket:
    """A basket of ``contents``, delivered for ``fee`` if given."""
    courier = None if fee is None else Courier(id=CourierId(uuid4()), fee=fee)
    return Basket(
        id=BasketId(uuid4()), name=name, items=list(contents), courier=courier
    )


def dataset() -> list[Basket]:
    """Items in two currencies, side by side, two deep, and a courier or none."""
    return [
        basket(
            "mixed",
            item(money("1", "USD")),
            item(money("1000", "EUR")),
            fee=money("5", "USD"),
        ),
        basket("spread", item(money("10", "USD")), item(money("500", "USD"))),
        basket("usd", item(money("150", "USD"))),
        basket(
            "eur", item(money("5", "EUR"), money("200", "USD")), fee=money("20", "EUR")
        ),
        basket("empty"),
    ]


@pytest.fixture
def database(database_url: str) -> Iterator[str]:
    """An empty database, selected in the settings."""
    with override_settings(DATABASES={"default": {"URL": database_url}}):
        yield database_url


@pytest.fixture
async def stored(database: str) -> AsyncIterator[None]:
    """The dataset, committed."""
    async with connections.engine().begin() as connection:
        await connection.run_sync(metadata.create_all)
    async with BasketsUnitOfWork() as uow:
        await uow.baskets.add_all(dataset())
        await uow.commit()
    yield
    await connections.close_all()


async def found(criteria: Criteria | Selection) -> list[str]:
    """The names of the baskets ``criteria`` selects, the same in memory and in SQL."""
    memory = InMemoryRepository[Basket, BasketId](dataset())
    in_memory = sorted(each.name for each in await memory.filter(criteria))
    async with BasketsUnitOfWork() as uow:
        in_sql = sorted(each.name for each in await uow.baskets.filter(criteria))
    assert in_sql == in_memory
    return in_sql


@pytest.mark.parametrize(
    ("criteria", "names"),
    [
        (F("items.price") >= money("100", "USD"), ["spread", "usd"]),
        (F("items.price") < money("10", "EUR"), ["eur"]),
        (F("items.price") == money("1000", "EUR"), ["mixed"]),
        (F("items.price").in_([money("1000", "USD"), money("150", "USD")]), ["usd"]),
        (
            F("items.price").range(money("100", "USD"), money("2000", "USD")),
            ["spread", "usd"],
        ),
        (~(F("items.price") >= money("100", "USD")), ["empty", "eur", "mixed"]),
        (F("items.extras.cost") >= money("100", "USD"), ["eur"]),
        (F("items.extras.cost") >= money("100", "EUR"), []),
        (F("courier.fee") == money("5", "USD"), ["mixed"]),
        (F("courier.fee") > money("10", "EUR"), ["eur"]),
        (F("courier.fee") > money("10", "USD"), []),
        (F("items.price.currency") == "EUR", ["eur", "mixed"]),
        (F("items.price").isnull(), []),
    ],
)
async def test_money_through_children_is_one_child_s(
    stored: None, criteria: Criteria, names: list[str]
) -> None:
    """Each condition holds of one child's amount in that child's currency."""
    assert await found(criteria) == names


def test_money_through_children_binds_as_one_criterion() -> None:
    """Its parts are named from the model, and bound to the child entity."""
    bound = (F("items.extras.cost") >= money("1", "USD")).bind(Basket)

    assert isinstance(bound, Within)
    assert bound.relation == "items.extras"
    assert [(each.field, each.lookup, each.value) for each in bound.conditions()] == [
        ("items.extras.cost.currency", "exact", currency("USD")),
        ("items.extras.cost.amount", "gte", Decimal("1.00")),
    ]
    assert bound.matches(dataset()[3])
    assert not bound.matches(dataset()[0])


def test_within_names_child_entities_only() -> None:
    """Its relation is a path of child entities, from the model; bind it first."""
    criteria = F("price.amount") > 1

    with pytest.raises(UnknownField, match="no field 'goods'"):
        Within("goods", criteria).bind(Basket)
    with pytest.raises(InvalidQuery, match="holds no child entities"):
        Within("name", criteria).bind(Basket)
    with pytest.raises(InvalidQuery, match="holds no child entities"):
        Within("items.price", criteria).bind(Basket)
    with pytest.raises(InvalidQuery, match=r"^Bind the criteria"):
        Within("items", criteria).evaluate(dataset()[0])


class BasketFilters(FilterSet):
    """A money filter through the items."""

    model = Basket
    item_price = Filter("gte", "lte", "isnull", field="items.price")


@pytest.mark.parametrize(
    ("values", "names"),
    [
        ({"item_price__gte": 100, "item_price_currency": "USD"}, ["spread", "usd"]),
        (
            {
                "item_price__gte": 100,
                "item_price__lte": 200,
                "item_price_currency": "USD",
            },
            ["usd"],
        ),
        (
            {
                "item_price__gte": 400,
                "item_price__lte": 50,
                "item_price_currency": "USD",
            },
            [],
        ),
        ({"item_price__lte": 1000, "item_price_currency": "EUR"}, ["eur", "mixed"]),
        ({"item_price_currency": "EUR"}, ["eur", "mixed"]),
        ({"item_price__isnull": False, "item_price_currency": "EUR"}, ["eur", "mixed"]),
    ],
)
async def test_a_filter_set_compares_one_child_s_amounts_in_its_currency(
    stored: None, values: dict[str, Any], names: list[str]
) -> None:
    """Every amount given and the currency are of one item."""
    assert await found(BasketFilters(None, **values).query()) == names


def test_a_filter_set_wants_the_currency_with_an_amount() -> None:
    """An amount without its currency compares nothing."""
    with pytest.raises(InvalidFilter) as refused:
        BasketFilters(item_price__gte=100)
    assert refused.value.detail == (
        "Give item_price_currency with an amount of item_price: amounts compare "
        "only within a currency."
    )
