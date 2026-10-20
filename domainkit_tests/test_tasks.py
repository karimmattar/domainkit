"""Background tasks: clean contexts, ownership per loop, supervision, shutdown."""

import asyncio
import gc
import inspect
import logging
import weakref
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any

import pytest
from prometheus_client import REGISTRY

from domainkit import clock, tasks
from domainkit import context as request_context
from domainkit.context import RequestContext, bind_request, current_request
from domainkit.contrib.idempotency.operations import (
    Operation,
    run_operation,
    running_operation,
)
from domainkit.i18n.translation import get_language, override
from domainkit.tasks import backoff_delay, owned, shutdown, spawn, supervise
from domainkit.tracing import current_ids, remote_parent

pytestmark = pytest.mark.anyio

REQUEST = RequestContext("req-first", "203.0.113.9", "POST", "/orders", actor="u1")
TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
"""Short enough for the backoff of these tests: restarts come at once."""
QUICK = {"backoff": 0.001, "max_backoff": 0.002}


def sample(name: str, task: str) -> float:
    """Read one per-task metric sample (0 when absent)."""
    return REGISTRY.get_sample_value(name, {"task": task}) or 0.0


def seen() -> dict[str, object]:
    """The context variables a request sets, as this context sees them."""
    return {
        "request": current_request(),
        "operation": running_operation(),
        "trace": current_ids(),
        "language": get_language(),
    }


async def wait_for(task: asyncio.Task[Any]) -> None:
    """Wait (at most 5 seconds) for ``task`` to end, however it ends."""
    await asyncio.wait([task], timeout=5)
    assert task.done()


async def test_spawned_tasks_carry_nothing_of_the_request_that_started_them() -> None:
    """No request, idempotency operation, span, or language reaches the task."""
    with (
        bind_request(REQUEST),
        run_operation(Operation("key-hash", "token")),
        remote_parent({"traceparent": TRACEPARENT}),
        override("ar"),
    ):
        inside = seen()
        task = spawn(asyncio.to_thread(seen), name="test.clean")  # A thread too.
        background = spawn(_seen(), name="test.clean")
        assert inside["request"] is REQUEST  # The request itself kept them.
        assert inside["trace"] is not None
        assert inside["language"] == "ar"
        await wait_for(task)
        await wait_for(background)

    clean = {"request": None, "operation": None, "trace": None, "language": "en"}
    assert background.result() == clean
    assert task.result() == clean


async def _seen() -> dict[str, object]:
    """``seen()`` from a coroutine."""
    return seen()


async def test_spawned_tasks_keep_a_frozen_clock() -> None:
    """A test's frozen clock is not request state: the task reads it too."""

    async def read() -> datetime:
        return clock.now()

    frozen_at = datetime(2026, 1, 1, 9, tzinfo=UTC)
    with clock.freeze_time(frozen_at):
        task = spawn(read(), name="test.clock")
        await wait_for(task)
    assert task.result() == frozen_at

    task = spawn(read(), name="test.clock")  # No clock frozen: real time.
    await wait_for(task)
    assert task.result() > frozen_at


async def test_spawned_tasks_are_kept_while_they_wait() -> None:
    """A task waiting on what only it holds is not collected half-way."""

    async def wait_alone() -> None:
        await asyncio.get_running_loop().create_future()

    task = weakref.ref(spawn(wait_alone(), name="test.kept"))
    gc.collect()
    kept = task()
    assert kept is not None
    assert kept in owned()
    assert not kept.done()
    kept.cancel()
    await wait_for(kept)
    assert kept not in owned()  # Finished tasks are let go.


async def test_an_unexpected_failure_is_logged_once_with_the_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Logged and counted by name; asyncio does not log it a second time."""

    async def fail() -> None:
        raise RuntimeError("boom")

    before = sample("tasks_failed_total", "test.fails")
    with caplog.at_level(logging.DEBUG):
        task = spawn(fail(), name="test.fails")
        await wait_for(task)
        await asyncio.sleep(0)  # Done callbacks run on the next loop pass.
        del task
        gc.collect()  # asyncio would log an unretrieved exception now.

    assert [(r.name, r.levelno, r.getMessage()) for r in caplog.records] == [
        ("domainkit.tasks", logging.ERROR, "Background task 'test.fails' failed")
    ]
    cause = caplog.records[0].exc_info
    assert cause
    assert str(cause[1]) == "boom"
    assert sample("tasks_failed_total", "test.fails") == before + 1


async def test_failures_awaited_elsewhere_and_cancellations_are_not_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``log_errors=False`` leaves errors to whoever awaits the task."""

    async def fail() -> None:
        raise RuntimeError("theirs to handle")

    with caplog.at_level(logging.DEBUG, "domainkit.tasks"):
        quiet = spawn(fail(), name="test.quiet", log_errors=False)
        with pytest.raises(RuntimeError, match="theirs to handle"):
            await quiet
        cancelled = spawn(asyncio.Event().wait(), name="test.cancelled")
        await asyncio.sleep(0)
        cancelled.cancel()
        await wait_for(cancelled)
        await asyncio.sleep(0)
    assert cancelled.cancelled()
    assert caplog.records == []


def test_spawning_without_a_running_loop_closes_the_coroutine() -> None:
    """The coroutine is closed unrun, so nothing warns that it never ran."""
    coro = asyncio.sleep(0)

    with pytest.raises(RuntimeError, match=r"spawn\('test.no_loop'\) needs a running"):
        spawn(coro, name="test.no_loop")
    assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED


async def test_shutdown_cancels_and_waits_for_every_task() -> None:
    """Cleanup runs before shutdown returns; tasks started meanwhile stop too."""
    stopped: list[str] = []
    late: list[asyncio.Task[None]] = []

    async def work(name: str, *, start_another: bool = False) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)  # Cleanup that waits.
            stopped.append(name)
            if start_another:
                late.append(spawn(work("late"), name="test.late"))

    spawn(work("first", start_another=True), name="test.first")
    spawn(work("second"), name="test.second")
    await asyncio.sleep(0)
    loop = asyncio.get_running_loop()

    assert await shutdown(loop.time() + 5) == []

    assert sorted(stopped) == ["first", "late", "second"]
    assert late[0].cancelled()
    assert owned() == frozenset()
    assert await shutdown(loop.time() + 5) == []  # Nothing left: at once.


async def test_shutdown_names_tasks_that_will_not_stop() -> None:
    """A task that ignores cancellation is reported, not waited for forever."""
    release = asyncio.Event()

    async def stubborn() -> None:
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue  # Ignores it.

    task = spawn(stubborn(), name="test.stubborn")
    spawn(asyncio.Event().wait(), name="test.polite")
    await asyncio.sleep(0)
    loop = asyncio.get_running_loop()

    try:
        assert await shutdown(loop.time() + 0.01) == ["test.stubborn"]
        assert await shutdown(loop.time()) == ["test.stubborn"]  # Past: no wait.
    finally:
        release.set()  # Fail fast, not hang, if an assertion fails.
        await wait_for(task)


async def test_shutdown_leaves_the_task_that_calls_it_alone() -> None:
    """A spawned task may shut the others down without cancelling itself."""
    other = spawn(asyncio.Event().wait(), name="test.other")

    async def stop_others() -> list[str]:
        return await shutdown(asyncio.get_running_loop().time() + 5)

    stopper = spawn(stop_others(), name="test.stopper")
    await wait_for(stopper)
    assert stopper.result() == []
    assert other.cancelled()


def test_each_loop_owns_its_own_tasks() -> None:
    """Shutting one loop's tasks down leaves another loop's running."""

    async def in_loop(other: asyncio.Task[Any] | None) -> asyncio.Task[Any]:
        mine = spawn(asyncio.Event().wait(), name="test.loop")
        await asyncio.sleep(0)
        assert owned() == {mine}
        if other is not None:
            assert await shutdown(asyncio.get_running_loop().time() + 5) == []
            assert mine.cancelled()
            assert not other.done()  # Another loop's: untouched.
        return mine

    with asyncio.Runner() as first:
        task = first.run(in_loop(None))
        with asyncio.Runner() as second:
            second.run(in_loop(task))
        assert not task.done()
    assert task.cancelled()  # Runner.close() cancels what is left.


def test_no_closed_loop_is_kept_alive() -> None:
    """After asyncio.run, or a loop closed with tasks pending, the loop goes."""

    async def leave_one_running() -> None:
        spawn(asyncio.Event().wait(), name="test.left")

    loop = asyncio.new_event_loop()
    ran = weakref.ref(loop)
    loop.run_until_complete(leave_one_running())
    loop.close()  # Its task still pending: it will never run again.
    del loop

    async def start_one() -> None:
        await wait_for(spawn(asyncio.sleep(0), name="test.next"))

    previous = asyncio.Runner()
    with previous:
        previous.run(start_one())  # A new loop lets the closed one go.
        used = weakref.ref(previous.get_loop())
    del previous
    gc.collect()
    assert ran() is None
    assert used() is None


async def run_n(
    calls: list[int], behaviours: list[Callable[[], Coroutine[Any, Any, None]]]
) -> None:
    """One supervised run: the next of ``behaviours``, recording its number."""
    calls.append(len(calls))
    await behaviours[len(calls) - 1]()


async def test_a_crashed_run_is_logged_counted_and_restarted_in_a_clean_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Context a run set does not reach the next run."""
    contexts: list[RequestContext | None] = []
    second_ran = asyncio.Event()

    async def crash() -> None:
        contexts.append(current_request())
        request_context._current.set(REQUEST)  # Left set, as a crash might.
        raise RuntimeError("crashed once")

    async def then_wait() -> None:
        contexts.append(current_request())
        second_ran.set()
        await asyncio.Event().wait()

    calls: list[int] = []
    before = sample("tasks_restarted_total", "test.restarts")
    with caplog.at_level(logging.ERROR, "domainkit.tasks"):
        supervisor = supervise(
            lambda: run_n(calls, [crash, then_wait]), name="test.restarts", **QUICK
        )
        async with asyncio.timeout(5):
            await second_ran.wait()

    assert contexts == [None, None]
    assert sample("tasks_restarted_total", "test.restarts") == before + 1
    (record,) = caplog.records
    assert record.getMessage().startswith("Task 'test.restarts' crashed; restarting")
    assert record.getMessage().endswith("(1 in a row)")
    assert record.exc_info
    assert str(record.exc_info[1]) == "crashed once"
    supervisor.cancel()
    await wait_for(supervisor)
    assert calls == [0, 1]


async def test_supervision_ends_when_a_run_returns() -> None:
    """Work that finishes is done: nothing restarts it."""
    calls: list[int] = []

    async def finish() -> None:
        return None

    supervisor = supervise(lambda: run_n(calls, [finish]), name="test.returns")
    await wait_for(supervisor)
    assert supervisor.result() is None
    assert calls == [0]


async def test_cancelling_the_supervisor_stops_the_run_for_good(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Shutdown cancels the supervisor; the run stops and nothing restarts."""
    started = asyncio.Event()
    cleaned_up: list[bool] = []

    async def run() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned_up.append(True)

    calls: list[int] = []
    supervisor = supervise(lambda: run_n(calls, [run]), name="test.stop", **QUICK)
    async with asyncio.timeout(5):
        await started.wait()
    with caplog.at_level(logging.ERROR, "domainkit.tasks"):
        assert await shutdown(asyncio.get_running_loop().time() + 5) == []
    assert supervisor.cancelled()
    assert cleaned_up == [True]
    assert calls == [0]
    assert caplog.records == []


async def test_a_run_that_crashes_while_stopping_is_not_restarted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A run that turns its cancellation into an error still stops."""
    started = asyncio.Event()

    async def run() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise RuntimeError("could not stop cleanly") from None

    calls: list[int] = []
    supervisor = supervise(lambda: run_n(calls, [run]), name="test.messy", **QUICK)
    async with asyncio.timeout(5):
        await started.wait()
    with caplog.at_level(logging.ERROR, "domainkit.tasks"):
        supervisor.cancel()
        await wait_for(supervisor)

    assert calls == [0]
    assert caplog.messages == ["Task 'test.messy' crashed while stopping"]


async def test_a_run_cancelled_by_someone_else_is_restarted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only the supervisor's own cancellation stops the work."""
    runs: list[asyncio.Task[Any] | None] = []
    first_ran, second_ran = asyncio.Event(), asyncio.Event()

    async def wait() -> None:
        runs.append(asyncio.current_task())
        first_ran.set()
        await asyncio.Event().wait()

    async def then_signal() -> None:
        second_ran.set()
        await asyncio.Event().wait()

    calls: list[int] = []
    supervisor = supervise(
        lambda: run_n(calls, [wait, then_signal]), name="test.foreign", **QUICK
    )
    with caplog.at_level(logging.ERROR, "domainkit.tasks"):
        async with asyncio.timeout(5):
            await first_ran.wait()
        assert runs[0] is not None
        runs[0].cancel()
        async with asyncio.timeout(5):
            await second_ran.wait()

    (record,) = caplog.records
    assert record.getMessage().startswith("Task 'test.foreign' was cancelled;")
    assert record.exc_info is None
    supervisor.cancel()
    await wait_for(supervisor)


async def test_crashes_in_a_row_back_off_until_a_run_stays_up(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A run that lasted ``max_backoff`` resets the count of crashes."""
    done = asyncio.Event()

    async def crash() -> None:
        raise RuntimeError("down")

    async def crash_after_a_while() -> None:
        frozen.tick(1)  # Well past max_backoff.
        raise RuntimeError("down again")

    async def stop() -> None:
        done.set()

    calls: list[int] = []
    behaviours = [crash, crash, crash_after_a_while, crash, stop]
    with (
        clock.freeze_time() as frozen,
        caplog.at_level(logging.ERROR, "domainkit.tasks"),
    ):
        supervisor = supervise(
            lambda: run_n(calls, behaviours), name="test.backoff", **QUICK
        )
        await wait_for(supervisor)

    assert done.is_set()
    assert [message[-12:] for message in caplog.messages] == [
        "(1 in a row)",
        "(2 in a row)",
        "(1 in a row)",
        "(2 in a row)",
    ]


@pytest.mark.parametrize(
    ("crashes", "low", "high"),
    [(0, 0.5, 1.0), (1, 0.5, 1.0), (2, 1.0, 2.0), (3, 2.0, 4.0), (5, 5.0, 10.0)],
)
def test_backoff_doubles_up_to_its_bound_with_jitter(
    crashes: int, low: float, high: float
) -> None:
    """Between half and all of ``backoff`` doubled per crash, at most the bound."""
    delays = {backoff_delay(crashes, 1.0, 10.0) for _ in range(200)}
    assert all(low <= delay <= high for delay in delays)
    assert len(delays) > 1  # Jittered.


def test_backoff_never_overflows() -> None:
    """However many crashes, the wait stays within ``max_backoff``."""
    assert 30.0 <= backoff_delay(10**6, 1.0, 60.0) <= 60.0


class Crashes(logging.Handler):
    """Collect the supervisor's crash messages; signal each as it comes."""

    def __init__(self) -> None:
        """Start with none."""
        super().__init__(logging.ERROR)
        self.messages: list[str] = []
        self.logged = asyncio.Event()

    def emit(self, record: logging.LogRecord) -> None:
        """Keep the message and wake whoever waits for it."""
        self.messages.append(record.getMessage())
        self.logged.set()

    async def next(self) -> str:
        """Wait for the next crash message (at most 5 seconds)."""
        async with asyncio.timeout(5):
            await self.logged.wait()
        self.logged.clear()
        return self.messages[-1]


async def test_supervision_backs_off_from_a_second_to_a_minute_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Half a second at the least jitter; a run of a minute counts as healthy."""
    monkeypatch.setattr(tasks._random, "uniform", lambda low, high: low)
    crashes = Crashes()
    logging.getLogger("domainkit.tasks").addHandler(crashes)

    async def crash() -> None:
        raise RuntimeError("down")

    async def crash_after_a_minute() -> None:
        frozen.tick(60)
        raise RuntimeError("down after a minute")

    async def wait() -> None:
        await asyncio.Event().wait()

    calls: list[int] = []
    try:
        with clock.freeze_time() as frozen:
            supervisor = supervise(
                lambda: run_n(calls, [crash, crash_after_a_minute, wait]),
                name="test.defaults",
            )
            first = await crashes.next()
            second = await crashes.next()  # After the first's 0.5 s backoff.
    finally:
        logging.getLogger("domainkit.tasks").removeHandler(crashes)
    supervisor.cancel()
    await wait_for(supervisor)

    assert first.endswith("restarting it in 0.5s (1 in a row)")
    assert second.endswith("restarting it in 0.5s (1 in a row)")


async def test_supervisor_and_runs_carry_the_name() -> None:
    """Both show up under the name given, in tasks and logs; one bound is fine."""
    names: list[str] = []

    async def run() -> None:
        current = asyncio.current_task()
        assert current is not None
        names.append(current.get_name())

    supervisor = supervise(run, name="test.named", backoff=1.0, max_backoff=1.0)
    assert supervisor.get_name() == "test.named"
    await wait_for(supervisor)
    assert names == ["test.named"]


async def test_a_run_that_lasted_exactly_max_backoff_was_healthy(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The count of crashes starts again from a run of ``max_backoff`` or more."""
    bound = 2.0**-7  # Exact in binary: the frozen clock adds it exactly.

    async def crash() -> None:
        raise RuntimeError("down")

    async def crash_on_the_bound() -> None:
        frozen.tick(bound)
        raise RuntimeError("down again")

    async def stop() -> None:
        return None

    calls: list[int] = []
    behaviours = [crash, crash_on_the_bound, stop]
    with (
        clock.freeze_time() as frozen,
        caplog.at_level(logging.ERROR, "domainkit.tasks"),
    ):
        supervisor = supervise(
            lambda: run_n(calls, behaviours),
            name="test.bound",
            backoff=bound / 2,
            max_backoff=bound,
        )
        await wait_for(supervisor)

    assert [message[-12:] for message in caplog.messages] == [
        "(1 in a row)",
        "(1 in a row)",
    ]


@pytest.mark.parametrize(("backoff", "max_backoff"), [(0.0, 1.0), (2.0, 1.0)])
async def test_supervise_refuses_a_backoff_out_of_bounds(
    backoff: float, max_backoff: float
) -> None:
    """A backoff of zero would restart a crashing run in a busy loop."""

    async def run() -> None:
        return None

    with pytest.raises(ValueError, match="0 < backoff <= max_backoff"):
        supervise(run, name="test.bad", backoff=backoff, max_backoff=max_backoff)
    assert owned() == frozenset()
