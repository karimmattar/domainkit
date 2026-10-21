"""Domain building blocks: model, events, errors, repositories, unit of work."""

import asyncio
import dataclasses
import functools
import logging
from typing import NewType
from uuid import UUID, uuid4

import pytest

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.ddd import (
    AggregateRoot,
    BusinessRuleViolation,
    Command,
    DomainError,
    DomainEvent,
    Entity,
    EventBus,
    HandlerTimedOut,
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


async def test_a_hung_handler_is_cut_off_logged_and_reported(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The handlers after it still run; the request is not held for ever."""
    bus = EventBus(handler_timeout=0.01)
    seen: list[str] = []
    reported: list[tuple[BaseException, DomainEvent]] = []
    bus.error_hook = lambda exc, event: reported.append((exc, event))

    async def hung(event: OrderPaid) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            seen.append("hung stopped")

    async def after(event: OrderPaid) -> None:
        seen.append("after")

    bus.subscribe(OrderPaid, hung)
    bus.subscribe(OrderPaid, after)
    event = OrderPaid(order_id=uuid4())
    with caplog.at_level(logging.ERROR, "domainkit.ddd.events"):
        async with asyncio.timeout(5):
            await bus.publish(event)

    assert seen == ["hung stopped", "after"]
    ((timed_out, about),) = reported
    assert about is event
    assert isinstance(timed_out, HandlerTimedOut)
    assert isinstance(timed_out, TimeoutError)
    message = (
        f"Handler {hung.__qualname__} took more than 0.01s for OrderPaid "
        f"{event.event_id}"
    )
    assert str(timed_out) == message
    assert caplog.messages == [f"{message}; cut off"]


async def test_a_handler_can_have_a_timeout_of_its_own() -> None:
    """``subscribe(timeout=...)`` overrides the bus's limit, either way."""
    bus = EventBus(handler_timeout=0.01)
    seen: list[str] = []
    reported: list[BaseException] = []
    bus.error_hook = lambda exc, event: reported.append(exc)

    async def slow(event: OrderPaid) -> None:
        await asyncio.sleep(0.05)
        seen.append("slow finished")

    bus.subscribe(OrderPaid, slow, timeout=5)
    await bus.publish(OrderPaid(order_id=uuid4()))
    assert seen == ["slow finished"]
    assert reported == []

    strict = EventBus(handler_timeout=5)
    strict.error_hook = lambda exc, event: reported.append(exc)
    strict.subscribe(OrderPaid, slow, timeout=0.01)
    await strict.publish(OrderPaid(order_id=uuid4()))
    assert seen == ["slow finished"]
    assert [type(exc) for exc in reported] == [HandlerTimedOut]


async def test_a_handlers_own_timeout_error_is_a_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only the bus's limit counts as cut off; the handler's own error is its own."""
    bus = EventBus()
    reported: list[BaseException] = []
    bus.error_hook = lambda exc, event: reported.append(exc)

    async def times_out(event: OrderPaid) -> None:
        raise TimeoutError("the payment provider did not answer")

    bus.subscribe(OrderPaid, times_out)
    event = OrderPaid(order_id=uuid4())
    await bus.publish(event)

    (failure,) = reported
    assert type(failure) is TimeoutError
    assert caplog.messages == [
        f"Handler {times_out.__qualname__} failed for OrderPaid {event.event_id}"
    ]


async def test_handlers_without_a_qualified_name_are_named_as_they_are(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A partial or a callable object is named by its repr in the log."""
    bus = EventBus()

    async def failing(event: OrderPaid, note: str) -> None:
        raise RuntimeError(note)

    handler = functools.partial(failing, note="broken")
    bus.subscribe(OrderPaid, handler)
    await bus.publish(OrderPaid(order_id=uuid4()))

    assert caplog.messages[0].startswith("Handler functools.partial(")


def test_unsubscribing_from_an_event_nothing_handles_does_nothing() -> None:
    """No subscription, no error."""

    async def handler(event: OrderPaid) -> None:
        return None

    bus = EventBus()
    bus.unsubscribe(OrderPaid, handler)
    assert bus.handlers_for(OrderPaid) == []


async def test_cancelling_the_request_still_cancels_its_handlers() -> None:
    """A handler's limit never swallows the request's own cancellation."""
    bus = EventBus()
    started = asyncio.Event()

    async def waits(event: OrderPaid) -> None:
        started.set()
        await asyncio.Event().wait()

    bus.subscribe(OrderPaid, waits)
    publishing = asyncio.create_task(bus.publish(OrderPaid(order_id=uuid4())))
    async with asyncio.timeout(5):
        await started.wait()
    publishing.cancel()
    await asyncio.wait([publishing], timeout=5)
    assert publishing.cancelled()


@pytest.mark.parametrize(
    ("timeout", "durable"), [(0, False), (-1, False), (float("inf"), False), (5, True)]
)
def test_subscribe_refuses_a_timeout_it_cannot_apply(
    timeout: float, durable: bool
) -> None:
    """Positive and finite, and only for in-process handlers."""

    async def handler(event: OrderPaid) -> None:
        return None

    with pytest.raises(
        ValueError,
        match=r"^timeout is a positive number of seconds, for in-process handlers\.$",
    ):
        EventBus().subscribe(OrderPaid, handler, durable=durable, timeout=timeout)


@pytest.mark.parametrize("seconds", [0, -1, float("inf")])
def test_a_bus_refuses_a_handler_timeout_it_cannot_apply(seconds: float) -> None:
    """Zero would cut off every handler; infinity none."""
    with pytest.raises(
        ValueError, match=r"^handler_timeout is a positive number of seconds\.$"
    ):
        EventBus(handler_timeout=seconds)


def test_setup_gives_the_bus_the_handler_timeout_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``EVENT_HANDLER_TIMEOUT`` reaches the process's bus at ``setup()``."""
    monkeypatch.setattr(event_bus, "handler_timeout", event_bus.handler_timeout)
    registry.clear()
    with override_settings(EVENT_HANDLER_TIMEOUT=2.5):
        setup()
    assert event_bus.handler_timeout == 2.5


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
