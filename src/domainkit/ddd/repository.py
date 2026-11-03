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
from collections.abc import AsyncIterator, Callable, Hashable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from typing import Any, Final, Literal, cast, overload

from domainkit import clock
from domainkit.ddd._dataclass import EntityMeta
from domainkit.ddd.exceptions import Conflict, DuplicateValue, MultipleFound, NotFound
from domainkit.ddd.fields import InvalidQuery, primitive
from domainkit.ddd.model import AggregateRoot
from domainkit.ddd.pagination import Page
from domainkit.ddd.preconditions import Expectation, check_expected
from domainkit.ddd.query import (
    ID,
    Criteria,
    CriteriaLike,
    F,
    Measure,
    Ordering,
    RowLock,
    Selection,
    as_criteria,
    as_ordering,
    as_query,
    following,
    order,
    position_of,
    resolve,
    total_ordering,
)
from domainkit.ddd.tenancy import (
    TENANT_ID_FIELD,
    TenantOwned,
    claim,
    row_tenant,
    visible,
)

DELETED_AT: Final = "deleted_at"
LOCK_TIMEOUT: Final = 10.0
"""Seconds an in-memory row lock is waited for, as ``LOCK_TIMEOUT`` defaults."""


class LockNotAvailable(Conflict):
    """Another transaction holds the row's lock, and the caller would not wait."""

    code = "lock_not_available"


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
        """Mark the aggregate deleted at ``when`` (default: ``clock.now()``)."""
        self.deleted_at = when or clock.now()

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
    _expected: Sequence[Expectation] = ()
    """What the unit of work that lent the repository out expects
    (``UnitOfWork.expect``): the same list, which it changes."""

    def __init__(self) -> None:
        """Start with no tracked aggregates."""
        self._seen: dict[AggregateT, int] = {}
        """Each tracked aggregate, with how many events it had when first seen."""

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
        """Store a new aggregate (a ``TenantOwned`` one, the current tenant's)."""
        claim(aggregate)
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

        A soft-deleted aggregate counts as missing unless ``include_deleted``,
        and a ``TenantOwned`` one of another tenant always does.

        Raises:
            PreconditionFailed: The use case expects another version of it
                (see ``domainkit.ddd.preconditions``).
        """
        aggregate = await self._get(id_)
        if (
            aggregate is None
            or (_is_deleted(aggregate) and not include_deleted)
            or not visible(aggregate)
        ):
            return None
        check_expected(aggregate, self._expected)
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
        self, query: Selection | CriteriaLike | None = None
    ) -> list[AggregateT]:
        """The aggregates ``query`` selects, in its ordering, then by id.

        ``query`` may also be bare criteria or a specification (everything
        matching, by id), or None (every aggregate).

        Raises:
            InvalidQuery: The query names an unknown field, or a value that
                does not fit its field.
            LockNotAvailable: The query locks rows with ``nowait`` and one
                is locked.
            PreconditionFailed: The use case expects another version of an
                aggregate found.
        """
        found = await self._filter(self._prepare(self._ordered(as_query(query))))
        for aggregate in found:
            check_expected(aggregate, self._expected)
            self._track(aggregate)
        return found

    async def first(
        self, query: Selection | CriteriaLike | None = None
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

    async def aggregate(
        self,
        criteria: CriteriaLike | None = None,
        *,
        include_deleted: bool = False,
        **measures: Measure,
    ) -> dict[str, object]:
        """Measures over the aggregates ``criteria`` select, in one statement.

        Django's ``aggregate()``::

            await uow.orders.aggregate(
                F("status") == "paid", orders=Count(), revenue=Sum("total_cents")
            )  # {"orders": 3, "revenue": 4200}

        Nothing is loaded or tracked.

        Raises:
            TypeError: No measure is given.
            InvalidQuery: A measure does not fit its field (``Measure.bind``).
        """
        if not measures:
            raise TypeError("aggregate() takes measures, as total=Sum('amount').")
        model = self.aggregate_type
        bound = {name: measure.bind(model) for name, measure in measures.items()}
        return await self._aggregate(
            self._scope(criteria, include_deleted=include_deleted), bound
        )

    async def paginate(
        self,
        query: Selection | CriteriaLike | None = None,
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
        base = self._ordered(as_query(query))
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

    async def values(
        self, query: Selection | CriteriaLike | None, /, *fields: str
    ) -> list[dict[str, object]]:
        """``fields`` of the aggregates ``query`` selects, as dicts, in its order.

        Django's ``values()``: one statement that reads the columns it names,
        for a listing or an export that needs a few fields of many
        aggregates. Nothing is loaded or tracked, so nothing read this way
        can be changed and committed. A query without an ordering follows
        ``Meta.ordering``, then the id; a limit and an offset apply.

        Raises:
            TypeError: No field is named.
            InvalidQuery: A field is a child entity, is encrypted, names
                nothing, or the query locks rows.
        """
        if not fields:
            raise TypeError("values() takes field names, as values(query, 'id').")
        base = self._ordered(as_query(query))
        if base.lock is not None:
            raise InvalidQuery("values() reads without locking; use filter() to lock.")
        model = self.aggregate_type
        for name in fields:
            path = resolve(model, name)
            if len(path) > 1:
                raise InvalidQuery(
                    f"values() reads fields of {model.__name__} itself, not {name!r}."
                )
            if path[0].encrypted:
                raise InvalidQuery(
                    f"{model.__name__}.{name} is encrypted: load the aggregate "
                    "to read it."
                )
        return await self._values(self._prepare(base), fields)

    async def stream(
        self, query: Selection | CriteriaLike | None = None, *, batch: int = 500
    ) -> AsyncIterator[AggregateT]:
        """Every aggregate ``query`` selects, in its ordering, ``batch`` at a time.

        Django's ``iterator()``: each batch is one statement that starts
        after the last aggregate of the one before (keyset), so no statement
        returns more than ``batch`` rows and none is held open between them.
        Each aggregate is tracked as ``filter`` tracks it, so its changes and
        events are committed with the unit of work; a read-only unit of work
        (``readonly=True``) is the one for reading a large table through.

        Raises:
            ValueError: ``batch`` is below 1, or ``query`` has a ``limit`` or
                an ``offset`` (a page is ``paginate``'s).
        """
        if batch < 1:
            raise ValueError(f"A batch holds at least one aggregate, not {batch}.")
        base = self._ordered(as_query(query))
        if base.limit is not None or base.offset:
            raise ValueError(
                "stream() reads everything a query selects; take a page with "
                "paginate(limit=..., offset=...)."
            )
        ordering = total_ordering(base.ordering)
        after = base.after
        while True:
            found = await self.filter(
                base.replace(ordering=ordering, limit=batch, after=after)
            )
            for aggregate in found:
                yield aggregate
            if len(found) < batch:
                return
            after = position_of(found[-1], ordering)

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
        found = await self.first(Selection(F(ID) == id_, lock=lock))
        if found is None and not skip_locked:
            raise self.not_found(id_)
        return found

    async def get_many_for_update(
        self, ids: Iterable[IdT], *, nowait: bool = False, skip_locked: bool = False
    ) -> dict[IdT, AggregateT]:
        """The aggregates ``ids``, their rows locked in the order of their ids.

        The rows are locked in one statement, by ascending id, whatever the
        order of ``ids``: two transactions that lock overlapping sets this
        way take the locks in the same order, so one waits for the other
        and they cannot deadlock. The answer maps each id to its aggregate,
        by ascending id. ``nowait`` raises ``LockNotAvailable`` instead of
        waiting; with ``skip_locked``, locked (and missing) aggregates are
        left out. SQLite has no row locks: there they are read without one.

        Raises:
            NotFound: An id names no aggregate (``not_found(id_)``).
            LockNotAvailable: Another transaction holds a lock, with ``nowait``.
        """
        wanted = list(dict.fromkeys(ids))
        if not wanted:
            return {}
        lock = RowLock(nowait=nowait, skip_locked=skip_locked)
        selection = Selection(F(ID).in_(wanted), lock=lock)
        found = {aggregate.id: aggregate for aggregate in await self.filter(selection)}
        if not skip_locked:
            for id_ in wanted:
                if id_ not in found:
                    raise self.not_found(id_)
        return found

    async def get_many(
        self, ids: Iterable[IdT], *, include_deleted: bool = False
    ) -> dict[IdT, AggregateT]:
        """The aggregates of ``ids`` that exist, by ascending id, in one statement.

        Django's ``in_bulk``: a missing id is left out, not an error
        (``get_many_for_update`` locks them and refuses one missing).
        """
        wanted = list(dict.fromkeys(ids))
        if not wanted:
            return {}
        selection = Selection(F(ID).in_(wanted), include_deleted=include_deleted)
        return {aggregate.id: aggregate for aggregate in await self.filter(selection)}

    async def get_or_create(
        self, criteria: CriteriaLike, create: Callable[[], AggregateT]
    ) -> tuple[AggregateT, bool]:
        """The aggregate ``criteria`` select, or a new one ``create()`` makes.

        The answer says whether it was created. A new aggregate is stored
        at once (on a database, in a savepoint): if another transaction
        stored a matching one first and a unique constraint refuses this
        one, that one is read and returned instead. Without a unique
        constraint on what ``criteria`` name, two transactions at once can
        each create one.

        Raises:
            MultipleFound: ``criteria`` select more than one aggregate.
            DuplicateValue: A unique constraint refused the new aggregate,
                and no aggregate meets ``criteria``.
        """
        found = await self._one(criteria)
        if found is not None:
            return found, False
        aggregate = create()
        claim(aggregate)
        try:
            await self._add_now(aggregate)
        except DuplicateValue:
            found = await self._one(criteria)
            if found is None:
                raise
            return found, False
        self._track(aggregate)
        return aggregate, True

    async def update_or_create(
        self,
        criteria: CriteriaLike,
        update: Callable[[AggregateT], object],
        create: Callable[[], AggregateT],
    ) -> tuple[AggregateT, bool]:
        """Change the aggregate ``criteria`` select, or store a new one.

        The aggregate found is read with its row locked (as
        ``get_for_update``), then ``update(aggregate)`` changes it, through
        its own methods. Otherwise ``create()`` makes one, stored as
        ``get_or_create`` stores it; if another transaction stored it first,
        that one is locked and updated instead. The answer says whether it
        was created.

        Raises:
            MultipleFound: ``criteria`` select more than one aggregate.
        """
        found = await self._one(criteria, lock=RowLock())
        if found is None:
            aggregate, created = await self.get_or_create(criteria, create)
            if created:
                return aggregate, True
            found = await self._one(criteria, lock=RowLock())
            if found is None:  # Removed again since; one more try.
                return await self.get_or_create(criteria, create)
        update(found)
        return found, False

    async def _one(
        self, criteria: CriteriaLike, *, lock: RowLock | None = None
    ) -> AggregateT | None:
        """The one aggregate ``criteria`` select, or None.

        Raises:
            MultipleFound: They select more than one.
        """
        found = await self.filter(Selection(criteria, limit=2, lock=lock))
        if len(found) > 1:
            raise MultipleFound(
                f"More than one {self.aggregate_type.__name__} meets the criteria."
            )
        return found[0] if found else None

    async def _add_now(self, aggregate: AggregateT) -> None:
        """Store a new aggregate at once, so a unique constraint answers now.

        Raises:
            DuplicateValue: A unique constraint refused it; nothing is stored.
        """
        await self._add(aggregate)

    def not_found(self, id_: IdT) -> NotFound:
        """Build the error for a missing aggregate; override for a domain error."""
        return NotFound(f"No aggregate has id {id_}.")

    def _track(self, aggregate: AggregateT) -> None:
        """Remember ``aggregate`` so its events are collected on commit."""
        self._seen.setdefault(aggregate, len(aggregate.pending_events))

    def _scope(
        self, criteria: CriteriaLike | None, *, include_deleted: bool
    ) -> Criteria | None:
        """``criteria`` bound to the aggregate, as every read sees it.

        Soft-deleted aggregates are left out, and a ``TenantOwned`` one's
        rows are the current tenant's only.
        """
        model = self.aggregate_type
        parts = [] if criteria is None else [as_criteria(criteria)]
        if issubclass(model, SoftDeletable) and not include_deleted:
            parts.append(F(DELETED_AT).isnull())
        if issubclass(model, TenantOwned) and (owner := row_tenant()) is not None:
            parts.append(F(TENANT_ID_FIELD) == owner)
        if not parts:
            return None
        joined = parts[0]
        for part in parts[1:]:
            joined &= part
        return joined.bind(model)

    def _ordered(self, query: Selection) -> Selection:
        """``query``, in the aggregate's ``Meta.ordering`` if it names none."""
        if query.ordering:
            return query
        default = default_ordering(self.aggregate_type)
        return query.replace(ordering=default) if default else query

    def _prepare(self, query: Selection) -> Selection:
        """``query`` bound to the aggregate: total ordering, position as criteria."""
        model = self.aggregate_type
        ordering = tuple(item.bind(model) for item in total_ordering(query.ordering))
        criteria = query.criteria
        if query.after is not None:
            beyond = following(model, ordering, query.after)
            criteria = beyond if criteria is None else criteria & beyond
        return Selection(
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
        """Go back to ``mark``: forget what was tracked since, and its events.

        An aggregate first seen since keeps the events it had then: a
        session hands the same object back if it is loaded again.
        """
        for aggregate, had in list(self._seen.items()):
            kept = aggregate.pull_events()[: mark.events.get(aggregate, had)]
            for event in kept:
                aggregate.record(event)
            if aggregate not in mark.events:
                del self._seen[aggregate]
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

    async def _filter(self, query: Selection) -> list[AggregateT]:
        """Load what the bound ``query`` selects, in its (total) ordering."""
        raise NotImplementedError(f"{type(self).__name__} cannot filter.")

    async def _count(self, criteria: Criteria | None) -> int:
        """Count what the bound ``criteria`` select."""
        raise NotImplementedError(f"{type(self).__name__} cannot count.")

    async def _exists(self, criteria: Criteria | None) -> bool:
        """Whether the bound ``criteria`` select anything."""
        return await self._count(criteria) > 0

    async def _values(
        self, query: Selection, fields: Sequence[str]
    ) -> list[dict[str, object]]:
        """The ``fields`` of what the bound ``query`` selects, in its order."""
        return [
            {name: getattr(aggregate, name) for name in fields}
            for aggregate in await self._filter(query)
        ]

    async def _aggregate(
        self, criteria: Criteria | None, measures: dict[str, Measure]
    ) -> dict[str, object]:
        """Work out the bound ``measures`` over what ``criteria`` select."""
        raise NotImplementedError(f"{type(self).__name__} cannot aggregate.")


@cache
def default_ordering(model: type) -> tuple[Ordering, ...]:
    """The aggregate's ``Meta.ordering`` (Django's), checked.

    Queries that name no ordering are in it, then by id.

    Raises:
        TypeError: It is not a tuple of field names, as ``("-placed_at",)``.
        InvalidQuery: A name is no field of the model, or one that does not
            sort.
    """
    declared = getattr(getattr(model, "Meta", None), "ordering", ())
    if isinstance(declared, str) or not isinstance(declared, (tuple, list)):
        raise TypeError(
            f"{model.__qualname__}.Meta.ordering is a tuple of field names, as "
            f"('-placed_at',), not {declared!r}."
        )
    return tuple(as_ordering(each).bind(model) for each in declared)


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

    async def _filter(self, query: Selection) -> list[AggregateT]:
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

    async def _aggregate(
        self, criteria: Criteria | None, measures: dict[str, Measure]
    ) -> dict[str, object]:
        """Each measure over the matching aggregates' values that are set."""
        matching = self._matching(criteria)
        answer: dict[str, object] = {}
        for name, measure in measures.items():
            if measure.field is None:
                answer[name] = len(matching)
                continue
            values = [
                value
                for aggregate in matching
                if (value := primitive(getattr(aggregate, measure.field))) is not None
            ]
            answer[name] = measure.present(self.aggregate_type, measure.compute(values))
        return answer

    def _matching(self, criteria: Criteria | None) -> list[AggregateT]:
        """The stored aggregates that meet the bound ``criteria``."""
        return [
            aggregate
            for aggregate in self.items.values()
            if criteria is None or criteria.evaluate(aggregate)
        ]

    def _snapshot(self) -> object:
        """The stored aggregates, their entities' fields, and their events' count."""
        return (
            dict(self.items),
            {key: _fields(aggregate) for key, aggregate in self.items.items()},
            {
                key: len(aggregate.pending_events)
                for key, aggregate in self.items.items()
            },
        )

    def _restore(self, snapshot: object) -> None:
        """Put the stored aggregates, their entities' fields, and their events back.

        Events recorded since are dropped, also by an aggregate loaded only
        after the snapshot (``_rewind`` drops those of aggregates seen before).
        """
        items, states, events = cast(
            "tuple[dict[IdT, AggregateT], dict[IdT, Fields], dict[IdT, int]]",
            snapshot,
        )
        self.items.clear()
        self.items.update(items)
        for key, aggregate in items.items():
            for entity, fields in states[key]:
                for name, value in fields.items():
                    setattr(entity, name, value)
            for event in aggregate.pull_events()[: events[key]]:
                aggregate.record(event)

    def _finished(self) -> None:
        """Release this repository's row locks."""
        self.locks.release(self)


type Fields = list[tuple[object, dict[str, object]]]
"""Each mutable object of an aggregate, with the values its fields had."""


def _fields(aggregate: object) -> Fields:
    """The fields of ``aggregate`` and of every entity it holds, to put back.

    Entities (mutable dataclasses) are kept by identity and their fields are
    recorded, so a rewind restores them in place. They are never copied: an
    entity mapped to a table carries the mapper's state, which a deep copy
    would corrupt. Lists, tuples, sets, and dicts are copied, as plain ones,
    around the entities they hold; any other value is deep-copied.
    """
    recorded: Fields = []
    seen: set[int] = set()

    def keep(value: object) -> object:
        if _is_entity(value):
            visit(value)
            return value
        if isinstance(value, list):
            return [keep(item) for item in value]
        if isinstance(value, tuple) and type(value) is tuple:
            return tuple(keep(item) for item in value)
        if isinstance(value, frozenset | set):
            items = (keep(item) for item in value)
            return frozenset(items) if isinstance(value, frozenset) else set(items)
        if isinstance(value, dict):
            return {key: keep(item) for key, item in value.items()}
        return copy.deepcopy(value)

    def visit(entity: object) -> None:
        if id(entity) in seen:
            return
        seen.add(id(entity))
        fields = dataclasses.fields(entity)  # type: ignore[arg-type]
        recorded.append(
            (
                entity,
                {field.name: keep(getattr(entity, field.name)) for field in fields},
            )
        )

    visit(aggregate)
    return recorded


def _is_entity(value: object) -> bool:
    """Whether ``value`` is a mutable dataclass instance, kept by identity."""
    if isinstance(value, type) or not dataclasses.is_dataclass(value):
        return False
    return not cast(Any, type(value)).__dataclass_params__.frozen
