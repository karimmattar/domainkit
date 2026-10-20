"""In-memory repositories and units of work: queries, locks, soft delete, savepoints."""

import asyncio
import re
from datetime import UTC, datetime
from typing import Any, NamedTuple

import pytest

from domainkit.clock import freeze_time
from domainkit.ddd import (
    AggregateRoot,
    DomainEvent,
    Entity,
    EventBus,
    InMemoryRepository,
    LockNotAvailable,
    NotFound,
    ReadOnlyError,
    Repository,
    RowLocks,
    SoftDeletable,
    UnitOfWork,
    ValueObject,
)
from domainkit.ddd.query import F, Query, RowLock
from domainkit.ddd.repository import LOCK_TIMEOUT
from domainkit_tests.gadgets import (
    Color,
    FakeGadgetsUnitOfWork,
    Gadget,
    GadgetId,
    GadgetMade,
    InMemoryGadgets,
    make_gadget,
    make_maker,
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
    async with asyncio.timeout(1):  # Fails rather than hangs if the wait is endless.
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


async def test_errors_name_the_missing_aggregate_and_the_refused_lock() -> None:
    """Both get and get_for_update name the id; a refused lock says try again."""
    repository = stocked("a")
    (gadget,) = repository.items.values()
    other = InMemoryGadgets(locks=repository.locks)
    other.items = repository.items
    missing = GadgetId(make_gadget().id)

    with pytest.raises(NotFound, match=re.escape(f"No aggregate has id {missing}.")):
        await repository.get(missing)
    with pytest.raises(NotFound, match=re.escape(f"No aggregate has id {missing}.")):
        await repository.get_for_update(missing)
    await repository.get_for_update(gadget.id)
    message = "The data is being changed by another request; try again."
    with pytest.raises(LockNotAvailable, match=f"^{re.escape(message)}$"):
        await other.get_for_update(gadget.id, nowait=True)


async def test_a_lock_is_waited_for_until_the_timeout_only() -> None:
    """LOCK_TIMEOUT seconds unless given; then the waiter gives up."""
    locks = RowLocks(timeout=0.01)
    holder = stocked("a")
    holder.locks = locks
    waiter = InMemoryGadgets(locks=locks)
    waiter.items = holder.items
    (gadget,) = holder.items.values()

    assert RowLocks().timeout == LOCK_TIMEOUT == 10.0
    assert locks.timeout == 0.01
    await holder.get_for_update(gadget.id)
    async with asyncio.timeout(1):  # Fails rather than hangs if the wait is endless.
        with pytest.raises(LockNotAvailable):
            await waiter.get_for_update(gadget.id)
    assert locks.holder(gadget.id) is holder


async def test_bulk_removes_mark_soft_deletable_aggregates_unless_hard() -> None:
    """remove_all marks, as remove does, and keeps the aggregates stored."""
    repository = stocked("a", "b")
    both = list(repository.items.values())

    await repository.remove_all(both)
    assert all(each.is_deleted for each in both)
    assert set(repository.items) == {each.id for each in both}
    assert await repository.count() == 0
    assert await repository.count(include_deleted=True) == 2


async def test_first_reads_and_locks_one_aggregate_only() -> None:
    """The others it might have matched are neither tracked nor locked."""
    repository = stocked("a", "b", "c")

    first = await repository.first(Query(ordering=["name"], lock=RowLock()))
    assert first is not None
    assert first.name == "a"
    assert repository.seen == (first,)
    locked = [key for key in repository.items if repository.locks.holder(key)]
    assert locked == [first.id]


async def test_a_page_reads_one_aggregate_past_its_end_and_no_further() -> None:
    """The one past the end says whether more follow; the rest stay unread."""
    repository = stocked(*"abcde")

    page = await repository.paginate(Query(ordering=["name"]), limit=2)
    assert [each.name for each in page.items] == ["a", "b"]
    assert page.has_more is True
    assert [each.name for each in repository.seen] == ["a", "b", "c"]


async def test_page_totals_count_deleted_aggregates_when_the_query_does() -> None:
    """The total follows the query's include_deleted, as its items do."""
    repository = stocked("a", "b", "c")
    await repository.remove(next(iter(repository.items.values())))

    every = await repository.paginate(Query(include_deleted=True), limit=1, count=True)
    assert every.total == 3
    live = await repository.paginate(Query(), limit=1, count=True)
    assert live.total == 2


async def test_adapters_are_told_whether_deleted_aggregates_are_wanted() -> None:
    """The bound query handed to _filter keeps the query's include_deleted."""

    class Recording(InMemoryGadgets):
        """Keeps each bound query it is asked."""

        def __init__(self) -> None:
            super().__init__()
            self.queries: list[Query] = []

        async def _filter(self, query: Query) -> list[Gadget]:
            self.queries.append(query)
            return await super()._filter(query)

    repository = Recording()
    await repository.filter(Query(include_deleted=True))
    await repository.filter()
    assert [each.include_deleted for each in repository.queries] == [True, False]


async def test_a_failed_savepoint_forgets_all_it_added_and_keeps_earlier_events() -> (
    None
):
    """Every aggregate added inside is untracked; earlier events stay as they were."""
    uow = FakeGadgetsUnitOfWork(stocked("a"))
    one, two = make_gadget("one"), make_gadget("two")

    async def undone() -> None:
        async with uow.savepoint():
            await uow.gadgets.add_all([one, two])
            a.rename("during")
            raise RuntimeError("undone")

    async with uow:
        (a,) = await uow.gadgets.filter()
        a.rename("before")
        (before,) = a.pending_events
        with pytest.raises(RuntimeError, match="undone"):
            await undone()
        assert uow.gadgets.seen == (a,)
        assert a.pending_events == (before,)
        assert set(uow.gadgets.items) == {a.id}


class Line(Entity[int]):
    """A child entity no table maps."""

    quantity: int


class Order(AggregateRoot[int]):
    """An aggregate whose child entities live in memory only."""

    lines: list[Line]


async def test_a_failed_savepoint_undoes_changes_inside_child_entities() -> None:
    """The snapshot copies fields deeply, so a line changed in place comes back."""
    order = Order(id=1, lines=[Line(id=1, quantity=1)])

    class Orders(UnitOfWork):
        """Orders in memory."""

        def __init__(self) -> None:
            super().__init__(events=EventBus())
            self.orders = InMemoryRepository[Order, int]([order])

        async def _commit(self) -> None: ...

        async def _rollback(self) -> None: ...

    uow = Orders()

    async def undone() -> None:
        async with uow.savepoint():
            order.lines[0].quantity = 9
            raise RuntimeError("undone")

    async with uow:
        with pytest.raises(RuntimeError, match="undone"):
            await undone()
    assert [line.quantity for line in order.lines] == [1]


class Memo(ValueObject):
    """A value object with a mutable field, copied deeply rather than kept."""

    words: list[str]


class Shelf(NamedTuple):
    """A tuple subclass, copied deeply rather than rebuilt."""

    first: Line
    second: Line


class Slot(Entity[int]):
    """A child entity that points back at its aggregate."""

    basket: object


class Basket(AggregateRoot[int]):
    """Child entities in every kind of container, and values beside them."""

    lines: list[Line]
    by_name: dict[str, Line]
    pair: tuple[Line, Line]
    shelf: Shelf
    tags: set[str]
    kinds: frozenset[str]
    memo: Memo
    slots: list[Slot]
    model: type


async def test_a_failed_savepoint_puts_child_entities_back_in_place() -> None:
    """Entities keep their identity; containers and values are as they were."""
    one, two, three = (
        Line(id=1, quantity=1),
        Line(id=2, quantity=2),
        Line(id=3, quantity=3),
    )
    five = Line(id=5, quantity=5)
    basket = Basket(
        id=1,
        lines=[one, two],
        by_name={"one": one, "five": five},
        pair=(one, three),
        shelf=Shelf(two, three),
        tags={"a"},
        kinds=frozenset({"k"}),
        memo=Memo(words=["w"]),
        slots=[],
        model=Line,
    )
    basket.slots.append(Slot(id=1, basket=basket))

    class Baskets(UnitOfWork):
        """Baskets in memory."""

        def __init__(self) -> None:
            super().__init__(events=EventBus())
            self.baskets = InMemoryRepository[Basket, int]([basket])

        async def _commit(self) -> None: ...

        async def _rollback(self) -> None: ...

    uow = Baskets()

    async def undone() -> None:
        async with uow.savepoint():
            one.quantity, three.quantity, five.quantity = 10, 30, 50
            basket.lines.append(Line(id=4, quantity=4))
            basket.lines.remove(two)
            del basket.by_name["one"]
            basket.pair = (three, three)
            basket.tags.add("b")
            basket.kinds = frozenset()
            basket.memo.words.append("x")
            raise RuntimeError("undone")

    async with uow:
        with pytest.raises(RuntimeError, match="undone"):
            await undone()

    assert basket.lines == [one, two]
    assert basket.lines[0] is one
    assert basket.by_name == {"one": one, "five": five}
    assert five.quantity == 5
    assert basket.by_name["one"] is one
    assert basket.pair == (one, three)
    assert basket.pair[1] is three
    assert [line.quantity for line in (one, two, three)] == [1, 2, 3]
    assert basket.shelf == Shelf(two, three)
    assert type(basket.shelf) is Shelf
    assert (basket.tags, basket.kinds) == ({"a"}, frozenset({"k"}))
    assert (type(basket.tags), type(basket.kinds)) == (set, frozenset)
    assert basket.memo.words == ["w"]
    assert basket.slots[0].basket is basket
    assert basket.model is Line


async def test_a_savepoint_begins_over_mapped_child_entities() -> None:
    """A gadget with a part and a maker can be snapshot, and comes back."""
    gadget = make_gadget("a", parts=[make_part("P", weight=1)], maker=make_maker("A"))
    uow = FakeGadgetsUnitOfWork(InMemoryGadgets([gadget]))

    async def undone() -> None:
        async with uow.savepoint():
            gadget.parts[0].weight = 9
            raise RuntimeError("undone")

    async with uow:
        with pytest.raises(RuntimeError, match="undone"):
            await undone()
    assert [part.weight for part in gadget.parts] == [1]


async def test_an_added_aggregate_is_found_as_it_was_added() -> None:
    """Adding stores the aggregate itself, under its id."""
    repository = InMemoryGadgets()
    gadget = make_gadget()

    await repository.add(gadget)
    assert repository.items == {gadget.id: gadget}
    assert await repository.get(gadget.id) is gadget


async def test_removing_an_aggregate_twice_is_harmless() -> None:
    """A second hard remove does nothing, as in an SQL unit of work."""
    repository = stocked("a")
    (gadget,) = repository.items.values()

    await repository.remove(gadget, hard=True)
    await repository.remove(gadget, hard=True)
    assert repository.items == {}


async def test_repositories_that_cannot_query_say_so() -> None:
    """Only _add, _get, and _remove are required; queries then name the class."""

    class Minimal(Repository[Note, int]):
        """Stores nothing, and answers no queries."""

        async def _add(self, aggregate: Note) -> None: ...

        async def _get(self, id_: int) -> Note | None:
            return None

        async def _remove(self, aggregate: Note) -> None: ...

    repository = Minimal()
    with pytest.raises(NotImplementedError, match=r"^Minimal cannot filter\.$"):
        await repository.filter()
    with pytest.raises(NotImplementedError, match=r"^Minimal cannot count\.$"):
        await repository.count()
    with pytest.raises(NotImplementedError, match=r"^Minimal cannot count\.$"):
        await repository.exists()


class Tagged[T]:
    """A generic mixin whose type argument is not the stored aggregate."""


async def test_only_repository_type_arguments_name_the_aggregate() -> None:
    """Another generic base naming an aggregate first is passed by."""

    class TaggedGadgets(Tagged[Note], InMemoryRepository[Gadget, GadgetId]):
        """Gadgets, with a mixin that names notes."""

    assert TaggedGadgets().aggregate_type is Gadget
