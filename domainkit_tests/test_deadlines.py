"""Deadlines: the time left, bounded blocks and awaits, and how they nest."""

import asyncio
import gc
import inspect
import math
from collections.abc import Generator

import pytest

from domainkit import deadlines
from domainkit.clock import freeze_time
from domainkit.deadlines import Deadline, DeadlineExceeded, Timeout

pytestmark = pytest.mark.anyio


async def forever() -> None:
    """Wait for an event nobody sets: only a deadline ends it."""
    await asyncio.Event().wait()


async def hang_in(*blocks: Timeout) -> None:
    """Enter ``blocks``, outermost first, and wait inside them until cut off."""
    if not blocks:
        await forever()
        return
    async with blocks[0]:
        await hang_in(*blocks[1:])


def test_without_a_deadline_nothing_is_left_or_bounded() -> None:
    """Outside any deadline, remaining() is None, or the bound it was given."""
    assert deadlines.current_deadline() is None
    assert deadlines.remaining() is None
    assert deadlines.remaining(5.0) == 5.0
    assert repr(Deadline(None)) == "<Deadline none>"
    assert Deadline(None).expired() is False


def test_a_deadline_counts_down_on_the_framework_clock() -> None:
    """remaining() follows a frozen clock, never goes below 0, then expires."""
    with freeze_time("2026-01-01") as clock:
        deadline = Deadline(10.0)
        assert deadline.remaining() == 10.0
        assert repr(deadline) == "<Deadline 10.000s left>"
        clock.tick(4)
        assert deadline.remaining() == 6.0
        assert not deadline.expired()
        clock.tick(6)
        assert deadline.remaining() == 0.0
        assert deadline.expired()
        clock.tick(1)
        assert deadline.remaining() == 0.0


def test_reset_counts_from_the_start_and_clear_removes_it() -> None:
    """A deadline moves relative to when its work started."""
    with freeze_time("2026-01-01") as clock:
        deadline = Deadline(10.0)
        clock.tick(3)
        deadline.reset(5.0)
        assert (deadline.seconds, deadline.remaining()) == (5.0, 2.0)
        deadline.reset(60.0)
        assert deadline.remaining() == 57.0
        deadline.clear()
        assert deadline.at is None
        assert deadline.remaining() is None
        assert deadline.seconds == 60.0  # What the work had, for logs.
        deadline.reset(None)
        assert deadline.remaining() is None
        assert not deadline.seconds


async def test_timeout_sets_the_deadline_inside_its_block() -> None:
    """remaining() inside is the block's bound; outside, as it was."""
    with freeze_time("2026-01-01"):
        async with deadlines.timeout(5.0) as bound:
            assert deadlines.current_deadline() is bound.deadline
            assert deadlines.remaining() == 5.0
            assert deadlines.remaining(2.0) == 2.0
            assert deadlines.remaining(9.0) == 5.0
        assert deadlines.remaining() is None


async def test_nested_blocks_get_the_tighter_bound() -> None:
    """An inner block never outlives the outer deadline."""
    with freeze_time("2026-01-01") as clock:
        async with deadlines.timeout(5.0):
            clock.tick(1)
            async with deadlines.timeout(60.0):
                assert deadlines.remaining() == 4.0
            async with deadlines.timeout(1.0):
                assert deadlines.remaining() == 1.0
            assert deadlines.remaining() == 4.0


async def test_timeout_without_seconds_or_deadline_bounds_nothing() -> None:
    """Nothing to bound by: the block runs, with no deadline inside."""
    async with deadlines.timeout() as bound:
        assert deadlines.remaining() is None
        await asyncio.sleep(0)
    assert not bound.expired()


async def test_the_blocks_own_seconds_raise_timeout_error() -> None:
    """When the block's seconds are the tighter bound, a plain TimeoutError."""
    bound = deadlines.timeout(0.01)
    with pytest.raises(TimeoutError) as raised:
        await hang_in(bound)
    assert type(raised.value) is TimeoutError
    assert isinstance(raised.value.__cause__, asyncio.CancelledError)  # Where it was.
    assert bound.expired()

    outer, inner = deadlines.timeout(60.0), deadlines.timeout(0.01)

    async def nested() -> None:
        async with outer:
            with pytest.raises(TimeoutError) as raised:
                await hang_in(inner)
            assert type(raised.value) is TimeoutError

    await nested()
    assert inner.expired()
    assert not outer.expired()


async def test_the_block_that_set_the_deadline_raises_when_it_passes() -> None:
    """Inner blocks bounded by the deadline leave its timer to its block.

    So the same block raises however the timers would have raced.
    """
    outer = deadlines.timeout(0.01)
    inner = deadlines.timeout(60.0)
    innermost = deadlines.timeout()
    with pytest.raises(TimeoutError) as raised:
        await hang_in(outer, inner, innermost)
    assert type(raised.value) is TimeoutError  # The outer block's own seconds.
    assert outer.expired()
    assert inner._timer is None
    assert innermost._timer is None


async def test_a_task_started_inside_gets_deadline_exceeded() -> None:
    """The block's timer cancels its own task; others bound themselves.

    A task started in the block shares its deadline, so a block in that
    task times itself, and raises DeadlineExceeded when the deadline passes,
    even after the block that set it ended.
    """
    shared: list[bool] = []

    async def child() -> None:
        deadline = deadlines.current_deadline()
        shared.append(deadline is not None and deadline._enforced())
        await deadlines.within(forever(), 60.0)

    with freeze_time("2026-01-01") as clock:
        async with deadlines.timeout(60.0):
            clock.tick(59.95)  # 0.05s left, on the clock deadlines read.
            task = asyncio.create_task(child())
            with pytest.raises(
                DeadlineExceeded, match=r"^The deadline passed\.$"
            ) as raised:
                await task
    assert isinstance(raised.value, TimeoutError)
    assert isinstance(raised.value.__cause__, TimeoutError)
    assert isinstance(raised.value.__cause__.__cause__, asyncio.CancelledError)
    assert shared == [False]


async def test_a_task_started_in_an_inner_block_times_itself_too() -> None:
    """The inner block leaves the deadline to the outer timer, of another task."""

    async def child() -> None:
        await deadlines.within(forever())

    with freeze_time("2026-01-01") as clock:
        async with deadlines.timeout(1.0), deadlines.timeout():
            clock.tick(0.95)  # 0.05s left, on the clock deadlines read.
            task = asyncio.create_task(child())
            with pytest.raises(DeadlineExceeded):
                await task


async def test_a_deadline_given_later_is_kept_by_the_blocks_inside() -> None:
    """A block without bounds whose deadline is set later: inner blocks time it."""
    async with deadlines.timeout() as unbounded:
        assert unbounded.deadline is not None
        unbounded.deadline.reset(0.01)  # No timer of its own to move.
        with pytest.raises(DeadlineExceeded):
            await hang_in(deadlines.timeout())


async def test_an_inner_block_cannot_lift_the_outer_deadline() -> None:
    """Clearing its own deadline leaves the outer one, and its timer, in force."""
    outer = deadlines.timeout(0.01)

    async def clear_inside() -> None:
        async with outer, deadlines.timeout() as inner:
            assert inner.deadline is not None
            inner.deadline.clear()
            assert deadlines.remaining() is None
            await forever()

    with pytest.raises(TimeoutError):
        await clear_inside()
    assert outer.expired()


async def test_no_time_left_refuses_the_block_on_entry() -> None:
    """A passed deadline, or no seconds at all, never runs the block."""
    refused = deadlines.timeout(5.0)
    with freeze_time("2026-01-01") as clock:
        async with deadlines.timeout(60.0):
            clock.tick(60)
            with pytest.raises(DeadlineExceeded, match=r"^The deadline has passed\.$"):
                await refused.__aenter__()
            with pytest.raises(DeadlineExceeded):  # Both ran out: the deadline did.
                await deadlines.timeout(0).__aenter__()
    assert refused.deadline is None
    assert not refused.expired()
    with pytest.raises(TimeoutError, match=r"^No time was given\.$") as zero:
        await deadlines.timeout(0).__aenter__()
    assert type(zero.value) is TimeoutError
    await refused.__aexit__(None, None, None)  # Never entered: nothing to undo.


async def test_other_errors_and_cancellation_pass_through() -> None:
    """Only the block's own expiry becomes a timeout error."""

    async def boom() -> None:
        async with deadlines.timeout(60.0):
            raise ValueError("boom")

    async def someone_elses() -> None:
        async with deadlines.timeout(60.0), asyncio.timeout(0.01):
            await forever()

    with pytest.raises(ValueError, match="boom"):
        await boom()
    with pytest.raises(TimeoutError):
        await someone_elses()

    entered = asyncio.Event()

    async def bounded() -> None:
        async with deadlines.timeout(60.0):
            entered.set()
            await forever()

    task = asyncio.create_task(bounded())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_reset_and_clear_move_the_timer_of_the_block() -> None:
    """The timer that cancels the block follows its deadline."""
    loop = asyncio.get_running_loop()
    async with deadlines.timeout(1.0) as bound:
        assert bound.deadline is not None
        assert bound._timer is not None
        bound.deadline.reset(60.0)
        when = bound._timer.when()
        assert when is not None
        assert loop.time() + 50 < when <= loop.time() + 60
        bound.deadline.clear()
        assert bound._timer.when() is None
        assert deadlines.remaining() is None
    assert not bound.expired()

    shortened = deadlines.timeout(60.0)

    async def shorten() -> None:
        async with shortened:
            assert shortened.deadline is not None
            shortened.deadline.reset(0.01)
            await forever()

    with pytest.raises(TimeoutError):
        await shorten()
    assert shortened.expired()


async def test_a_fired_or_finished_timer_is_left_alone() -> None:
    """Moving a deadline after its timer fired, or after the block, is harmless."""
    bound = deadlines.timeout(0.01)

    async def move_late() -> None:
        async with bound:
            try:
                await forever()
            finally:
                assert bound.deadline is not None
                bound.deadline.reset(60.0)  # The timer fired: nothing to move.

    with pytest.raises(TimeoutError):
        await move_late()
    async with deadlines.timeout(1.0) as done:
        pass
    assert done.deadline is not None
    assert done.deadline.remaining() is None  # Cleared when the block ended.
    done.deadline.reset(0.0)  # No timer to move any more.
    assert done.deadline.expired()


async def test_within_bounds_one_await() -> None:
    """within() returns the result, or raises as timeout() does."""

    async def answer() -> int:
        await asyncio.sleep(0)
        return 42

    async def under_deadline() -> None:
        async with deadlines.timeout(0.01):
            await deadlines.within(forever(), 60.0)

    assert await deadlines.within(answer()) == 42
    assert await deadlines.within(answer(), 5.0) == 42
    with pytest.raises(TimeoutError) as own:
        await deadlines.within(forever(), 0.01)
    assert type(own.value) is TimeoutError
    with pytest.raises(TimeoutError) as outer:
        await under_deadline()
    assert type(outer.value) is TimeoutError  # Raised by the block, not within().


async def test_within_discards_what_it_never_awaits() -> None:
    """A refused coroutine is closed and a future cancelled: nothing leaks."""

    class Waits:
        def __await__(self) -> Generator[None]:
            yield

    with freeze_time("2026-01-01") as clock:
        async with deadlines.timeout(1.0):
            clock.tick(1)
            coroutine = forever()
            with pytest.raises(DeadlineExceeded):
                await deadlines.within(coroutine)
            assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED
            future: asyncio.Future[int] = asyncio.get_running_loop().create_future()
            with pytest.raises(DeadlineExceeded):
                await deadlines.within(future)
            assert future.cancelled()
            with pytest.raises(DeadlineExceeded):
                await deadlines.within(Waits())
    gc.collect()  # A coroutine left unclosed would warn, and fail the test.


@pytest.mark.parametrize("seconds", [math.nan, math.inf, -math.inf])
async def test_seconds_that_are_not_a_finite_number_are_refused(
    seconds: float,
) -> None:
    """NaN bounds nothing and infinity overflows: None asks for no bound.

    A deadline, a block, and one await all refuse them, and within() closes
    the coroutine it was given; 0 and less stay a block with no time.
    """
    message = r"^A deadline is a finite number of seconds, or None; not -?(nan|inf)\.$"
    with pytest.raises(ValueError, match=message):
        Deadline(seconds)
    with pytest.raises(ValueError, match=message):
        Deadline(10.0).reset(seconds)
    with pytest.raises(ValueError, match=message):
        deadlines.timeout(seconds)
    coroutine = forever()
    with pytest.raises(ValueError, match=message):
        await deadlines.within(coroutine, seconds)
    assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED
    gc.collect()  # A coroutine left unclosed would warn, and fail the test.
    with pytest.raises(TimeoutError, match=r"^No time was given\.$"):
        await deadlines.timeout(-1.0).__aenter__()
    assert Deadline(1e300).remaining() is not None


async def test_the_deadline_is_seen_by_tasks_started_inside() -> None:
    """Context variables carry the deadline into tasks the work starts."""

    async def left() -> float | None:
        return deadlines.remaining()

    with freeze_time("2026-01-01"):
        async with deadlines.timeout(5.0) as bound:
            assert await asyncio.create_task(left()) == 5.0
            assert bound.deadline is not None
            bound.deadline.clear()  # The task's context holds the same deadline.
            assert await asyncio.create_task(left()) is None


async def test_enforced_says_whether_a_timer_cuts_this_task_off() -> None:
    """The task that entered a block, and its inner blocks; not a task sharing it."""

    async def in_a_task() -> bool:
        return deadlines.enforced()

    assert deadlines.enforced() is False
    async with deadlines.timeout():  # Neither seconds nor a deadline.
        assert deadlines.enforced() is False
    async with deadlines.timeout(30.0):
        assert deadlines.enforced() is True
        async with deadlines.timeout():  # Bounded by the outer deadline.
            assert deadlines.enforced() is True
        assert await asyncio.create_task(in_a_task()) is False
