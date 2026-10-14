"""Unit of work: one business transaction over one or more repositories.

Application services use it as an async context manager::

    async with uow:
        order = await uow.orders.get(order_id)
        order.cancel()
        await uow.commit()

Leaving the block without ``commit()`` rolls back. After a successful commit,
the events recorded by every aggregate the repositories saw are published.

``defer(task, **kwargs)`` queues background work that is stored with the
same commit and run by a worker afterwards (``domainkit.contrib.messaging``).
"""

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from types import TracebackType
from typing import Any, Self

from domainkit.ddd.events import DomainEvent, EventBus, event_bus
from domainkit.ddd.repository import Repository


@dataclass(frozen=True, slots=True)
class DeferredTask:
    """Background work queued by ``UnitOfWork.defer``."""

    function: Callable[..., Awaitable[None]]
    kwargs: Mapping[str, object] = field(default_factory=dict)
    delay: timedelta | None = None
    dedupe_key: str | None = None


class UnitOfWork(ABC):
    """Transaction boundary; subclasses expose repositories as attributes."""

    def __init__(self, *, events: EventBus | None = None) -> None:
        """Publish committed events to ``events`` (default: the global bus)."""
        self.events = events if events is not None else event_bus
        self._deferred: list[DeferredTask] = []
        self.queued_tasks: list[DeferredTask] = []
        """Deferred tasks of committed work (in memory, nothing runs them)."""

    def defer(
        self,
        function: Callable[..., Awaitable[None]],
        /,
        *,
        delay: timedelta | None = None,
        dedupe_key: str | None = None,
        **kwargs: object,
    ) -> None:
        """Run ``function(**kwargs)`` in the background once this work commits.

        ``dedupe_key`` drops the task if one with the same key was ever queued.
        """
        self._deferred.append(DeferredTask(function, kwargs, delay, dedupe_key))

    async def __aenter__(self) -> Self:
        """Begin the unit of work."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Roll back anything not committed."""
        await self.rollback()

    async def commit(self) -> None:
        """Store changes and durable work together, then publish the events."""
        events = self.collect_new_events()
        deferred, self._deferred = self._deferred, []
        await self._stage(events, deferred)
        await self._commit()
        await self._committed()
        await self.events.publish_all(events)

    async def rollback(self) -> None:
        """Discard changes and deferred tasks that were not committed."""
        self._deferred.clear()
        await self._rollback()

    async def _stage(
        self, events: Sequence[DomainEvent], tasks: Sequence[DeferredTask]
    ) -> None:
        """Persist durable work in the transaction; in memory, keep the tasks."""
        self.queued_tasks.extend(tasks)

    async def _committed(self) -> None:  # noqa: B027 - optional, for subclasses.
        """Run follow-up work after the commit; it must not fail the commit."""

    def collect_new_events(self) -> list[DomainEvent]:
        """Take the pending events from every aggregate the repositories saw."""
        return [
            event
            for repository in self.repositories()
            for aggregate in repository.seen
            for event in aggregate.pull_events()
        ]

    def repositories(self) -> list[Repository[Any, Any]]:
        """Return the repositories stored as attributes of this unit of work."""
        return [value for value in vars(self).values() if isinstance(value, Repository)]

    @abstractmethod
    async def _commit(self) -> None:
        """Commit the underlying transaction."""

    @abstractmethod
    async def _rollback(self) -> None:
        """Roll the underlying transaction back."""
