"""Unit of work backed by one SQLAlchemy session and transaction.

Before each commit, every installed domain's ``DomainConfig.stage`` may add
rows to the same transaction: ``domainkit.contrib.messaging`` writes the
outbox (durable handlers, deferred tasks), ``domainkit.contrib.audit`` the
audit trail. They commit, or roll back, together with the business change.
"""

from abc import abstractmethod
from collections.abc import Sequence
from types import TracebackType
from typing import Final, Self

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from domainkit.apps import registry
from domainkit.db.connections import connections
from domainkit.db.routing import ShardKey, router
from domainkit.ddd import (
    ConcurrentUpdate,
    DeferredTask,
    DomainEvent,
    EventBus,
    UnitOfWork,
)
from domainkit.exceptions import ImproperlyConfigured
from domainkit.tracing import span

MESSAGING: Final = "domainkit.contrib.messaging"


class SqlAlchemyUnitOfWork(UnitOfWork):
    """Open a session on enter, commit on ``commit()``, close on exit.

    Subclasses create their repositories in ``create_repositories``. The
    session opens on the routing target of the domain that defines the
    subclass (``DATABASE_ROUTING``), unless ``database`` names one. On a shard
    group, ``shard_key`` picks the database::

        async with AccountsUnitOfWork(shard_key=tenant_id) as uow: ...
    """

    database: str | None = None
    """Target (a SQL alias or a shard group); None follows ``DATABASE_ROUTING``."""
    alias: str
    """The one SQL alias this unit of work uses, resolved from the target."""
    session: AsyncSession

    def __init__(
        self,
        *,
        shard_key: ShardKey | None = None,
        database: str | None = None,
        events: EventBus | None = None,
    ) -> None:
        """Resolve the database now, so a missing shard key fails before any work."""
        super().__init__(events=events)
        self.database = (
            database or self.database or router.target_for_module(type(self).__module__)
        )
        self.alias = router.alias_for(self.database, shard_key)
        self.shard_key = shard_key

    async def __aenter__(self) -> Self:
        """Open a session and the repositories that share it."""
        self.session = connections.session(self.alias)
        self.create_repositories(self.session)
        return await super().__aenter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the session, which rolls back anything not committed.

        Unlike ``rollback()``, closing detaches loaded aggregates without
        expiring them, so a use case can return what it read.
        """
        self._deferred.clear()
        await self.session.close()

    @abstractmethod
    def create_repositories(self, session: AsyncSession) -> None:
        """Assign this unit of work's repositories, all bound to ``session``."""

    async def commit(self) -> None:
        """Commit inside a ``uow.commit`` span (see ``domainkit.tracing``)."""
        with span("uow.commit", database=self.alias):
            await super().commit()

    async def _stage(
        self, events: Sequence[DomainEvent], tasks: Sequence[DeferredTask]
    ) -> None:
        """Let installed domains add their rows; refuse durable work nobody runs."""
        durable = any(self.events.handlers_for(type(e), durable=True) for e in events)
        if (tasks or durable) and not registry.is_installed(MESSAGING):
            raise ImproperlyConfigured(
                f"Durable handlers and deferred tasks need {MESSAGING!r} in "
                "INSTALLED_DOMAINS."
            )
        for config in registry:
            await config.stage(self, events, tasks)

    async def _commit(self) -> None:
        """Commit, turning a lost optimistic-locking race into ``ConcurrentUpdate``."""
        try:
            await self.session.commit()
        except StaleDataError as exc:
            raise ConcurrentUpdate(
                "The data changed while this request was running; reload it and "
                "try again."
            ) from exc

    async def _rollback(self) -> None:
        """Roll the session's transaction back."""
        await self.session.rollback()
