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
from domainkit.conf import DEFAULT_DB_ALIAS
from domainkit.db.connections import connections
from domainkit.ddd import ConcurrentUpdate, DeferredTask, DomainEvent, UnitOfWork
from domainkit.exceptions import ImproperlyConfigured
from domainkit.tracing import span

MESSAGING: Final = "domainkit.contrib.messaging"


class SqlAlchemyUnitOfWork(UnitOfWork):
    """Open a session on enter, commit on ``commit()``, close on exit.

    Subclasses create their repositories in ``create_repositories``.
    """

    database: str = DEFAULT_DB_ALIAS
    """``DATABASES`` alias this unit of work uses."""
    session: AsyncSession

    async def __aenter__(self) -> Self:
        """Open a session and the repositories that share it."""
        self.session = connections.session(self.database)
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
        with span("uow.commit", database=self.database):
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
