"""Entities, aggregates, and value objects.

Subclasses become dataclasses automatically::

    class Money(ValueObject):
        amount: Decimal
        currency: str

    class Order(AggregateRoot[OrderId]):
        lines: list[OrderLine]
"""

from collections.abc import Hashable
from typing import Final

from domainkit.ddd._dataclass import EntityMeta, ValueObjectMeta
from domainkit.ddd.events import DomainEvent

_EVENTS: Final = "_domain_events"


class ValueObject(metaclass=ValueObjectMeta):
    """Immutable value defined only by its attributes; equal values are equal.

    Validate in ``__post_init__`` and raise a ``DomainError`` subclass, so an
    invalid value can never exist.
    """


class Entity[IdT: Hashable](metaclass=EntityMeta):
    """Object with a lifecycle, equal to another only when the ids match.

    Fields are keyword-only, so subclasses may add fields with or without
    defaults in any order.
    """

    id: IdT

    def __eq__(self, other: object) -> bool:
        """Compare by type and identity, never by attribute values."""
        return (
            isinstance(other, Entity)
            and type(other) is type(self)
            and other.id == self.id
        )

    def __hash__(self) -> int:
        """Hash by type and identity, matching ``__eq__``."""
        return hash((type(self), self.id))


class AggregateRoot[IdT: Hashable](Entity[IdT]):
    """Entry point to a consistency boundary; records events as it changes.

    Repositories load and save whole aggregates, and a unit of work publishes
    their recorded events after a successful commit. Events are kept outside
    the dataclass fields so aggregates loaded by an ORM (which skips
    ``__init__``) behave the same as new ones.

    ``version`` counts stored changes. A table with a ``version`` column turns
    on optimistic locking: saving a stale copy raises ``ConcurrentUpdate``
    instead of silently overwriting someone else's change.
    """

    version: int = 0

    def record(self, event: DomainEvent) -> None:
        """Remember ``event`` until the unit of work publishes it."""
        self.__dict__.setdefault(_EVENTS, []).append(event)

    @property
    def pending_events(self) -> tuple[DomainEvent, ...]:
        """Recorded events that have not been published yet."""
        return tuple(self.__dict__.get(_EVENTS, ()))

    def pull_events(self) -> list[DomainEvent]:
        """Return the recorded events and forget them."""
        events: list[DomainEvent] = self.__dict__.pop(_EVENTS, [])
        return events
