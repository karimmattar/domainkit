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

``run(work)`` runs work that may lose a race with another transaction, and
runs it again, whole, when it does (``Retry``)::

    await uow.run(lambda: transfer(command, uow), retry=Retry(attempts=5))

``lock(name)`` holds a lock by name until the transaction ends, for work
that has no row to lock.

``expect(model, id, versions)`` holds an aggregate to the versions a client
read (``If-Match``): the repositories check it when they first load it, and
the unit of work refuses to commit before they did::

    with uow.expect(Order, order_id, {3}) as expected:
        await services.cancel_order(command, uow)
"""

import asyncio
import logging
import math
import secrets
from abc import ABC, abstractmethod
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Collection,
    Hashable,
    Iterator,
    Mapping,
    Sequence,
)
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from types import TracebackType
from typing import Any, Final, Self

from domainkit import clock, deadlines
from domainkit.ddd.events import DomainEvent, EventBus, event_bus
from domainkit.ddd.preconditions import Expectation, check_loaded, expectation
from domainkit.ddd.query import RowLock
from domainkit.ddd.repository import Mark, Repository, RowLocks
from domainkit.exceptions import DomainkitError

MAX_ATTEMPTS: Final = 10
"""The most runs a ``Retry`` allows."""
MAX_BACKOFF: Final = 60.0
"""The longest wait, in seconds, a ``Retry`` allows before a run."""
NAMED_LOCKS: Final = RowLocks()
"""The locks ``lock(name)`` takes in memory: one registry for the process, as
one database would be for every transaction."""
logger = logging.getLogger(__name__)
_random = secrets.SystemRandom()


class ReadOnlyError(RuntimeError, DomainkitError):
    """A read-only unit of work was asked to commit."""


@dataclass(frozen=True, slots=True)
class DeferredTask:
    """Background work queued by ``UnitOfWork.defer``."""

    function: Callable[..., Awaitable[None]]
    kwargs: Mapping[str, object] = field(default_factory=dict)
    delay: timedelta | None = None
    dedupe_key: str | None = None


@dataclass(frozen=True, slots=True)
class Retry:
    """How ``UnitOfWork.run`` runs its work again when the transaction lost a race.

    The work runs at most ``attempts`` times, the first included. Before
    each next run it waits a random time between 0 and ``backoff`` seconds,
    doubled for each run so far, at most ``max_backoff`` ("full jitter"),
    so transactions that collided do not collide again. The database's
    serialization failures and deadlocks are run again; ``retry_on`` adds
    errors of your own, such as ``ConcurrentUpdate``.
    """

    attempts: int = 3
    """Runs in all, the first included: 1 to ``MAX_ATTEMPTS``."""
    backoff: float = 0.02
    """Seconds: the longest wait before the second run."""
    max_backoff: float = 1.0
    """Seconds: the longest wait before any run, at most ``MAX_BACKOFF``."""
    retry_on: tuple[type[Exception], ...] = ()
    """Errors that run the work again, besides the database's conflicts."""

    def __post_init__(self) -> None:
        """Refuse a policy out of bounds.

        Raises:
            ValueError: ``attempts`` is not 1 to ``MAX_ATTEMPTS``, the waits
                are not finite with 0 <= ``backoff`` <= ``max_backoff`` <=
                ``MAX_BACKOFF``, or ``retry_on`` holds something other than
                exception classes.
        """
        attempts = self.attempts
        if isinstance(attempts, bool) or not isinstance(attempts, int):
            raise ValueError(f"Retry attempts is a whole number, not {attempts!r}.")
        if not 1 <= attempts <= MAX_ATTEMPTS:
            raise ValueError(
                f"Retry attempts is 1 to {MAX_ATTEMPTS} runs, the first "
                f"included, not {attempts}."
            )
        backoff, most = self.backoff, self.max_backoff
        if not (
            math.isfinite(backoff)
            and math.isfinite(most)
            and 0 <= backoff <= most <= MAX_BACKOFF
        ):
            raise ValueError(
                f"Retry waits need 0 <= backoff <= max_backoff <= {MAX_BACKOFF:g} "
                f"seconds, not backoff={backoff!r}, max_backoff={most!r}."
            )
        errors = tuple(self.retry_on)
        for error in errors:
            if not (isinstance(error, type) and issubclass(error, Exception)):
                raise ValueError(f"retry_on takes exception classes, not {error!r}.")
        object.__setattr__(self, "retry_on", errors)  # A list, say, as a tuple.

    def delay(self, runs: int) -> float:
        """A random wait, in seconds, before run ``runs + 1``."""
        ceiling = min(self.max_backoff, self.backoff * 2.0 ** min(runs - 1, 64))
        return _random.uniform(0.0, ceiling)


DEFAULT_RETRY: Final = Retry()
"""What ``UnitOfWork.run`` retries with when it is given no ``Retry``."""


class Savepoint:
    """A savepoint's handle; in memory, the repositories restore themselves."""

    async def rollback(self) -> None:
        """Undo what happened since the savepoint."""

    async def release(self) -> None:
        """Keep what happened since the savepoint."""


class _Commits:
    """How many commits a unit of work made while ``run`` ran its work."""

    __slots__ = ("count",)

    def __init__(self) -> None:
        """None yet."""
        self.count = 0


class UnitOfWork(ABC):
    """Transaction boundary; subclasses expose repositories as attributes."""

    readonly: bool = False
    """Refuse to commit; the SQL unit of work reads from a replica if it can."""
    _commits: _Commits | None = None
    """Counted while ``run`` runs. Set on the instance only then, as is
    ``_named_locks``: ``repositories()`` walks the instance's attributes."""
    _named_locks: bool = False
    """Whether this unit of work holds named locks in memory (``lock``)."""
    _expected: list[Expectation] | tuple[()] = ()
    """What ``expect`` holds the repositories to. Set on the instance only
    while it holds something, for the same reason as ``_commits``."""

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
        """Begin the unit of work: its repositories hold to what it expects.

        Each transaction checks the expected aggregates again, the first
        time it loads them.
        """
        if self._expected:
            for expected in self._expected:
                expected.renew()
            self._lend_expectations()
        return self

    @contextmanager
    def expect(
        self, model: type, id_: Hashable, versions: Collection[int] | None
    ) -> Iterator[Expectation]:
        """Hold aggregate ``id_`` of ``model`` to one of ``versions`` in the block.

        This unit of work's repositories check the aggregate the first time
        they hand it out in each of its transactions (``get``, ``find``,
        ``filter``, ``first``, and the locking reads), and ``commit``
        refuses to write before they did. A version this unit of work's own
        commit produced is accepted afterwards; any other is refused, so a
        service that reads in one block and writes in the next cannot write
        over another client's change made in between. ``versions`` None
        only notes whether the aggregate is loaded (``If-Match: *``). Read
        ``checked`` on what it yields to know whether the block loaded it.
        Other units of work are not held to it.

        Raises:
            PreconditionFailed: In the block, when a repository loads the
                aggregate at another version.
        """
        expected = expectation(model, id_, versions)
        held = self._expected
        if not isinstance(held, list):
            held = self._expected = []
        held.append(expected)
        self._lend_expectations()
        try:
            yield expected
        finally:
            held.remove(expected)
            if not held:
                del self._expected  # Back to the class's: none.

    def _accept_committed(self) -> None:
        """Let a later transaction load the versions this commit produced."""
        for repository in self.repositories():
            for aggregate in repository.seen:
                for expected in self._expected:
                    expected.accept(aggregate)

    def _lend_expectations(self) -> None:
        """Give each repository this unit of work's expectations, to check loads."""
        for repository in self.repositories():
            repository._expected = self._expected

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
        check_loaded(self._expected)
        events = self.collect_new_events()
        deferred, self._deferred = self._deferred, []
        await self._stage(events, deferred)
        await self._commit()
        if self._commits is not None:
            self._commits.count += 1
        if self._expected:
            self._accept_committed()
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

    async def run[T](
        self, work: Callable[[], Awaitable[T]], /, *, retry: Retry | None = None
    ) -> T:
        """Run ``work``, and run it again, whole, if its transaction lost a race.

        ``work`` takes no arguments and does the whole job, entering this
        unit of work itself, as a service does::

            await uow.run(lambda: transfer(command, uow), retry=Retry(attempts=5))

        A failure that ``retry`` names (``DEFAULT_RETRY`` if None) runs it
        again after a short random wait: the database's serialization
        failures and deadlocks, and the errors in ``retry_on``. What the
        failed run wrote is gone with its transaction: rows, outbox
        messages, audit entries, an ``Idempotency-Key``'s mark. In-memory
        repositories are put back as they were before it. Events are
        published, and deferred tasks kept, only by a commit that succeeds.

        It is not run again, and its failure is raised as it was, once
        ``retry.attempts`` runs failed, when this unit of work committed
        during the run (running it again would commit twice), or when the
        wait would pass the deadline (``domainkit.deadlines``). Under a
        frozen clock (``freeze_time``) the wait takes no time.

        Raises:
            Exception: The last run's failure, or one that is not retried.
        """
        policy = DEFAULT_RETRY if retry is None else retry
        commits = self._commits
        outermost = commits is None
        if commits is None:  # A run inside another's shares its count.
            commits = self._commits = _Commits()
        try:
            runs = 0
            while True:
                runs += 1
                before = commits.count
                marks = self._run_marks()
                try:
                    return await work()
                except Exception as exc:
                    if commits.count != before or not (
                        isinstance(exc, policy.retry_on) or self._conflicted(exc)
                    ):
                        raise
                    wait = policy.delay(runs)
                    left = deadlines.remaining()
                    if runs >= policy.attempts or (left is not None and wait >= left):
                        if runs > 1:
                            exc.add_note(
                                f"The work ran {runs} times; each run lost a race."
                            )
                        raise
                    for repository, mark in marks:
                        repository._rewind(mark)
                    logger.info(
                        "Run %d of %s lost a race (%s); running it again",
                        runs,
                        type(self).__name__,
                        type(exc).__name__,
                    )
                await _sleep(wait)
        finally:
            if outermost:
                del self._commits

    async def lock(self, name: str, *, nowait: bool = False) -> None:
        """Hold the lock called ``name`` until this unit of work's transaction ends.

        For work that has no row to lock, such as "one payout per account
        and day": another unit of work that asks for the same name waits
        until this one commits or rolls back. ``nowait`` raises
        ``LockNotAvailable`` instead of waiting. Take it before reading what
        it guards. In memory the names are the process's (``NAMED_LOCKS``),
        and a wait past its timeout raises ``LockNotAvailable``.

        Raises:
            ValueError: ``name`` is not a non-empty string.
            ReadOnlyError: The unit of work is read-only.
            LockNotAvailable: Another unit of work holds it, with ``nowait``.
        """
        self._check_lock(name)
        self._named_locks = True
        await NAMED_LOCKS.acquire(name, self, RowLock(nowait=nowait))

    def _check_lock(self, name: str) -> None:
        """Refuse a lock's name that is not a non-empty string, or a read-only lock.

        Raises:
            ValueError: ``name`` is not a non-empty string.
            ReadOnlyError: The unit of work is read-only.
        """
        if not isinstance(name, str) or not name:
            raise ValueError(f"A lock's name is a non-empty string, not {name!r}.")
        if self.readonly:
            raise ReadOnlyError(
                f"{type(self).__name__} is read-only; it takes no locks."
            )

    def _conflicted(self, exc: BaseException) -> bool:
        """Whether ``exc`` says the transaction lost a race; in memory, none does."""
        return False

    def _run_marks(self) -> list[tuple[Repository[Any, Any], Mark]]:
        """What a run of ``run`` that failed puts back: every repository's state."""
        return [(each, each._mark()) for each in self.repositories()]

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
        """Let every repository release what the transaction held, and named locks."""
        for repository in self.repositories():
            repository._finished()
        if self._named_locks:
            self._named_locks = False
            NAMED_LOCKS.release(self)

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


async def _sleep(seconds: float) -> None:
    """Wait before running work again; under a frozen clock, only yield."""
    await asyncio.sleep(0 if clock.frozen() is not None else seconds)
