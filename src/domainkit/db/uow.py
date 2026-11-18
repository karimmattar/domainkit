"""Unit of work backed by one SQLAlchemy session and transaction.

Before each commit, the domains that registered ``on_stage`` hooks
(``domainkit.db.hooks``) add rows to the same transaction:
``domainkit.contrib.messaging`` writes the outbox (durable handlers,
deferred tasks), ``domainkit.contrib.audit`` the audit trail. They commit,
or roll back, together with the business change; ``on_committed`` hooks
follow up after it.

A unique constraint that refuses a value, at commit or at an earlier flush,
raises ``DuplicateValue`` (a 409), or the subclass the constraint names.

A read-only unit of work opens its session on a replica of its database
when ``DATABASES`` names one (``REPLICAS``), and on the database itself
otherwise; on PostgreSQL its transaction is ``READ ONLY`` either way. A
replica with ``MAX_LAG`` is read only while it is known to be within it,
checked again each time the unit of work is entered; a commit makes this
task's later reads wait for the replica to replay it (ADR 0159).
``reads`` says how it reads (``Reads``): ``STATEMENTS`` runs each statement
on its own, without ``BEGIN`` and ``ROLLBACK``, and ``SNAPSHOT`` reads one
snapshot (``REPEATABLE READ``). ``savepoint()`` is a ``SAVEPOINT`` in the
session's transaction.

A unit of work that writes may choose its transaction's isolation level
(``Isolation``), set as the transaction begins: on PostgreSQL in its
``BEGIN``, with no round trip of its own. SQLite runs every transaction
serializably; ``REPEATABLE_READ`` and ``SERIALIZABLE`` begin it with
``BEGIN IMMEDIATE``, so such units of work take the database's write lock
first and run one at a time, never failing on each other.

``run`` retries PostgreSQL's serialization failures (SQLSTATE ``40001``)
and deadlocks (``40P01``); SQLite reports neither. ``lock(name)`` is
``pg_advisory_xact_lock`` on a 64-bit key (``lock_key``), and on SQLite
the database's write lock.
"""

import asyncio
import hashlib
import logging
import re
import threading
from collections.abc import Sequence
from enum import StrEnum
from functools import partial
from types import TracebackType
from typing import Any, Final, Self
from weakref import WeakKeyDictionary

from sqlalchemy import BigInteger, Connection, bindparam, event, func, select, text
from sqlalchemy.exc import (
    DBAPIError,
    DisconnectionError,
    IntegrityError,
    PendingRollbackError,
    ResourceClosedError,
    StatementError,
)
from sqlalchemy.exc import TimeoutError as PoolTimeout
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, AsyncSessionTransaction
from sqlalchemy.orm.exc import FlushError, StaleDataError

from domainkit.apps import registry
from domainkit.conf import settings
from domainkit.db.connections import INTERNAL, Reads, connections
from domainkit.db.errors import CONNECTION_EXCEPTION, duplicate_value
from domainkit.db.hooks import on_committed, on_stage
from domainkit.db.orm import mapper_registry, metadata
from domainkit.db.repository import SqlAlchemyRepository, reload_expired, sqlstate_of
from domainkit.db.routing import ShardKey, router
from domainkit.ddd import (
    ConcurrentUpdate,
    DeferredTask,
    DomainEvent,
    EventBus,
    HandlerTimedOut,
    LockNotAvailable,
    UnitOfWork,
)
from domainkit.ddd.repository import Mark, Repository
from domainkit.ddd.stores import implementation, repositories_of
from domainkit.ddd.tenancy import TenantRequired, current_tenant
from domainkit.ddd.uow import Savepoint
from domainkit.exceptions import ImproperlyConfigured
from domainkit.reporting import report_error
from domainkit.tracing import span

MESSAGING: Final = "domainkit.contrib.messaging"
REFUSED_BEFORE_COMMIT: Final = (
    StaleDataError,
    FlushError,
    PendingRollbackError,
    ResourceClosedError,
    PoolTimeout,
    DisconnectionError,
)
"""SQLAlchemy's own errors that come before a ``COMMIT`` is sent: a stale
version or a refused flush, a session or connection unusable, no connection
to be had. A commit they fail stored nothing."""
CONFLICTS: Final = frozenset({"40001", "40P01"})
"""PostgreSQL's SQLSTATEs of a transaction that lost a race and may succeed
if run again: ``serialization_failure`` and ``deadlock_detected``."""
LOCK_KEY_PERSON: Final = b"domainkit.lock"
"""Personalizes the hash of a lock's name, so its keys are domainkit's own."""
ADVISORY_LOCK: Final = select(
    func.pg_advisory_xact_lock(bindparam("key", type_=BigInteger))
)
TRY_ADVISORY_LOCK: Final = select(
    func.pg_try_advisory_xact_lock(bindparam("key", type_=BigInteger))
)
logger = logging.getLogger(__name__)


class Isolation(StrEnum):
    """The isolation level of a unit of work's transaction (``isolation=``).

    Each value is SQLAlchemy's name for it; ``Isolation("serializable")``
    and ``Isolation("repeatable_read")`` are read too.
    """

    READ_COMMITTED = "READ COMMITTED"
    """Each statement sees what was committed before it began: PostgreSQL's
    default. On SQLite, as without a level."""
    REPEATABLE_READ = "REPEATABLE READ"
    """Every statement sees the data as the first one did. On PostgreSQL, a
    row another transaction changed meanwhile cannot be changed: the change
    fails with a serialization failure, which ``run`` retries. On SQLite,
    as ``SERIALIZABLE``."""
    SERIALIZABLE = "SERIALIZABLE"
    """As if the transactions ran one after another. On PostgreSQL, one that
    could not have fails with a serialization failure, which ``run``
    retries. On SQLite, begun with ``BEGIN IMMEDIATE``: they do run one
    after another."""

    @classmethod
    def _missing_(cls, value: object) -> "Isolation | None":
        """Read a level in any case, with ``_`` or a space between its words."""
        if isinstance(value, str):
            name = value.upper().replace("_", " ").strip()
            for level in cls:
                if level.value == name:
                    return level
        return None


def lock_key(name: str) -> int:
    """The 64-bit key ``lock(name)`` takes on PostgreSQL, signed, as ``bigint``.

    A keyed BLAKE2b hash of the name's UTF-8, the same in every process and
    release: ``pg_locks`` shows it as ``classid`` (the high 32 bits) and
    ``objid`` (the low ones), with ``objsubid`` 1.
    """
    digest = hashlib.blake2b(
        name.encode("utf-8"), digest_size=8, person=LOCK_KEY_PERSON
    ).digest()
    return int.from_bytes(digest, "big", signed=True)


class SqlAlchemyUnitOfWork(UnitOfWork):
    """Open a session on enter, commit on ``commit()``, close on exit.

    Its repositories are those its class annotates, made by
    ``create_repositories``::

        class SqlAlchemyCatalogUnitOfWork(SqlAlchemyUnitOfWork, CatalogUnitOfWork):
            pass  # CatalogUnitOfWork's repositories, in SQL

    The
    session opens on the routing target of the domain that defines the
    subclass (``DATABASE_ROUTING``), unless ``database`` names one. On a shard
    group, ``shard_key`` picks the database::

        async with AccountsUnitOfWork(shard_key=tenant_id) as uow: ...

    ``readonly=True`` (or ``readonly = True`` on the subclass) reads from a
    replica of that database, if it has one, and refuses to commit;
    ``replica=False`` keeps it on the primary, for reads that must see the
    latest commit, such as permission checks. A replica with ``MAX_LAG``
    is read only while it is within it (``domainkit.db.replicas``).
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
    replica: bool = True
    """Whether a read-only unit of work reads from a replica, if the database
    has one. False keeps it on the primary: what must see the latest
    commit, such as a permission check, reads there (user report D1)."""
    isolation: Isolation | None = None
    """The isolation level of its transactions (``Isolation``); None keeps
    the database's own (``READ COMMITTED`` on PostgreSQL). For units of
    work that write: a read-only one says how it reads with ``reads``."""
    alias: str
    """The one SQL alias this unit of work uses, resolved from the target.

    A read-only unit of work's is a replica's, when the database has one
    it may read: chosen when it is made, and checked again as it is
    entered, when a replica with ``MAX_LAG`` may have fallen behind.
    """
    _replicated: str | None = None
    """The primary whose replicas this read-only unit of work reads."""
    session: AsyncSession
    schema: str | None
    """The tenant's schema its transactions use (``TENANCY.SCHEMA_DATABASES``)."""

    def __init__(
        self,
        *,
        shard_key: ShardKey | None = None,
        database: str | None = None,
        events: EventBus | None = None,
        readonly: bool | None = None,
        reads: Reads | str | None = None,
        isolation: Isolation | str | None = None,
        replica: bool | None = None,
    ) -> None:
        """Resolve the database now, so a missing shard key fails before any work.

        ``replica`` sets whether a read-only unit of work reads from a
        replica, in place of the class's.

        ``reads`` makes the unit of work read-only, unless ``readonly=False``
        says otherwise, which only ``Reads.TRANSACTION`` allows.
        ``isolation`` sets the isolation level of its transactions, in place
        of the class's.

        Raises:
            ValueError: ``reads`` is not a ``Reads`` value, or asks a unit of
                work that writes (``readonly=False``) to read without its
                transaction: one that writes always has one. Or
                ``isolation`` is not an ``Isolation`` level, or is given to
                a read-only unit of work.
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
        level = self.isolation if isolation is None else isolation
        if level is not None:
            self.isolation = Isolation(level)
            if self.readonly:
                raise ValueError(
                    "isolation= is for units of work that write; a read-only one "
                    "picks how it reads with reads= (Reads.SNAPSHOT reads one "
                    "snapshot)."
                )
        self.database = (
            database or self.database or router.target_for_module(type(self).__module__)
        )
        if shard_key is None and router.is_shard_group(self.database):
            shard_key = current_tenant()  # The tenant's database (TENANCY).
        self.alias = router.alias_for(self.database, shard_key)
        self.schema = tenant_schema_of(self.alias)
        if replica is not None:
            self.replica = replica
        if self.readonly and self.replica and settings.DATABASES[self.alias].REPLICAS:
            self._replicated = self.alias
            self.alias = router.read_alias(self.alias)
        self.shard_key = shard_key

    async def __aenter__(self) -> Self:
        """Open a session and the repositories that share it.

        A read-only unit of work reads the replica it chose while it still
        may, or another, or the primary (``router.read_alias``).
        """
        if self._replicated is not None:
            self.alias = router.read_alias(self._replicated, self.alias)
        if self.isolation is None:
            self.session = connections.session(
                self.alias,
                readonly=self.readonly,
                reads=self.reads,
                transaction=self.schema is not None,  # SET LOCAL needs one.
            )
        else:
            self.session = AsyncSession(
                _isolated(connections.engine(self.alias), self.isolation),
                expire_on_commit=False,
            )
        if self.schema is not None:
            event.listen(
                self.session.sync_session,
                "after_begin",
                partial(_search_path, self.schema),
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
        if isinstance(exc, StaleDataError):
            raise concurrent_update(exc) from exc
        if isinstance(exc, IntegrityError):
            duplicate = duplicate_value(exc, metadata)
            if duplicate is not None:
                raise duplicate from exc

    def create_repositories(self, session: AsyncSession) -> None:
        """Assign this unit of work's repositories, all bound to ``session``.

        By default, one for each repository the class annotates
        (``products: ProductRepository``): a ``SqlAlchemyRepository`` that
        implements the port, or the class annotated, if it is one
        (``domainkit.ddd.stores``). Override it to build them otherwise.
        """
        for name, declared in repositories_of(type(self)).items():
            setattr(self, name, implementation(declared, SqlAlchemyRepository)(session))

    async def commit(self) -> None:
        """Commit inside a ``uow.commit`` span (see ``domainkit.tracing``).

        A lost optimistic-locking race raises ``ConcurrentUpdate``, whether
        the stale write was flushed by the commit or before it, by a domain's
        ``on_stage`` hook.
        """
        with span("uow.commit", database=self.alias):
            try:
                await super().commit()
            except StaleDataError as exc:
                raise concurrent_update(exc) from exc

    async def _stage(
        self, events: Sequence[DomainEvent], tasks: Sequence[DeferredTask]
    ) -> None:
        """Run the ``on_stage`` hooks; refuse durable work nobody runs."""
        durable = any(self.events.handlers_for(type(e), durable=True) for e in events)
        if (tasks or durable) and not registry.is_installed(MESSAGING):
            raise ImproperlyConfigured(
                f"Durable handlers and deferred tasks need {MESSAGING!r} in "
                "INSTALLED_DOMAINS."
            )
        for hook in on_stage.hooks():
            await hook.call(self, events, tasks)

    async def _committed(self) -> None:
        """Run the ``on_committed`` hooks; a failure is reported, never raised.

        No deadline cuts them off any more (``deadlines.after_commit``), so
        each runs within ``EVENT_HANDLER_TIMEOUT``, as an in-process handler
        does: one still running then is cut off, logged and reported as
        ``HandlerTimedOut``, and the next one runs.
        """
        seconds = settings.EVENT_HANDLER_TIMEOUT
        for hook in on_committed.hooks():
            try:
                async with asyncio.timeout(seconds) as limit:
                    await hook.call(self)
            except Exception as exc:  # The data is committed: report, carry on.
                if not limit.expired():
                    logger.exception("After-commit work of %r failed", hook.domain)
                    report_error(exc, domain=hook.domain)
                    continue
                cut = HandlerTimedOut(
                    f"After-commit work of {hook.domain!r} took more than {seconds:g}s"
                )
                logger.error("%s; cut off", cut)
                report_error(cut, domain=hook.domain)

    async def _commit(self) -> None:
        """Commit, translating what the database refused.

        A taken unique value raises ``DuplicateValue``; a lost
        optimistic-locking race is ``commit``'s, wherever it flushed. On a
        database with replicas, this task's later reads wait for a replica
        with ``MAX_LAG`` to replay the commit (``note_commit``).
        """
        try:
            await self.session.commit()
        except IntegrityError as exc:
            duplicate = duplicate_value(exc, metadata)
            if duplicate is None:
                raise
            raise duplicate from exc
        if settings.DATABASES[self.alias].REPLICAS:
            from domainkit.db.replicas import note_commit  # Loaded with replicas.

            note_commit(self.alias)

    async def _rollback(self) -> None:
        """Roll the session's transaction back, then read again what it expired.

        A rollback expires every loaded aggregate; read lazily, that would
        need I/O outside ``await`` (``MissingGreenlet``), so they are read
        again now, as stored (DATA-19). Those added since are gone.
        """
        await self.session.rollback()
        await reload_expired(self.session)

    async def lock(self, name: str, *, nowait: bool = False) -> None:
        """Hold the lock called ``name`` until the transaction ends.

        On PostgreSQL, ``pg_advisory_xact_lock`` on ``lock_key(name)``; a
        wait past ``LOCK_TIMEOUT`` fails as a row lock's does (503
        ``database_busy``). On SQLite, the database's write lock: the
        transaction begins (``BEGIN IMMEDIATE``) if it had not written yet,
        waiting as long as any write would; ``nowait`` changes nothing there.

        Raises:
            ValueError: ``name`` is not a non-empty string.
            ReadOnlyError: The unit of work is read-only.
            LockNotAvailable: Another transaction holds it, with ``nowait``.
        """
        self._check_lock(name)
        if self.session.get_bind().dialect.name != "postgresql":
            connection = await self.session.connection()
            await connection.run_sync(_begin_immediate)
            return
        key = {"key": lock_key(name)}
        if not nowait:
            await self.session.execute(ADVISORY_LOCK, key)
        elif not await self.session.scalar(TRY_ADVISORY_LOCK, key):
            raise LockNotAvailable(
                "The data is being changed by another request; try again."
            )

    def _run_marks(self) -> list[tuple[Repository[Any, Any], Mark]]:
        """Nothing: each run opens a session, and repositories, of its own."""
        return []

    def _conflicted(self, exc: BaseException) -> bool:
        """Whether PostgreSQL refused the transaction: serialization or deadlock."""
        return isinstance(exc, DBAPIError) and sqlstate_of(exc) in CONFLICTS

    def _refused(self, exc: BaseException) -> bool:
        """Whether the commit surely stored nothing.

        A constraint, a stale version, a lost race, or another error the
        server sent back; SQLAlchemy's own errors that come before a
        ``COMMIT`` is sent (``REFUSED_BEFORE_COMMIT``); and a statement it
        could not build or bind. A connection lost or cut off (SQLSTATE
        class ``08``), a timeout, or a cancellation leaves the outcome
        unknown: the server may have applied the ``COMMIT`` whose answer
        never came. So do SQLAlchemy's other errors (``InvalidRequestError``,
        ``MissingGreenlet``): a session's ``after_commit`` listener may raise
        them once the ``COMMIT`` landed.
        """
        if super()._refused(exc) or isinstance(exc, REFUSED_BEFORE_COMMIT):
            return True
        if isinstance(exc, DBAPIError):
            return not exc.connection_invalidated and not (
                sqlstate_of(exc) or ""
            ).startswith(CONNECTION_EXCEPTION)
        return isinstance(exc, StatementError)  # Built or bound: never sent.

    async def _begin_savepoint(self) -> Savepoint:
        """``SAVEPOINT`` in the session's transaction (flushing first)."""
        return _SessionSavepoint(self.session, await self.session.begin_nested())

    def _translate(self, exc: BaseException) -> BaseException | None:
        """In a savepoint, a unique violation or a lost race, as the domain's error."""
        if isinstance(exc, StaleDataError):
            return concurrent_update(exc)
        if isinstance(exc, IntegrityError):
            return duplicate_value(exc, metadata)
        return None


STALE_TABLE: Final = re.compile(r"\bstatement on table '([^']+)'")
"""Where SQLAlchemy's ``StaleDataError`` names the table it found stale."""


def concurrent_update(error: StaleDataError) -> ConcurrentUpdate:
    """The domain's error for a lost optimistic-locking race the ORM reported.

    SQLAlchemy raises ``StaleDataError`` (``ConcurrentModificationError`` is
    the same class) when an ``UPDATE`` or ``DELETE`` of a versioned row
    matches no row of the version it read, at whichever flush sends it: a
    commit, a query that flushes first, an explicit ``flush()``, a
    savepoint, or an ``on_stage`` hook. ``model`` names the class mapped to
    the table its message names, if one is.
    """
    found = STALE_TABLE.search(str(error))
    model = None
    if found is not None:
        model = next(
            (
                qualified(mapper.base_mapper.class_)
                for mapper in mapper_registry.mappers
                if mapper.local_table.fullname == found[1]  # type: ignore[attr-defined]
            ),
            None,
        )
    return ConcurrentUpdate(
        "The data changed while this request was running; reload it and try again.",
        model=model,
    )


def qualified(cls: type) -> str:
    """``module.QualName`` of ``cls``."""
    return f"{cls.__module__}.{cls.__qualname__}"


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


_ISOLATED: Final[WeakKeyDictionary[AsyncEngine, dict[Isolation, AsyncEngine]]] = (
    WeakKeyDictionary()
)
"""Per engine, the engine with each isolation level's options (sharing its pool).

Engines belong to one event loop each; loops on several threads add theirs
with ``_ISOLATED_LOCK`` held, and read their own entry without it."""
_ISOLATED_LOCK: Final = threading.Lock()


def _isolated(engine: AsyncEngine, isolation: Isolation) -> AsyncEngine:
    """``engine`` with transactions at ``isolation``, made once per engine and level.

    On PostgreSQL, the ``isolation_level`` execution option: the driver puts
    it in the ``BEGIN`` it sends, and SQLAlchemy restores the connection's
    default when it goes back to the pool. On SQLite, a transaction that
    asks for more than ``READ COMMITTED`` begins with ``BEGIN IMMEDIATE``.
    """
    levels = _ISOLATED.get(engine)
    if levels is None:
        with _ISOLATED_LOCK:
            levels = _ISOLATED.setdefault(engine, {})
    bound = levels.get(isolation)
    if bound is None:
        if engine.dialect.name == "postgresql":
            bound = engine.execution_options(isolation_level=isolation.value)
        else:
            bound = engine.execution_options()
            if isolation is not Isolation.READ_COMMITTED:
                # A callable per engine, as connections._listen explains.
                event.listen(bound.sync_engine, "begin", partial(_begin_immediate))
        levels[isolation] = bound
    return bound


def _begin_immediate(connection: Connection) -> None:
    """Begin SQLite's transaction with the database's write lock, unless begun.

    The driver begins one only before a write, so the reads before it
    would not be part of it.
    """
    if not connection.connection.driver_connection.in_transaction:  # type: ignore[union-attr]
        connection.exec_driver_sql(
            "BEGIN IMMEDIATE", execution_options={INTERNAL: True}
        )


def tenant_schema_of(alias: str) -> str | None:
    """The current tenant's schema, if ``alias`` keeps one per tenant.

    Raises:
        TenantRequired: It does, and no tenant is current (``all_tenants``
            included: a schema holds one tenant's rows).
    """
    config = settings.TENANCY
    if config is None or alias not in config.SCHEMA_DATABASES:
        return None
    current = current_tenant()
    if current is None:
        raise TenantRequired
    from domainkit.tenancy import tenant_schema

    return tenant_schema(current)


async def tenant_schemas(alias: str) -> list[str | None]:
    """The schemas of ``alias`` that hold its tables: its own, and each tenant's.

    None stands for the connection's own schema. On a database of
    ``TENANCY.SCHEMA_DATABASES``, every table also lives in each tenant's
    schema (``TENANCY.SCHEMA``), so work on all of a database's rows, such
    as erasing a person or sealing the audit trail, visits each of them.
    """
    config = settings.TENANCY
    if config is None or alias not in config.SCHEMA_DATABASES:
        return [None]
    prefix, _, suffix = config.SCHEMA.partition("{tenant}")
    async with connections.engine(alias).connect() as connection:
        names = (
            await connection.execute(text("SELECT nspname FROM pg_namespace"))
        ).scalars()
        tenants = sorted(
            name
            for name in names
            if name.startswith(prefix)
            and name.endswith(suffix)
            and len(name) > len(prefix) + len(suffix)
        )
    return [None, *tenants]


def _search_path(schema: str, session: Any, transaction: Any, connection: Any) -> None:  # noqa: ANN401 - SQLAlchemy's event arguments.
    """Work in ``schema`` for the transaction just begun (``SET LOCAL``)."""
    connection.exec_driver_sql(f'SET LOCAL search_path TO "{schema}"')
