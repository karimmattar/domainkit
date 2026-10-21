"""Unit of work backed by one SQLAlchemy session and transaction.

Before each commit, every installed domain's ``DomainConfig.stage`` may add
rows to the same transaction: ``domainkit.contrib.messaging`` writes the
outbox (durable handlers, deferred tasks), ``domainkit.contrib.audit`` the
audit trail. They commit, or roll back, together with the business change.

A unique constraint that refuses a value, at commit or at an earlier flush,
raises ``DuplicateValue`` (a 409), or the subclass the constraint names.

A read-only unit of work opens its session on a replica of its database
when ``DATABASES`` names one (``REPLICAS``), and on the database itself
otherwise; on PostgreSQL its transaction is ``READ ONLY`` either way.
``reads`` says how it reads (``Reads``): ``STATEMENTS`` runs each statement
on its own, without ``BEGIN`` and ``ROLLBACK``, and ``SNAPSHOT`` reads one
snapshot (``REPEATABLE READ``). ``savepoint()`` is a ``SAVEPOINT`` in the
session's transaction.
"""

import logging
from abc import abstractmethod
from collections.abc import Sequence
from types import TracebackType
from typing import Final, Self

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, AsyncSessionTransaction
from sqlalchemy.orm.exc import StaleDataError

from domainkit.apps import registry
from domainkit.db.connections import Reads, connections
from domainkit.db.errors import duplicate_value
from domainkit.db.orm import metadata
from domainkit.db.repository import reload_expired
from domainkit.db.routing import ShardKey, router
from domainkit.ddd import (
    ConcurrentUpdate,
    DeferredTask,
    DomainEvent,
    EventBus,
    UnitOfWork,
)
from domainkit.ddd.uow import Savepoint
from domainkit.exceptions import ImproperlyConfigured
from domainkit.reporting import report_error
from domainkit.tracing import span

MESSAGING: Final = "domainkit.contrib.messaging"
logger = logging.getLogger(__name__)


class SqlAlchemyUnitOfWork(UnitOfWork):
    """Open a session on enter, commit on ``commit()``, close on exit.

    Subclasses create their repositories in ``create_repositories``. The
    session opens on the routing target of the domain that defines the
    subclass (``DATABASE_ROUTING``), unless ``database`` names one. On a shard
    group, ``shard_key`` picks the database::

        async with AccountsUnitOfWork(shard_key=tenant_id) as uow: ...

    ``readonly=True`` (or ``readonly = True`` on the subclass) reads from a
    replica of that database, if it has one, and refuses to commit.
    ``reads`` picks how such a unit of work reads (``Reads``), and makes it
    read-only::

        async with AccountsUnitOfWork(reads=Reads.STATEMENTS) as uow: ...
    """

    database: str | None = None
    """Target (a SQL alias or a shard group); None follows ``DATABASE_ROUTING``."""
    reads: Reads = Reads.TRANSACTION
    """How a read-only unit of work reads. A subclass that sets another
    value is read-only; with ``STATEMENTS`` or ``SNAPSHOT`` its session also
    refuses to flush or to run a statement that writes."""
    alias: str
    """The one SQL alias this unit of work uses, resolved from the target.

    A read-only unit of work's is a replica's, when the database has one.
    """
    session: AsyncSession

    def __init__(
        self,
        *,
        shard_key: ShardKey | None = None,
        database: str | None = None,
        events: EventBus | None = None,
        readonly: bool | None = None,
        reads: Reads | str | None = None,
    ) -> None:
        """Resolve the database now, so a missing shard key fails before any work.

        ``reads`` makes the unit of work read-only, unless ``readonly=False``
        says otherwise, which only ``Reads.TRANSACTION`` allows.

        Raises:
            ValueError: ``reads`` is not a ``Reads`` value, or asks a unit of
                work that writes (``readonly=False``) to read without its
                transaction: one that writes always has one.
        """
        super().__init__(events=events, readonly=readonly)
        if reads is not None:
            self.reads = Reads(reads)
        if (reads is not None or self.reads is not Reads.TRANSACTION) and (
            readonly is not False
        ):
            self.readonly = True
        elif self.reads is not Reads.TRANSACTION and not self.readonly:
            raise ValueError(
                f"reads={self.reads.value!r} is for read-only units of work; "
                "one that writes always runs in a transaction."
            )
        self.database = (
            database or self.database or router.target_for_module(type(self).__module__)
        )
        self.alias = router.alias_for(self.database, shard_key)
        if self.readonly:
            self.alias = router.read_alias(self.alias)
        self.shard_key = shard_key

    async def __aenter__(self) -> Self:
        """Open a session and the repositories that share it."""
        self.session = connections.session(
            self.alias, readonly=self.readonly, reads=self.reads
        )
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
        expiring them, so a use case can return what it read. A unique
        violation from a flush inside the block becomes ``DuplicateValue``.
        """
        self._deferred.clear()
        await self.session.close()
        if isinstance(exc, IntegrityError):
            duplicate = duplicate_value(exc, metadata)
            if duplicate is not None:
                raise duplicate from exc

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

    async def _committed(self) -> None:
        """Let installed domains follow up; a failure is reported, never raised."""
        for config in registry:
            try:
                await config.committed(self)
            except Exception as exc:  # The data is committed: report, carry on.
                logger.exception("After-commit work of %r failed", config.label)
                report_error(exc, domain=config.label)

    async def _commit(self) -> None:
        """Commit, translating what the database refused.

        A lost optimistic-locking race raises ``ConcurrentUpdate``, and a
        taken unique value ``DuplicateValue``.
        """
        try:
            await self.session.commit()
        except StaleDataError as exc:
            raise ConcurrentUpdate(
                "The data changed while this request was running; reload it and "
                "try again."
            ) from exc
        except IntegrityError as exc:
            duplicate = duplicate_value(exc, metadata)
            if duplicate is None:
                raise
            raise duplicate from exc

    async def _rollback(self) -> None:
        """Roll the session's transaction back."""
        await self.session.rollback()

    async def _begin_savepoint(self) -> Savepoint:
        """``SAVEPOINT`` in the session's transaction (flushing first)."""
        return _SessionSavepoint(self.session, await self.session.begin_nested())

    def _translate(self, exc: BaseException) -> BaseException | None:
        """A unique violation inside a savepoint becomes ``DuplicateValue``."""
        if isinstance(exc, IntegrityError):
            return duplicate_value(exc, metadata)
        return None


class _SessionSavepoint(Savepoint):
    """A ``SAVEPOINT``; rolling back reloads what it had changed."""

    def __init__(self, session: AsyncSession, nested: AsyncSessionTransaction) -> None:
        """Hold the nested transaction of ``session``."""
        self.session = session
        self.nested = nested

    async def rollback(self) -> None:
        """``ROLLBACK TO SAVEPOINT``, then reload what SQLAlchemy expired.

        Rolling back expires what the savepoint changed; read lazily, that
        would need I/O outside ``await``, so it is read again now.
        """
        await self.nested.rollback()
        await reload_expired(self.session)

    async def release(self) -> None:
        """``RELEASE SAVEPOINT``, after flushing."""
        await self.nested.commit()
