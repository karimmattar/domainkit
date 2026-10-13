"""Domain building blocks: model, events, errors, repositories, unit of work."""

import dataclasses
from typing import NewType
from uuid import UUID, uuid4

import pytest

from domainkit.ddd import (
    AggregateRoot,
    BusinessRuleViolation,
    Command,
    DomainError,
    DomainEvent,
    Entity,
    EventBus,
    InMemoryRepository,
    NotFound,
    Page,
    Query,
    UnitOfWork,
    ValueObject,
    event_bus,
)

pytestmark = pytest.mark.anyio

OrderId = NewType("OrderId", UUID)


class Money(ValueObject):
    """Amount in minor units and a currency."""

    amount: int
    currency: str


class Line(Entity[int]):
    """Entity used to check identity semantics."""

    quantity: int = 1


class Order(AggregateRoot[OrderId]):
    """Aggregate used throughout these tests."""

    total: Money

    def pay(self) -> None:
        """Record a payment event."""
        self.record(OrderPaid(order_id=self.id))


class OrderPaid(DomainEvent):
    """An order was paid."""

    order_id: UUID


class PlaceOrder(Command):
    """Command with one field."""

    total: int


class FindOrder(Query):
    """Query with one field."""

    order_id: UUID


class OrderTooLarge(BusinessRuleViolation):
    """Error with a derived code."""


class Gone(NotFound):
    """Error with an explicit code."""

    code = "order_gone"


def new_order(amount: int = 100) -> Order:
    """Build an order with a fresh id."""
    return Order(id=OrderId(uuid4()), total=Money(amount, "EGP"))


async def test_value_objects_compare_by_value_and_are_frozen() -> None:
    """Equal values are equal and hashable; attributes cannot change."""
    assert Money(5, "EGP") == Money(5, "EGP")
    assert len({Money(5, "EGP"), Money(5, "EGP")}) == 1
    with pytest.raises(dataclasses.FrozenInstanceError):
        Money(5, "EGP").amount = 6  # type: ignore[misc]


async def test_entities_compare_by_identity() -> None:
    """Same type and id means equal, whatever the attributes."""
    assert Line(id=1, quantity=1) == Line(id=1, quantity=9)
    assert Line(id=1) != Line(id=2)
    assert Line(id=1) != new_order()
    assert Line(id=1) != 1
    assert hash(Line(id=1)) == hash(Line(id=1, quantity=3))


async def test_aggregates_record_and_release_events() -> None:
    """Events wait on the aggregate until pulled."""
    order = new_order()
    order.pay()

    assert [type(event) for event in order.pending_events] == [OrderPaid]
    (event,) = order.pull_events()
    assert isinstance(event, OrderPaid)
    assert event.occurred_at.tzinfo is not None
    assert isinstance(event.event_id, UUID)
    assert order.pull_events() == []
    assert order.pending_events == ()


async def test_messages_are_frozen_and_keyword_only() -> None:
    """Commands, queries, and events reject positional and changed values."""
    command = PlaceOrder(total=5)

    with pytest.raises(TypeError):
        PlaceOrder(5)  # type: ignore[call-arg]
    with pytest.raises(dataclasses.FrozenInstanceError):
        command.total = 6  # type: ignore[misc]
    assert FindOrder(order_id=UUID(int=1)) == FindOrder(order_id=UUID(int=1))


async def test_domain_errors_carry_codes() -> None:
    """Codes come from the class name unless the class sets one."""
    assert OrderTooLarge("too large").code == "order_too_large"
    assert Gone("gone").code == "order_gone"
    assert DomainError("x").code == "domain_error"
    assert OrderTooLarge("too large").message == "too large"


async def test_event_bus_delivers_by_class_hierarchy(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Specific handlers run first; a failing handler does not stop the rest."""
    bus = EventBus()
    seen: list[str] = []

    async def specific(event: OrderPaid) -> None:
        seen.append("specific")

    async def failing(event: OrderPaid) -> None:
        raise RuntimeError("handler broke")

    async def every(event: DomainEvent) -> None:
        seen.append("every")

    bus.subscribe(OrderPaid, specific)
    bus.subscribe(OrderPaid, specific)
    bus.subscribe(OrderPaid, failing)
    bus.subscribe(DomainEvent, every)

    event = OrderPaid(order_id=uuid4())
    await bus.publish_all([event])

    assert seen == ["specific", "every"]
    (record,) = [r for r in caplog.records if r.exc_info]
    assert record.getMessage() == (
        f"Handler {failing.__qualname__} failed for OrderPaid {event.event_id}"
    )

    bus.unsubscribe(OrderPaid, failing)
    bus.unsubscribe(OrderPaid, failing)
    assert bus.handlers_for(OrderPaid) == [specific, every]
    bus.clear()
    assert bus.handlers_for(OrderPaid) == []


async def test_repository_tracks_what_it_hands_out() -> None:
    """Every added, loaded, or removed aggregate is seen once, in order."""
    first, second = new_order(), new_order()
    repository = InMemoryRepository[Order, OrderId]([first])

    await repository.add(second)
    assert await repository.get(first.id) is first
    assert await repository.find(OrderId(uuid4())) is None
    await repository.remove(second)

    assert repository.seen == (second, first)
    assert list(repository.items) == [first.id]
    with pytest.raises(NotFound, match="No aggregate has id"):
        await repository.get(second.id)


class FakeUnitOfWork(UnitOfWork):
    """Unit of work over one in-memory repository."""

    def __init__(self, bus: EventBus | None = None) -> None:
        """Track commits and rollbacks."""
        super().__init__(events=bus)
        self.orders = InMemoryRepository[Order, OrderId]()
        self.log: list[str] = []

    async def _commit(self) -> None:
        self.log.append("commit")

    async def _rollback(self) -> None:
        self.log.append("rollback")


async def test_unit_of_work_publishes_after_commit() -> None:
    """Events reach the bus only after a commit; leaving always rolls back."""
    bus = EventBus()
    published: list[DomainEvent] = []

    async def collect(event: DomainEvent) -> None:
        published.append(event)

    bus.subscribe(DomainEvent, collect)
    uow = FakeUnitOfWork(bus)
    order = new_order()

    async with uow:
        await uow.orders.add(order)
        order.pay()
        assert published == []
        await uow.commit()

    assert [type(event) for event in published] == [OrderPaid]
    assert uow.log == ["commit", "rollback"]
    assert uow.repositories() == [uow.orders]
    assert FakeUnitOfWork().events is event_bus


async def test_pages_know_whether_more_follow() -> None:
    """``Page.from_items`` keeps ``limit`` items of ``limit + 1`` fetched."""
    assert Page.from_items([1, 2, 3], 2) == Page(items=[1, 2], has_more=True)
    assert Page.from_items([1], 2) == Page(items=[1], has_more=False)


async def test_new_aggregates_start_unversioned() -> None:
    """Storage assigns version 1 on insert; new objects start at 0."""
    assert new_order().version == 0
