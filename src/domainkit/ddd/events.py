"""Domain events and the in-process bus that delivers them.

Aggregates record events; a unit of work publishes them after commit; handlers
subscribed in ``DomainConfig.ready()`` react. This is how one aggregate or
bounded context reacts to another without importing its internals (Django
signals play the same role).

Delivery is in-process and best effort: a failing handler is logged and does
not stop the others. Use an outbox table when another service must receive an
event reliably.
"""

import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import field
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID, uuid4

from domainkit.ddd._dataclass import MessageMeta

logger = logging.getLogger(__name__)


def _now() -> datetime:
    """Return the current time in UTC."""
    return datetime.now(UTC)


class DomainEvent(metaclass=MessageMeta):
    """Something that happened in the domain, named in the past tense.

    Subclasses are frozen, keyword-only dataclasses. Prefer plain values
    (ids, strings, numbers) over domain objects, so events stay valid
    messages after the model changes.
    """

    event_id: UUID = field(default_factory=uuid4)
    occurred_at: datetime = field(default_factory=_now)


type EventHandler[E: DomainEvent] = Callable[[E], Awaitable[None]]


class EventBus:
    """Deliver events to handlers subscribed to their class or a base class.

    In-process handlers run right after the commit, inside the request; a
    failure is logged and reported, then skipped. Durable handlers
    (``durable=True``) run later in a worker, from the outbox written in the
    same transaction: they survive crashes and are retried, so they must be
    idempotent. Durable delivery needs ``domainkit.contrib.messaging``.
    """

    def __init__(self) -> None:
        """Start with no subscriptions."""
        self._handlers: dict[type[DomainEvent], list[_Subscription]] = {}
        self.error_hook: Callable[[BaseException, DomainEvent], None] | None = None
        """Called with each in-process handler failure (``setup()`` wires it)."""

    def subscribe[E: DomainEvent](
        self, event_type: type[E], handler: EventHandler[E], *, durable: bool = False
    ) -> None:
        """Call ``handler`` for every ``event_type`` event; repeats are ignored."""
        subscriptions = self._handlers.setdefault(event_type, [])
        if (handler, durable) not in subscriptions:
            subscriptions.append((handler, durable))

    def unsubscribe[E: DomainEvent](
        self, event_type: type[E], handler: EventHandler[E]
    ) -> None:
        """Stop calling ``handler``, durable or not; unknown handlers are ignored."""
        subscriptions = self._handlers.get(event_type, [])
        subscriptions[:] = [entry for entry in subscriptions if entry[0] != handler]

    def clear(self) -> None:
        """Remove every subscription."""
        self._handlers.clear()

    def handlers_for(
        self, event_type: type[DomainEvent], *, durable: bool = False
    ) -> list[EventHandler[Any]]:
        """Return in-process (or durable) handlers, most specific first."""
        return [
            handler
            for cls in event_type.__mro__
            for handler, is_durable in self._handlers.get(cls, ())
            if is_durable is durable
        ]

    def durable_subscriptions(
        self,
    ) -> list[tuple[type[DomainEvent], EventHandler[Any]]]:
        """Return every durable ``(event type, handler)`` pair."""
        return [
            (event_type, handler)
            for event_type, subscriptions in self._handlers.items()
            for handler, durable in subscriptions
            if durable
        ]

    async def publish(self, event: DomainEvent) -> None:
        """Run the in-process handlers for ``event`` in order, isolating failures."""
        for handler in self.handlers_for(type(event)):
            try:
                await handler(event)
            except Exception as exc:
                logger.exception(
                    "Handler %s failed for %s %s",
                    getattr(handler, "__qualname__", handler),
                    type(event).__name__,
                    event.event_id,
                )
                if self.error_hook is not None:
                    self.error_hook(exc, event)

    async def publish_all(self, events: Iterable[DomainEvent]) -> None:
        """Publish ``events`` one after another, in order."""
        for event in events:
            await self.publish(event)


type _Subscription = tuple[EventHandler[Any], bool]


event_bus: Final = EventBus()
"""Process-wide bus used by units of work unless they are given another."""
