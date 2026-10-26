"""Install ``domainkit.contrib.idempotency`` and add its middleware."""

from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Iterator,
    Mapping,
    Sequence,
)
from contextlib import asynccontextmanager
from importlib import import_module

from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, iter_route_contexts
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.routing import BaseRoute, Host, Mount

from domainkit import deadlines
from domainkit.apps import DomainConfig, registry
from domainkit.checks import CheckMessage
from domainkit.conf import settings
from domainkit.conf.global_settings import DEADLINE_MIDDLEWARE, DEPLOYED_ENVIRONMENTS
from domainkit.db import SqlAlchemyUnitOfWork, connections
from domainkit.db.hooks import (
    on_committed,
    on_stage,
    on_stage_transaction,
    on_transaction_committed,
)
from domainkit.db.routing import DatabaseRouter, router
from domainkit.ddd import DeferredTask, DomainEvent
from domainkit.ddd.personal import event_subjects
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.deadlines import RequestTimeout

from . import operations
from .operations import (
    KeyTakenOver,
    Operation,
    committed_values,
    running_operation,
)
from .settings import IdempotencySettings
from .statements import MARK_COMMITTED, PIN_LEASE, holder

MESSAGING = "domainkit.contrib.messaging"


class IdempotencyConfig(DomainConfig):
    """Store replayable responses; mark keys whose changes committed."""

    label = "idempotency"
    verbose_name = "Idempotency keys"
    settings_class = IdempotencySettings
    requires = ()

    def ready(self) -> None:
        """Check the keys' database and lease; schedule the purge with messaging.

        Every commit, in a unit of work or not, holds the keyed request's
        key (``on_stage``, ``on_stage_transaction``) and notes it committed
        (``on_committed``, ``on_transaction_committed``).

        Raises:
            ImproperlyConfigured: ``LEASE_SECONDS`` is not above
                ``REQUEST_TIMEOUT``, or the keys' database is a shard.
        """
        config = self.get_settings()
        router.require_standalone(config.DATABASE, "IDEMPOTENCY.DATABASE")
        timeout = settings.REQUEST_TIMEOUT
        if timeout is not None and timeout >= config.LEASE_SECONDS:
            raise ImproperlyConfigured(
                f"IDEMPOTENCY.LEASE_SECONDS ({config.LEASE_SECONDS:g}) must be above "
                f"REQUEST_TIMEOUT ({timeout:g}): a retry with the same "
                "Idempotency-Key could take the key over from a request still "
                "running, and repeat what that request did outside its "
                "transaction. Raise IDEMPOTENCY.LEASE_SECONDS, or lower "
                "REQUEST_TIMEOUT."
            )
        if registry.is_installed(MESSAGING):
            import_module(f"{self.name}.tasks")
        on_stage(self.hold_key)
        on_stage_transaction(self.hold_key_in_transaction)
        on_committed(self.note_commit)
        on_transaction_committed(self.note_transaction_commit)

    async def hold_key(
        self,
        uow: SqlAlchemyUnitOfWork,
        events: Sequence[DomainEvent],
        tasks: Sequence[DeferredTask],
    ) -> None:
        """Mark the keyed request committed here, or pin its key; refuse a lost key.

        On ``IDEMPOTENCY.DATABASE`` the mark joins this transaction. Elsewhere
        the key is pinned first, by a statement that commits on its own, and
        the operation notes it: whether or not the commit lands, or reports
        back, no retry can take the key over and the request never releases
        it. The pin keeps to the deadline (``_within_deadline``). The
        people the events are about are noted, to be kept with the response.
        """
        operation = running_operation()
        if operation is None:
            return
        for event in events:
            operation.subjects |= event_subjects(event)
        await self._hold(operation, uow.alias, uow.session.connection)

    async def hold_key_in_transaction(
        self, connection: AsyncConnection, database: str
    ) -> None:
        """Hold the key for a commit made outside a unit of work, as ``hold_key`` does.

        ``enqueue`` commits its task on its own: queued work is a change the
        keyed request made, and its retry must not queue it again.
        """
        operation = running_operation()
        if operation is None:
            return

        async def joined() -> AsyncConnection:
            return connection

        await self._hold(operation, database, joined)

    async def _hold(
        self,
        operation: Operation,
        alias: str,
        connect: Callable[[], Awaitable[AsyncConnection]],
    ) -> None:
        """Mark the key in the commit on ``alias``, or pin it first elsewhere."""
        database = self.get_settings().DATABASE
        if alias == database:
            connection = await connect()
            held = await connection.execute(MARK_COMMITTED, committed_values(operation))
        else:
            async with (
                _within_deadline(),
                connections.autocommit(database) as connection,
            ):
                held = await connection.execute(
                    PIN_LEASE, holder(operation.key_hash, operation.token)
                )
            if held.rowcount:
                operation.pinned = True  # Before the commit, whatever it reports.
        if not held.rowcount:
            raise KeyTakenOver(
                "This request ran past its Idempotency-Key lease, and a retry "
                "took the key over; its changes were not saved."
            )

    async def note_commit(self, uow: SqlAlchemyUnitOfWork) -> None:
        """Note the commit; mark the key now if it went to another database.

        The mark keeps to the deadline (``_within_deadline``); cut off, the
        key stays pinned, so the changes still never run twice.
        """
        await self._note_commit(uow.alias)

    async def note_transaction_commit(self, database: str) -> None:
        """Note a commit made outside a unit of work, as ``note_commit`` does."""
        await self._note_commit(database)

    async def _note_commit(self, alias: str) -> None:
        """Note that changes committed on ``alias``; mark the key there if elsewhere."""
        operation = running_operation()
        if operation is None:
            return
        operation.committed = True
        database = self.get_settings().DATABASE
        if alias != database:
            async with (
                _within_deadline(),
                connections.autocommit(database) as connection,
            ):
                await connection.execute(MARK_COMMITTED, committed_values(operation))

    def check_application(self, app: FastAPI) -> list[str]:
        """Keyed routes whose own ``RequestTimeout`` outlasts the keys' lease.

        ``ready`` keeps ``REQUEST_TIMEOUT`` below ``LEASE_SECONDS``; a route
        that moves its deadline (``Depends(RequestTimeout(...))``) to the
        lease or past it, or removes it, could still lose its key to a retry
        while it runs.
        """
        config = self.get_settings()
        lease = config.LEASE_SECONDS
        return [
            CheckMessage(
                "idempotency.E001",
                f"{route} may run "
                + ("without a deadline" if seconds is None else f"for {seconds:g}s")
                + f" (RequestTimeout), but IDEMPOTENCY.LEASE_SECONDS is {lease:g}: "
                "a retry with the same Idempotency-Key could take the key over "
                "while it runs. Raise IDEMPOTENCY.LEASE_SECONDS above the route's "
                "timeout, or shorten the timeout.",
            )
            for route, seconds in _route_timeouts(app.routes, config.METHODS)
            if seconds is None or seconds >= lease
        ]

    def deployment_warnings(self) -> list[str]:
        """Keyed requests without a deadline, in staging and production.

        ``ready`` and ``check_application`` hold each keyed request's
        deadline below the lease; with no deadline at all (no
        ``REQUEST_TIMEOUT``, or no ``DeadlineMiddleware``) nothing does,
        and a request that runs past the lease can lose its key to a
        retry. A warning in 0.9.1, so deployments that run this way keep
        starting; 0.10 refuses to start.
        """
        if settings.ENVIRONMENT not in DEPLOYED_ENVIRONMENTS:
            return []
        if settings.REQUEST_TIMEOUT is None:
            missing = "REQUEST_TIMEOUT is None"
        elif DEADLINE_MIDDLEWARE not in settings.MIDDLEWARE:
            missing = f"{DEADLINE_MIDDLEWARE} is not in MIDDLEWARE"
        else:
            return []
        lease = self.get_settings().LEASE_SECONDS
        warning = CheckMessage(
            "idempotency.W001",
            f"Requests with an Idempotency-Key run without a deadline ({missing}): "
            f"one that runs past IDEMPOTENCY.LEASE_SECONDS ({lease:g}s) can lose "
            "its key to a retry, which runs it again. Set REQUEST_TIMEOUT below "
            f"IDEMPOTENCY.LEASE_SECONDS and list {DEADLINE_MIDDLEWARE} in "
            "MIDDLEWARE; 0.10 refuses to start without a deadline.",
        )
        return [warning]

    async def erase_subject(self, subject: str, reason: str) -> Mapping[str, int]:
        """Drop the kept responses about ``subject``; their keys stay, answered 409."""
        dropped = await operations.erase_subject(self.get_settings().DATABASE, subject)
        return {"responses": dropped}

    async def export_subject(self, subject: str) -> Mapping[str, object]:
        """The kept responses about ``subject``."""
        found = await operations.export_subject(self.get_settings().DATABASE, subject)
        return {"responses": found} if found else {}

    def databases(self, router: DatabaseRouter) -> frozenset[str]:
        """Only ``IDEMPOTENCY.DATABASE``: keys are stored apart from business data."""
        return frozenset({self.get_settings().DATABASE})

    def get_settings(self) -> IdempotencySettings:
        """Return the validated ``IDEMPOTENCY`` settings."""
        return registry.settings(IdempotencySettings)


@asynccontextmanager
async def _within_deadline() -> AsyncIterator[None]:
    """Cut the block off at the deadline, unless a timer already does.

    The pin and the late mark commit as they run, without the transaction
    whose ``SET LOCAL`` lowered ``statement_timeout`` to the time left. A
    request's own timer cuts them off already; a task that only shares
    the deadline gets a timer of its own here, at no round trip. A
    deadline that has passed changes nothing, as for a transaction's
    timeouts: the work after it must still finish.
    """
    if deadlines.enforced() or not deadlines.remaining():
        yield
        return
    async with deadlines.timeout(None):
        yield


def _route_timeouts(
    routes: Sequence[BaseRoute], methods: Sequence[str], prefix: str = ""
) -> Iterator[tuple[str, float | None]]:
    """``METHODS /path`` and seconds of each ``RequestTimeout`` of keyed routes.

    Routes of included routers carry their routers' dependencies, and the
    routes of mounted applications are walked too: the middleware sees
    their requests as well.
    """
    for context in iter_route_contexts(routes):
        route = context.original_route
        if isinstance(route, Mount | Host):
            inner = prefix + str(context.path) if isinstance(route, Mount) else prefix
            yield from _route_timeouts(context.routes or (), methods, inner)
        elif isinstance(route, APIRoute) and (
            keyed := set(methods).intersection(route.methods or ())
        ):
            name = f"{' '.join(sorted(keyed))} {prefix}{context.path}"
            for timeout in _timeouts(context.dependant):
                yield name, timeout.seconds


def _timeouts(dependant: Dependant) -> Iterator[RequestTimeout]:
    """The ``RequestTimeout`` dependencies of ``dependant``, at any depth."""
    for dependency in dependant.dependencies:
        if isinstance(dependency.call, RequestTimeout):
            yield dependency.call
        yield from _timeouts(dependency)
