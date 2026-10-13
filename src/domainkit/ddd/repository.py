"""Repositories: collection-like access to whole aggregates.

The domain declares an abstract repository (a port); infrastructure provides
the adapter, such as ``domainkit.db.SqlAlchemyRepository``. Every aggregate a
repository hands out or receives is tracked, so the unit of work can publish
the events those aggregates recorded.
"""

from abc import ABC, abstractmethod
from collections.abc import Hashable, Iterable
from typing import Any

from domainkit.ddd.exceptions import NotFound
from domainkit.ddd.model import AggregateRoot


class Repository[AggregateT: AggregateRoot[Any], IdT: Hashable](ABC):
    """Load and store aggregates of one type; subclasses implement ``_`` methods."""

    def __init__(self) -> None:
        """Start with no tracked aggregates."""
        self._seen: dict[AggregateT, None] = {}

    @property
    def seen(self) -> tuple[AggregateT, ...]:
        """Aggregates loaded or added through this repository, in first-seen order."""
        return tuple(self._seen)

    async def add(self, aggregate: AggregateT) -> None:
        """Store a new aggregate."""
        await self._add(aggregate)
        self._track(aggregate)

    async def get(self, id_: IdT) -> AggregateT:
        """Return the aggregate with ``id_``, or raise ``not_found(id_)``."""
        aggregate = await self.find(id_)
        if aggregate is None:
            raise self.not_found(id_)
        return aggregate

    async def find(self, id_: IdT) -> AggregateT | None:
        """Return the aggregate with ``id_``, or None."""
        aggregate = await self._get(id_)
        if aggregate is not None:
            self._track(aggregate)
        return aggregate

    async def remove(self, aggregate: AggregateT) -> None:
        """Delete ``aggregate``; its recorded events are still published."""
        await self._remove(aggregate)
        self._track(aggregate)

    def not_found(self, id_: IdT) -> NotFound:
        """Build the error for a missing aggregate; override for a domain error."""
        return NotFound(f"No aggregate has id {id_}.")

    def _track(self, aggregate: AggregateT) -> None:
        """Remember ``aggregate`` so its events are collected on commit."""
        self._seen.setdefault(aggregate)

    @abstractmethod
    async def _add(self, aggregate: AggregateT) -> None:
        """Store ``aggregate``."""

    @abstractmethod
    async def _get(self, id_: IdT) -> AggregateT | None:
        """Load the aggregate with ``id_``."""

    @abstractmethod
    async def _remove(self, aggregate: AggregateT) -> None:
        """Delete ``aggregate``."""


class InMemoryRepository[AggregateT: AggregateRoot[Any], IdT: Hashable](
    Repository[AggregateT, IdT]
):
    """Dictionary-backed repository for unit tests and prototypes."""

    def __init__(self, aggregates: Iterable[AggregateT] = ()) -> None:
        """Start with ``aggregates`` already stored."""
        super().__init__()
        self.items: dict[IdT, AggregateT] = {item.id: item for item in aggregates}

    async def _add(self, aggregate: AggregateT) -> None:
        """Store ``aggregate`` under its id."""
        self.items[aggregate.id] = aggregate

    async def _get(self, id_: IdT) -> AggregateT | None:
        """Look ``id_`` up."""
        return self.items.get(id_)

    async def _remove(self, aggregate: AggregateT) -> None:
        """Forget ``aggregate``."""
        self.items.pop(aggregate.id, None)
