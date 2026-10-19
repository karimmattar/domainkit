"""Repositories: collection-like access to whole aggregates.

The domain declares an abstract repository (a port); infrastructure provides
the adapter, such as ``domainkit.db.SqlAlchemyRepository``. Every aggregate a
repository hands out or receives is tracked, so the unit of work can publish
the events those aggregates recorded.

Repositories answer queries (``domainkit.ddd.query``): ``filter``, ``first``,
``count``, ``exists``, and ``paginate``. ``InMemoryRepository`` evaluates
them in Python exactly as the SQL adapter does in the database, so use cases
tested against it behave the same in production.
"""

import asyncio
import copy
import dataclasses
from abc import ABC, abstractmethod
from collections.abc import Hashable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from typing import Any, Final, Literal, cast, overload

from domainkit.ddd._dataclass import EntityMeta
from domainkit.ddd.exceptions import Conflict, NotFound
from domainkit.ddd.model import AggregateRoot
from domainkit.ddd.pagination import Page
from domainkit.ddd.query import (
    ID,
    Criteria,
    CriteriaLike,
    F,
    Query,
    RowLock,
    as_criteria,
    as_query,
    following,
    order,
    position_of,
    total_ordering,
)

DELETED_AT: Final = "deleted_at"
LOCK_TIMEOUT: Final = 10.0
"""Seconds an in-memory row lock is waited for, as ``LOCK_TIMEOUT`` defaults."""


class LockNotAvailable(Conflict):
    """Another transaction holds the row's lock, and the caller would not wait."""


class SoftDeletable(metaclass=EntityMeta):
    """Mixin for aggregates that are marked deleted rather than removed.

    Put it before the aggregate base::

        class Invoice(SoftDeletable, AggregateRoot[InvoiceId]): ...

    Repositories leave deleted aggregates out of every read unless asked
    (``include_deleted=True``); ``remove`` marks one deleted, unless
    ``hard=True``, and ``restore`` brings it back. The table needs a
    nullable ``deleted_at`` column.
    """

    deleted_at: datetime | None = None

    @property
    def is_deleted(self) -> bool:
        """Whether the aggregate is marked deleted."""
        return self.deleted_at is not None

    def mark_deleted(self, when: datetime | None = None) -> None:
        """Mark the aggregate deleted at ``when`` (default: now)."""
        self.deleted_at = when or datetime.now(UTC)

    def restore(self) -> None:
        """Bring a deleted aggregate back."""
        self.deleted_at = None


class RowLocks:
    """Row locks for in-memory repositories, held until the unit of work ends.

    Repositories that share one ``RowLocks``, and the same ``items``, behave
    like two transactions on one database: a lock another holds is waited
    for, ``nowait`` raises ``LockNotAvailable``, and ``skip_locked`` passes
    the row by. A wait longer than ``timeout`` seconds raises
    ``LockNotAvailable``, as PostgreSQL does after ``LOCK_TIMEOUT``.
    """

    def __init__(self, timeout: float = LOCK_TIMEOUT) -> None:
        """Hold no locks yet."""
        self.timeout = timeout
        self._held: dict[Hashable, tuple[object, asyncio.Event]] = {}

    def holder(self, key: Hashable) -> object | None:
        """Who holds the lock on ``key``, if anyone."""
        held = self._held.get(key)
        return None if held is None else held[0]

    async def acquire(self, key: Hashable, owner: object, lock: RowLock) -> bool:
        """Lock ``key`` for ``owner``; False if it was skipped as locked.

        Raises:
            LockNotAvailable: Another owner holds it, and ``nowait`` is set
                or the wait timed out.
        """
        while (held := self._held.get(key)) is not None and held[0] is not owner:
            if lock.skip_locked:
                return False
            if lock.nowait:
                raise _locked()
            try:
                async with asyncio.timeout(self.timeout):
                    await held[1].wait()
            except TimeoutError:
                raise _locked() from None
        self._held.setdefault(key, (owner, asyncio.Event()))
        return True

    def release(self, owner: object) -> None:
        """Release every lock ``owner`` holds, waking those who wait."""
        for key, (holder, released) in list(self._held.items()):
            if holder is owner:
                del self._held[key]
                released.set()


def _locked() -> LockNotAvailable:
    """The error for a row someone else has locked."""
    return LockNotAvailable("The data is being changed by another request; try again.")


@dataclass(frozen=True, slots=True)
class Mark:
    """What a repository held when a savepoint began."""

    events: dict[Any, int]
    """Each tracked aggregate, with how many events it had recorded."""
    state: object
    """The repository's own snapshot (in memory: its items)."""


class Repository[AggregateT: AggregateRoot[Any], IdT: Hashable](ABC):
    """Load and store aggregates of one type; subclasses implement ``_`` methods.

    The aggregate class comes from ``model``, or from the type argument
    (``InMemoryRepository[Order, OrderId]``). Queries need it.
    """

    model: type[AggregateT]

    def __init__(self) -> None:
        """Start with no tracked aggregates."""
        self._seen: dict[AggregateT, None] = {}

    @property
    def seen(self) -> tuple[AggregateT, ...]:
        """Aggregates loaded or added through this repository, in first-seen order."""
        return tuple(self._seen)

    @property
    def aggregate_type(self) -> type[AggregateT]:
        """The class of the aggregates this repository stores.

        Raises:
            TypeError: Neither ``model`` nor a type argument names it.
        """
        model = getattr(self, "model", None) or _type_argument(self)
        if model is None:
            raise TypeError(
                f"{type(self).__name__} does not say which aggregate it stores; "
                "set model = <the aggregate class>."
            )
        return model

    async def add(self, aggregate: AggregateT) -> None:
        """Store a new aggregate."""
        await self._add(aggregate)
        self._track(aggregate)

    async def add_all(self, aggregates: Iterable[AggregateT]) -> None:
        """Store new aggregates; each one's events are published on commit."""
        for aggregate in aggregates:
            await self.add(aggregate)

    async def get(self, id_: IdT, *, include_deleted: bool = False) -> AggregateT:
        """Return the aggregate with ``id_``, or raise ``not_found(id_)``."""
        aggregate = await self.find(id_, include_deleted=include_deleted)
        if aggregate is None:
            raise self.not_found(id_)
        return aggregate

    async def find(
        self, id_: IdT, *, include_deleted: bool = False
    ) -> AggregateT | None:
        """Return the aggregate with ``id_``, or None.

        A soft-deleted aggregate counts as missing unless ``include_deleted``.
        """
        aggregate = await self._get(id_)
        if aggregate is None or (_is_deleted(aggregate) and not include_deleted):
            return None
        self._track(aggregate)
        return aggregate

    async def remove(self, aggregate: AggregateT, *, hard: bool = False) -> None:
        """Delete ``aggregate``; its recorded events are still published.

        A ``SoftDeletable`` aggregate is marked deleted and kept, unless
        ``hard`` is set.
        """
        if isinstance(aggregate, SoftDeletable) and not hard:
            aggregate.mark_deleted()
        else:
            await self._remove(aggregate)
        self._track(aggregate)

    async def remove_all(
        self, aggregates: Iterable[AggregateT], *, hard: bool = False
    ) -> None:
        """Delete each of ``aggregates``, as ``remove`` does."""
        for aggregate in aggregates:
            await self.remove(aggregate, hard=hard)

    async def restore(self, id_: IdT) -> AggregateT:
        """Bring back the soft-deleted aggregate ``id_`` and return it.

        Raises:
            TypeError: The aggregate is not ``SoftDeletable``.
        """
        aggregate = await self.get(id_, include_deleted=True)
        if not isinstance(aggregate, SoftDeletable):
            raise TypeError(f"{type(aggregate).__name__} is not SoftDeletable.")
        aggregate.restore()
        return aggregate

    async def filter(
        self, query: Query | CriteriaLike | None = None
    ) -> list[AggregateT]:
        """The aggregates ``query`` selects, in its ordering, then by id.

        ``query`` may also be bare criteria or a specification (everything
        matching, by id), or None (every aggregate).

        Raises:
            InvalidQuery: The query names an unknown field, or a value that
                does not fit its field.
            LockNotAvailable: The query locks rows with ``nowait`` and one
                is locked.
        """
        found = await self._filter(self._prepare(as_query(query)))
        for aggregate in found:
            self._track(aggregate)
        return found

    async def first(
        self, query: Query | CriteriaLike | None = None
    ) -> AggregateT | None:
        """The first aggregate ``query`` selects, or None."""
        found = await self.filter(as_query(query).replace(limit=1))
        return found[0] if found else None

    async def count(
        self, criteria: CriteriaLike | None = None, *, include_deleted: bool = False
    ) -> int:
        """How many aggregates meet ``criteria`` (all, if None)."""
        return await self._count(self._scope(criteria, include_deleted=include_deleted))

    async def exists(
        self, criteria: CriteriaLike | None = None, *, include_deleted: bool = False
    ) -> bool:
        """Whether any aggregate meets ``criteria``."""
        return await self._exists(
            self._scope(criteria, include_deleted=include_deleted)
        )

    async def paginate(
        self,
        query: Query | CriteriaLike | None = None,
        *,
        limit: int,
        after: Sequence[object] | None = None,
        offset: int = 0,
        count: bool = False,
    ) -> Page[AggregateT]:
        """One page of what ``query`` selects: after ``after``, or past ``offset``.

        ``after`` is the previous page's ``next_position``. The ordering
        always ends with the id, so positions are unique. ``count`` also
        counts every match into ``total``.
        """
        if limit < 1:
            raise ValueError(f"A page holds at least one item, not {limit}.")
        base = as_query(query)
        ordering = total_ordering(base.ordering)
        found = await self.filter(
            base.replace(ordering=ordering, limit=limit + 1, offset=offset, after=after)
        )
        items = found[:limit]
        more = len(found) > limit
        total = (
            await self.count(base.criteria, include_deleted=base.include_deleted)
            if count
            else None
        )
        return Page(
            items=items,
            has_more=more,
            next_position=position_of(items[-1], ordering) if more else None,
            total=total,
        )

    @overload
    async def get_for_update(
        self, id_: IdT, *, nowait: bool = False, skip_locked: Literal[False] = False
    ) -> AggregateT: ...

    @overload
    async def get_for_update(
        self, id_: IdT, *, nowait: bool = False, skip_locked: bool
    ) -> AggregateT | None: ...

    async def get_for_update(
        self, id_: IdT, *, nowait: bool = False, skip_locked: bool = False
    ) -> AggregateT | None:
        """The aggregate ``id_``, its row locked until the transaction ends.

        Another transaction that locks it waits until this one commits or
        rolls back, then sees what it left. ``nowait`` raises
        ``LockNotAvailable`` instead of waiting; with ``skip_locked`` a
        locked (or missing) aggregate is None. SQLite has no row locks:
        there the aggregate is read without one.

        Raises:
            NotFound: There is no such aggregate (``not_found(id_)``).
            LockNotAvailable: Another transaction holds the lock, with ``nowait``.
        """
        lock = RowLock(nowait=nowait, skip_locked=skip_locked)
        found = await self.first(Query(F(ID) == id_, lock=lock))
        if found is None and not skip_locked:
            raise self.not_found(id_)
        return found

    def not_found(self, id_: IdT) -> NotFound:
        """Build the error for a missing aggregate; override for a domain error."""
        return NotFound(f"No aggregate has id {id_}.")

    def _track(self, aggregate: AggregateT) -> None:
        """Remember ``aggregate`` so its events are collected on commit."""
        self._seen.setdefault(aggregate)

    def _scope(
        self, criteria: CriteriaLike | None, *, include_deleted: bool
    ) -> Criteria | None:
        """``criteria`` bound to the aggregate, soft-deleted ones left out."""
        model = self.aggregate_type
        parts = [] if criteria is None else [as_criteria(criteria)]
        if issubclass(model, SoftDeletable) and not include_deleted:
            parts.append(F(DELETED_AT).isnull())
        if not parts:
            return None
        joined = parts[0] if len(parts) == 1 else parts[0] & parts[1]
        return joined.bind(model)

    def _prepare(self, query: Query) -> Query:
        """``query`` bound to the aggregate: total ordering, position as criteria."""
        model = self.aggregate_type
        ordering = tuple(item.bind(model) for item in total_ordering(query.ordering))
        criteria = query.criteria
        if query.after is not None:
            beyond = following(model, ordering, query.after)
            criteria = beyond if criteria is None else criteria & beyond
        return Query(
            self._scope(criteria, include_deleted=query.include_deleted),
            ordering=ordering,
            limit=query.limit,
            offset=query.offset,
            lock=query.lock,
            include_deleted=query.include_deleted,
        )

    def _mark(self) -> Mark:
        """Note what is tracked now, for a savepoint to go back to."""
        return Mark(
            {aggregate: len(aggregate.pending_events) for aggregate in self._seen},
            self._snapshot(),
        )

    def _rewind(self, mark: Mark) -> None:
        """Go back to ``mark``: forget what was tracked since, and its events."""
        for aggregate in list(self._seen):
            if aggregate not in mark.events:
                del self._seen[aggregate]
                continue
            kept = aggregate.pull_events()[: mark.events[aggregate]]
            for event in kept:
                aggregate.record(event)
        self._restore(mark.state)

    def _snapshot(self) -> object:
        """The stored state a savepoint may restore; a database keeps its own."""
        return None

    def _restore(self, snapshot: object) -> None:  # noqa: B027 - optional hook.
        """Put the stored state back as ``_snapshot`` took it."""

    def _finished(self) -> None:  # noqa: B027 - optional hook.
        """The transaction ended: let go of what it held, such as row locks."""

    @abstractmethod
    async def _add(self, aggregate: AggregateT) -> None:
        """Store ``aggregate``."""

    @abstractmethod
    async def _get(self, id_: IdT) -> AggregateT | None:
        """Load the aggregate with ``id_``."""

    @abstractmethod
    async def _remove(self, aggregate: AggregateT) -> None:
        """Delete ``aggregate``."""

    async def _filter(self, query: Query) -> list[AggregateT]:
        """Load what the bound ``query`` selects, in its (total) ordering."""
        raise NotImplementedError(f"{type(self).__name__} cannot filter.")

    async def _count(self, criteria: Criteria | None) -> int:
        """Count what the bound ``criteria`` select."""
        raise NotImplementedError(f"{type(self).__name__} cannot count.")

    async def _exists(self, criteria: Criteria | None) -> bool:
        """Whether the bound ``criteria`` select anything."""
        return await self._count(criteria) > 0


def _is_deleted(aggregate: object) -> bool:
    """Whether ``aggregate`` is soft-deleted."""
    return isinstance(aggregate, SoftDeletable) and aggregate.is_deleted


def _type_argument(repository: "Repository[Any, Any]") -> type | None:
    """The aggregate class a repository's type arguments name, if any."""
    alias = getattr(repository, "__orig_class__", None)
    if alias is not None and (model := _aggregate_in(alias)) is not None:
        return model
    return _declared_aggregate(type(repository))


@cache
def _declared_aggregate(cls: type) -> type | None:
    """The aggregate class named in the generic bases of ``cls`` or its parents."""
    for klass in cls.__mro__:
        for base in klass.__dict__.get("__orig_bases__", ()):
            if (model := _aggregate_in(base)) is not None:
                return model
    return None


def _aggregate_in(alias: object) -> type | None:
    """The first type argument of ``Repository[...]``-like aliases, if a class."""
    origin = getattr(alias, "__origin__", None)
    arguments = getattr(alias, "__args__", ())
    if not (isinstance(origin, type) and issubclass(origin, Repository) and arguments):
        return None
    model = arguments[0]
    return (
        model if isinstance(model, type) and issubclass(model, AggregateRoot) else None
    )


class InMemoryRepository[AggregateT: AggregateRoot[Any], IdT: Hashable](
    Repository[AggregateT, IdT]
):
    """Dictionary-backed repository for unit tests and prototypes.

    Queries are evaluated in Python with the SQL adapter's semantics. Two
    repositories given the same ``items`` and ``locks`` act like two
    transactions on one table (see ``RowLocks``).
    """

    def __init__(
        self, aggregates: Iterable[AggregateT] = (), *, locks: RowLocks | None = None
    ) -> None:
        """Start with ``aggregates`` already stored."""
        super().__init__()
        self.items: dict[IdT, AggregateT] = {item.id: item for item in aggregates}
        self.locks = locks if locks is not None else RowLocks()

    async def _add(self, aggregate: AggregateT) -> None:
        """Store ``aggregate`` under its id."""
        self.items[aggregate.id] = aggregate

    async def _get(self, id_: IdT) -> AggregateT | None:
        """Look ``id_`` up."""
        return self.items.get(id_)

    async def _remove(self, aggregate: AggregateT) -> None:
        """Forget ``aggregate``."""
        self.items.pop(aggregate.id, None)

    async def _filter(self, query: Query) -> list[AggregateT]:
        """Match, sort, skip, lock, and limit, as the database would."""
        found = order(self._matching(query.criteria), query.ordering)[query.offset :]
        if query.lock is None:
            return found[: query.limit]
        taken: list[AggregateT] = []
        for aggregate in found:
            if query.limit is not None and len(taken) >= query.limit:
                break
            if await self.locks.acquire(aggregate.id, self, query.lock):
                taken.append(aggregate)
        return taken

    async def _count(self, criteria: Criteria | None) -> int:
        """Count the matches."""
        return len(self._matching(criteria))

    def _matching(self, criteria: Criteria | None) -> list[AggregateT]:
        """The stored aggregates that meet the bound ``criteria``."""
        return [
            aggregate
            for aggregate in self.items.values()
            if criteria is None or criteria.evaluate(aggregate)
        ]

    def _snapshot(self) -> object:
        """The stored aggregates, and a deep copy of each one's fields."""
        return dict(self.items), {
            key: _fields(aggregate) for key, aggregate in self.items.items()
        }

    def _restore(self, snapshot: object) -> None:
        """Put the stored aggregates, and their fields, back."""
        items, states = cast(
            "tuple[dict[IdT, AggregateT], dict[IdT, dict[str, object]]]", snapshot
        )
        self.items.clear()
        self.items.update(items)
        for key, aggregate in items.items():
            for name, value in states[key].items():
                setattr(aggregate, name, value)

    def _finished(self) -> None:
        """Release this repository's row locks."""
        self.locks.release(self)


def _fields(aggregate: object) -> dict[str, object]:
    """A deep copy of the dataclass fields of ``aggregate``."""
    return {
        field.name: copy.deepcopy(getattr(aggregate, field.name))
        for field in dataclasses.fields(aggregate)  # type: ignore[arg-type]
    }
