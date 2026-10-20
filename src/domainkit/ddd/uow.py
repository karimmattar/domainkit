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

``savepoint()`` undoes part of the work when it fails, and keeps the rest::

    async with uow:
        try:
            async with uow.savepoint():
                await reserve_stock(uow, order)
        except OutOfStock:
            order.backorder()
        await uow.commit()

A read-only unit of work (``readonly=True``, or a subclass that sets
``readonly = True``) refuses to commit; the SQL one reads from a replica
when ``DATABASES`` names one.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from types import TracebackType
from typing import Any, Self

from domainkit.ddd.events import DomainEvent, EventBus, event_bus
from domainkit.ddd.preconditions import check_loaded
from domainkit.ddd.repository import Repository


class ReadOnlyError(RuntimeError):
    """A read-only unit of work was asked to commit."""


@dataclass(frozen=True, slots=True)
class DeferredTask:
    """Background work queued by ``UnitOfWork.defer``."""

    function: Callable[..., Awaitable[None]]
    kwargs: Mapping[str, object] = field(default_factory=dict)
    delay: timedelta | None = None
    dedupe_key: str | None = None


class Savepoint:
    """A savepoint's handle; in memory, the repositories restore themselves."""

    async def rollback(self) -> None:
        """Undo what happened since the savepoint."""

    async def release(self) -> None:
        """Keep what happened since the savepoint."""


class UnitOfWork(ABC):
    """Transaction boundary; subclasses expose repositories as attributes."""

    readonly: bool = False
    """Refuse to commit; the SQL unit of work reads from a replica if it can."""

    def __init__(
        self, *, events: EventBus | None = None, readonly: bool | None = None
    ) -> None:
        """Publish committed events to ``events`` (default: the global bus).

        ``readonly`` overrides the class's ``readonly``.
        """
        self.events = events if events is not None else event_bus
        if readonly is not None:
            self.readonly = readonly
        self._deferred: list[DeferredTask] = []
        self._savepoints = 0
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
        """Store changes and durable work together, then publish the events.

        Raises:
            ReadOnlyError: The unit of work is read-only.
            RuntimeError: A savepoint is still open, or an aggregate that
                ``If-Match`` names was not loaded yet (``preconditions``).
        """
        if self.readonly:
            raise ReadOnlyError(
                f"{type(self).__name__} is read-only; open one without "
                "readonly to change data."
            )
        if self._savepoints:
            raise RuntimeError("Leave every savepoint before committing.")
        check_loaded()
        events = self.collect_new_events()
        deferred, self._deferred = self._deferred, []
        await self._stage(events, deferred)
        await self._commit()
        self._finish()
        await self._committed()
        await self.events.publish_all(events)

    async def rollback(self) -> None:
        """Discard changes and deferred tasks that were not committed."""
        self._deferred.clear()
        await self._rollback()
        self._finish()

    @asynccontextmanager
    async def savepoint(self) -> AsyncIterator[None]:
        """Run the block in a savepoint: if it raises, undo it, then re-raise.

        Undone are its database changes, the aggregates it added, the events
        they recorded, and the tasks it deferred; what came before stays.
        Savepoints nest. Commit only after leaving them.
        """
        marks = [(repository, repository._mark()) for repository in self.repositories()]
        deferred = len(self._deferred)
        point = await self._begin_savepoint()
        self._savepoints += 1
        try:
            yield
            await point.release()
        except BaseException as exc:
            await point.rollback()
            for repository, mark in marks:
                repository._rewind(mark)
            del self._deferred[deferred:]
            translated = self._translate(exc)
            if translated is None:
                raise
            raise translated from exc
        finally:
            self._savepoints -= 1

    async def _begin_savepoint(self) -> Savepoint:
        """Start a savepoint; the SQL unit of work starts one in the database."""
        return Savepoint()

    def _translate(self, exc: BaseException) -> BaseException | None:
        """The error to raise instead of ``exc`` from a savepoint; None keeps it."""
        return None

    async def _stage(
        self, events: Sequence[DomainEvent], tasks: Sequence[DeferredTask]
    ) -> None:
        """Persist durable work in the transaction; in memory, keep the tasks."""
        self.queued_tasks.extend(tasks)

    async def _committed(self) -> None:  # noqa: B027 - optional, for subclasses.
        """Run follow-up work after the commit; it must not fail the commit."""

    def _finish(self) -> None:
        """Let every repository release what the transaction held."""
        for repository in self.repositories():
            repository._finished()

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
