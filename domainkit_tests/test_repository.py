"""In-memory repositories and units of work: queries, locks, soft delete, savepoints."""

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from domainkit.clock import freeze_time
from domainkit.ddd import (
    AggregateRoot,
    DomainEvent,
    EventBus,
    InMemoryRepository,
    LockNotAvailable,
    NotFound,
    ReadOnlyError,
    RowLocks,
    SoftDeletable,
    UnitOfWork,
)
from domainkit.ddd.query import F, Query, RowLock
from domainkit_tests.gadgets import (
    Color,
    FakeGadgetsUnitOfWork,
    Gadget,
    GadgetId,
    GadgetMade,
    InMemoryGadgets,
    make_gadget,
    make_part,
)

pytestmark = pytest.mark.anyio


class Note(AggregateRoot[int]):
    """An aggregate that is not soft-deletable."""

    text: str


def stocked(*names: str) -> InMemoryGadgets:
    """A repository holding gadgets with these names, stock = position."""
    return InMemoryGadgets(
        make_gadget(name, stock=index) for index, name in enumerate(names)
    )


async def test_the_aggregate_class_comes_from_model_or_type_arguments() -> None:
    """A class attribute, a subclass's type argument, or the alias called."""

    class Named(InMemoryRepository[Any, int]):
        model = Note

    class Bare(InMemoryRepository):  # type: ignore[type-arg]
        """No type argument."""

    assert InMemoryGadgets().aggregate_type is Gadget
    assert Named().aggregate_type is Note
    assert InMemoryRepository[Note, int]().aggregate_type is Note
    with pytest.raises(TypeError, match="Bare does not say which aggregate"):
        Bare().aggregate_type  # noqa: B018 - raising is the point.
    with pytest.raises(TypeError, match="does not say"):
        await Bare().filter()


async def test_queries_select_order_and_track() -> None:
    """filter, first, count, and exists, with every result tracked."""
    repository = stocked("a", "b", "c")

    found = await repository.filter(Query(F("stock") >= 1, ordering=["-stock"]))
    assert [each.name for each in found] == ["c", "b"]
    assert repository.seen == tuple(found)
    assert [each.name for each in await repository.filter(F("name") == "a")] == ["a"]
    assert len(await repository.filter()) == 3
    first = await repository.first(Query(ordering=["name"]))
    assert first is not None
    assert first.name == "a"
    assert await repository.first(F("name") == "z") is None
    assert await repository.count() == 3
    assert await repository.count(F("stock") > 0) == 2
    assert await repository.exists(F("name") == "b")
    assert not await repository.exists(F("name") == "z")
    limited = await repository.filter(Query(ordering=["name"], limit=1, offset=1))
    assert [each.name for each in limited] == ["b"]


async def test_results_without_an_ordering_come_by_id() -> None:
    """The order is total: the id, when nothing else is asked."""
    repository = stocked(*"abcdef")

    assert [each.id for each in await repository.filter()] == sorted(repository.items)


async def test_pages_continue_after_the_last_position() -> None:
    """Keyset pages cover everything once; offsets and totals as asked."""
    repository = stocked(*"abcde")
    query = Query(F("stock") > 0, ordering=["-name"])

    first = await repository.paginate(query, limit=2, count=True)
    assert [each.name for each in first.items] == ["e", "d"]
    assert first.has_more is True
    assert first.total == 4
    assert first.next_position == ("d", first.items[-1].id)
    second = await repository.paginate(query, limit=2, after=first.next_position)
    assert [each.name for each in second.items] == ["c", "b"]
    assert second.has_more is False
    assert second.next_position is None
    assert second.total is None
    skipped = await repository.paginate(query, limit=3, offset=3)
    assert [each.name for each in skipped.items] == ["b"]
    with pytest.raises(ValueError, match="at least one item, not 0"):
        await repository.paginate(query, limit=0)
    with pytest.raises(ValueError, match="not both"):
        await repository.paginate(query, limit=1, offset=1, after=("a", "b"))


async def test_bulk_adds_and_removes_keep_their_events() -> None:
    """Each aggregate added or removed in bulk is tracked."""
    events = EventBus()
    published: list[DomainEvent] = []

    async def publish(event: GadgetMade) -> None:
        published.append(event)

    events.subscribe(GadgetMade, publish)
    uow = FakeGadgetsUnitOfWork(InMemoryGadgets(), events=events)
    one, two = make_gadget("one"), make_gadget("two")
    one.rename("uno")
    async with uow:
        await uow.gadgets.add_all([one, two])
        await uow.commit()
    assert set(uow.gadgets.items) == {one.id, two.id}
    two.rename("dos")
    async with uow:
        await uow.gadgets.remove_all([one, two], hard=True)
        await uow.commit()
    assert uow.gadgets.items == {}
    assert [event.gadget_id for event in published] == [one.id, two.id]  # type: ignore[attr-defined]


async def test_soft_deleted_aggregates_are_left_out_unless_asked() -> None:
    """Removing marks; reads skip; include_deleted and restore bring them back."""
    repository = stocked("kept", "gone")
    gone = next(each for each in repository.items.values() if each.name == "gone")

    await repository.remove(gone)
    assert gone.is_deleted
    assert gone.deleted_at is not None
    assert gone.id in repository.items
    assert await repository.find(gone.id) is None
    assert await repository.find(gone.id, include_deleted=True) is gone
    with pytest.raises(NotFound):
        await repository.get(gone.id)
    assert await repository.count() == 1
    assert await repository.count(include_deleted=True) == 2
    assert not await repository.exists(F("name") == "gone")
    assert await repository.exists(F("name") == "gone", include_deleted=True)
    assert [each.name for each in await repository.filter()] == ["kept"]
    every = await repository.filter(Query(ordering=["name"], include_deleted=True))
    assert [each.name for each in every] == ["gone", "kept"]
    trash = await repository.filter(
        Query(F("deleted_at").isnull(False), include_deleted=True)
    )
    assert trash == [gone]
    assert await repository.restore(gone.id) is gone
    assert not gone.is_deleted
    assert await repository.count() == 2
    await repository.remove(gone, hard=True)
    assert gone.id not in repository.items


async def test_only_soft_deletable_aggregates_are_restored() -> None:
    """Others are removed for good, and refuse restore."""
    note = Note(id=1, text="x")
    repository = InMemoryRepository[Note, int]([note])

    with pytest.raises(TypeError, match="Note is not SoftDeletable"):
        await repository.restore(1)
    await repository.remove(note)
    assert repository.items == {}
    assert await repository.count() == 0


async def test_aggregates_mark_their_own_deletion() -> None:
    """mark_deleted takes a time, or the clock's now."""
    when = datetime(2026, 3, 1, tzinfo=UTC)
    gadget = make_gadget()

    gadget.mark_deleted(when)
    assert gadget.deleted_at == when
    gadget.restore()
    assert gadget.deleted_at is None
    with freeze_time("2026-04-01T12:00:00Z"):
        gadget.mark_deleted()
    assert gadget.deleted_at == datetime(2026, 4, 1, 12, tzinfo=UTC)
    assert isinstance(gadget, SoftDeletable)


async def test_a_locked_aggregate_is_waited_for() -> None:
    """Two fakes sharing items and locks act as two transactions."""
    locks = RowLocks()
    first = FakeGadgetsUnitOfWork(stocked("a"))
    second = FakeGadgetsUnitOfWork(InMemoryGadgets(locks=locks))
    first.gadgets.locks = locks
    second.gadgets.items = first.gadgets.items
    (gadget,) = first.gadgets.items.values()

    async with first, second:
        assert await first.gadgets.get_for_update(gadget.id) is gadget
        assert locks.holder(gadget.id) is first.gadgets
        assert await first.gadgets.get_for_update(gadget.id) is gadget  # Held already.
        waiting = asyncio.create_task(second.gadgets.get_for_update(gadget.id))
        done, _ = await asyncio.wait({waiting}, timeout=0.05)
        assert not done
        with pytest.raises(LockNotAvailable, match="being changed by another request"):
            await second.gadgets.get_for_update(gadget.id, nowait=True)
        assert await second.gadgets.get_for_update(gadget.id, skip_locked=True) is None
        await first.commit()
        assert await waiting is gadget
        assert locks.holder(gadget.id) is second.gadgets
        await second.rollback()
        assert locks.holder(gadget.id) is None
    assert LockNotAvailable.code == "lock_not_available"


async def test_locks_time_out_and_skipped_rows_do_not_count() -> None:
    """A wait past the timeout fails; skip_locked fills its limit with others."""
    locks = RowLocks(timeout=0.01)
    holder = stocked("a", "b", "c")
    holder.locks = locks
    other = InMemoryGadgets(locks=locks)
    other.items = holder.items
    query = Query(ordering=["name"], limit=1)

    (locked,) = await holder.filter(query.replace(lock=RowLock()))
    assert locked.name == "a"
    with pytest.raises(LockNotAvailable):
        await other.filter(query.replace(lock=RowLock()))
    skipping = await other.filter(
        query.replace(limit=2, lock=RowLock(skip_locked=True))
    )
    assert [each.name for each in skipping] == ["b", "c"]
    with pytest.raises(LockNotAvailable):
        await other.filter(Query(lock=RowLock(nowait=True)))
    everything = await holder.filter(
        Query(ordering=["name"], lock=RowLock(skip_locked=True))
    )
    assert [each.name for each in everything] == ["a"]  # b and c are the other's.
    locks.release(other)
    locks.release(holder)
    assert locks.holder(locked.id) is None


async def test_get_for_update_reports_missing_aggregates() -> None:
    """NotFound, unless locked rows are skipped: then None."""
    repository = stocked("a")
    missing = GadgetId(make_gadget().id)

    with pytest.raises(NotFound):
        await repository.get_for_update(missing)
    assert await repository.get_for_update(missing, skip_locked=True) is None


async def test_read_only_work_refuses_to_commit() -> None:
    """Read-only on the instance, or the class; reads still work."""

    class Reports(FakeGadgetsUnitOfWork):
        readonly = True

    reading = FakeGadgetsUnitOfWork(stocked("a"), readonly=True)
    async with reading:
        assert await reading.gadgets.count() == 1
        with pytest.raises(ReadOnlyError, match="FakeGadgetsUnitOfWork is read-only"):
            await reading.commit()
    async with Reports(stocked()) as reports:
        with pytest.raises(ReadOnlyError):
            await reports.commit()
    writable = Reports(stocked(), readonly=False)
    async with writable:
        await writable.commit()
    assert (reading.commits, writable.commits) == (0, 1)
    assert FakeGadgetsUnitOfWork(stocked()).readonly is False


async def test_a_failed_savepoint_undoes_its_part_only() -> None:
    """Changes, additions, removals, events, and tasks after the savepoint go."""
    uow = FakeGadgetsUnitOfWork(stocked("a", "b"))
    a, b = await uow.gadgets.filter(Query(ordering=["name"]))
    a.rename("first")
    added = make_gadget("added")

    async def task() -> None: ...

    async def reserve() -> None:
        async with uow.savepoint():
            a.rename("second")
            a.parts.append(make_part("P"))
            a.color = Color.BLUE
            added.rename("new")
            await uow.gadgets.add(added)
            await uow.gadgets.remove(b, hard=True)
            uow.defer(task, inside=True)
            raise RuntimeError("out of stock")

    async with uow:
        uow.defer(task, before=True)
        with pytest.raises(RuntimeError, match="out of stock"):
            await reserve()
        assert a.name == "first"
        assert a.parts == []
        assert a.color is Color.RED
        assert set(uow.gadgets.items) == {a.id, b.id}
        assert uow.gadgets.seen == (a, b)
        assert len(a.pending_events) == 1
        assert len(added.pending_events) == 1  # Its own, kept if added again.
        await uow.commit()
    assert [each.kwargs for each in uow.queued_tasks] == [{"before": True}]
    assert not a.pending_events


async def test_a_savepoint_that_succeeds_keeps_its_part() -> None:
    """Nested savepoints: an inner failure leaves the outer's work."""
    uow = FakeGadgetsUnitOfWork(stocked("a"))
    (a,) = await uow.gadgets.filter()

    async def inner() -> None:
        async with uow.savepoint():
            a.rename("inner")
            raise ValueError("inner")

    async with uow:
        async with uow.savepoint():
            a.rename("outer")
            with pytest.raises(ValueError, match="inner"):
                await inner()
            assert a.name == "outer"
            with pytest.raises(RuntimeError, match="Leave every savepoint"):
                await uow.commit()
        await uow.commit()
    assert a.name == "outer"
    assert uow.commits == 1


async def test_savepoints_may_translate_what_they_raise() -> None:
    """A unit of work's _translate replaces the error, chained to it."""

    class Translating(FakeGadgetsUnitOfWork):
        def _translate(self, exc: BaseException) -> BaseException | None:
            return LookupError("translated") if isinstance(exc, KeyError) else None

    uow = Translating(stocked())
    async with uow:
        with pytest.raises(LookupError, match="translated") as caught:
            async with uow.savepoint():
                raise KeyError("x")
        assert isinstance(caught.value.__cause__, KeyError)
        with pytest.raises(ValueError, match="kept"):
            async with uow.savepoint():
                raise ValueError("kept")


async def test_units_of_work_without_repositories_have_savepoints() -> None:
    """The base unit of work needs nothing of its subclasses for savepoints."""

    class Empty(UnitOfWork):
        async def _commit(self) -> None: ...

        async def _rollback(self) -> None: ...

    async with Empty(events=EventBus()) as uow:
        async with uow.savepoint():
            pass
        await uow.commit()
