"""Background tasks the framework starts: clean, kept, owned, and stopped.

``spawn(coro, name=...)`` starts a task the way every framework task starts:

- in a fresh context, so it carries none of the starting code's context
  variables: no request id or actor, no span, no language, no idempotency
  operation. Only a frozen clock (``domainkit.clock.freeze_time``) is
  carried, since it is a test's, not a request's;
- with a strong reference, so a task waiting on something only it holds is
  never collected half-way;
- logging an unexpected exception once, at ERROR, with the task's name;
- owned by its event loop: ``domainkit.lifecycle.close_resources`` cancels
  it and waits for it within the shutdown budget (``SHUTDOWN_TIMEOUT``).

``supervise(factory, name=...)`` keeps work running: each run is a new task
from ``factory``; when one crashes, the crash is logged and counted, and a
new run starts after a backoff that doubles from ``backoff`` up to
``max_backoff``, jittered. Cancelling the supervisor (shutdown does) stops
the run and the restarts::

    publisher = supervise(lambda: relay_forever(outbox), name="outbox.relay")

Tasks belong to the event loop that runs them. Tests and management commands
run several loops in one process; each loop's tasks are kept apart, and a
closed loop's tasks are let go, so nothing here keeps a closed loop alive.

Names label log lines and the ``tasks_failed_total`` and
``tasks_restarted_total`` metrics: use fixed names, never request data.
"""

import asyncio
import logging
import secrets
from collections.abc import Callable, Coroutine
from contextvars import Context
from typing import Any, Final, cast
from weakref import WeakKeyDictionary

from domainkit import clock

logger = logging.getLogger(__name__)

try:
    from prometheus_client import Counter
except ImportError:  # pragma: no cover - only without the [metrics] extra.

    def _count_failure(task: str) -> None:
        """Count nothing: ``prometheus_client`` is not installed."""

    def _count_restart(task: str) -> None:
        """Count nothing: ``prometheus_client`` is not installed."""

else:
    FAILURES: Final = Counter(
        "tasks_failed_total",
        "Background tasks that ended with an unexpected error.",
        ["task"],
    )
    RESTARTS: Final = Counter(
        "tasks_restarted_total",
        "Supervised background tasks restarted after a crash.",
        ["task"],
    )

    def _count_failure(task: str) -> None:
        """Count one background task that failed."""
        FAILURES.labels(task).inc()

    def _count_restart(task: str) -> None:
        """Count one restart of a supervised task."""
        RESTARTS.labels(task).inc()


_MAX_DOUBLINGS: Final = 64
"""Backoff stops doubling here, long past any ``max_backoff``, so it never overflows."""

_random = secrets.SystemRandom()

type Factory = Callable[[], Coroutine[Any, Any, object]]
"""Makes one run of a supervised task (an ``async def`` called with no arguments)."""

_owned: WeakKeyDictionary[asyncio.AbstractEventLoop, set[asyncio.Task[Any]]] = (
    WeakKeyDictionary()
)
"""Each loop's tasks still running; a set holds no reference to its loop."""


def clean_context() -> Context:
    """Return a new, empty context, holding only the frozen clock if one is in use.

    For code that must create a task itself; ``spawn`` uses it.
    """
    context = Context()
    frozen = clock.frozen()
    if frozen is not None:
        context.run(clock._frozen.set, frozen)
    return context


def spawn[T](
    coro: Coroutine[Any, Any, T], *, name: str, log_errors: bool = True
) -> asyncio.Task[T]:
    """Run ``coro`` as a task of the running loop, in a clean context, owned.

    Args:
        coro: The coroutine to run.
        name: A fixed name for logs, metrics, and ``Task.get_name()``.
        log_errors: Log an unexpected exception (and count it). Pass False
            when callers await the task and handle its errors themselves.

    Raises:
        RuntimeError: No event loop is running; ``coro`` is closed unrun.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        raise RuntimeError(f"spawn({name!r}) needs a running event loop.") from None
    task = loop.create_task(coro, name=name, context=clean_context())
    _own(loop, task)
    if log_errors:
        task.add_done_callback(_log_failure)
    return task


def supervise(
    factory: Factory,
    *,
    name: str,
    backoff: float = 1.0,
    max_backoff: float = 60.0,
) -> asyncio.Task[None]:
    """Keep ``factory``'s work running: restart a run that crashes, until cancelled.

    A run that returns ends the supervision. A run that raises (or is
    cancelled by anything but its supervisor) is logged, counted, and
    restarted after ``backoff_delay``; a run that lasted ``max_backoff`` or
    longer counts as healthy, so the next crash waits ``backoff`` again.
    The supervisor is ``spawn``-ed: owned, clean, and cancelled at shutdown,
    which also cancels the run.

    Raises:
        ValueError: ``backoff`` is not positive, or above ``max_backoff``.
    """
    if not 0 < backoff <= max_backoff:
        raise ValueError(
            f"supervise({name!r}) needs 0 < backoff <= max_backoff, "
            f"not {backoff!r} and {max_backoff!r}."
        )
    return spawn(_supervised(factory, name, backoff, max_backoff), name=name)


def backoff_delay(crashes: int, backoff: float, max_backoff: float) -> float:
    """Seconds to wait after ``crashes`` crashes in a row.

    ``backoff`` doubled for each crash after the first, at most
    ``max_backoff``, then jittered to between half and all of it, so tasks
    that crashed together do not restart together.
    """
    doublings = min(max(crashes - 1, 0), _MAX_DOUBLINGS)
    ceiling = min(max_backoff, backoff * 2.0**doublings)
    return ceiling * _random.uniform(0.5, 1.0)


def owned() -> frozenset[asyncio.Task[Any]]:
    """The running loop's ``spawn``-ed tasks that have not finished."""
    return frozenset(_owned.get(asyncio.get_running_loop(), ()))


async def shutdown(deadline: float) -> list[str]:
    """Cancel the running loop's owned tasks; wait for them until ``deadline``.

    ``deadline`` is in the loop's time (``loop.time()``). Tasks started
    while waiting are cancelled too. The calling task is left alone.

    Returns:
        The names of the tasks still running at the deadline, sorted: they
        did not stop when cancelled.
    """
    loop = asyncio.get_running_loop()
    current = asyncio.current_task()
    while True:
        pending = {task for task in _owned.get(loop, ()) if task is not current}
        if not pending:
            return []
        for task in pending:
            task.cancel()
        timeout = deadline - loop.time()
        if timeout <= 0:
            return sorted(task.get_name() for task in pending)
        _, still = await asyncio.wait(pending, timeout=timeout)
        if still:
            return sorted(task.get_name() for task in still)


def _own(loop: asyncio.AbstractEventLoop, task: asyncio.Task[Any]) -> None:
    """Keep ``task`` until it finishes; let go of the tasks of closed loops."""
    tasks = _owned.get(loop)
    if tasks is None:
        # A new loop: those that closed with tasks pending will never run
        # them, and the tasks would keep those loops alive.
        for closed in [other for other in _owned if other.is_closed()]:
            del _owned[closed]
        tasks = _owned[loop] = set()
    tasks.add(task)
    task.add_done_callback(tasks.discard)


def _log_failure(task: asyncio.Task[Any]) -> None:
    """Log (and count) the exception a task ended with, if any."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _count_failure(task.get_name())
        logger.error("Background task %r failed", task.get_name(), exc_info=exc)


async def _supervised(
    factory: Factory, name: str, backoff: float, max_backoff: float
) -> None:
    """Run ``factory`` again after each crash, until cancelled or a run returns."""
    loop = asyncio.get_running_loop()
    supervisor = cast("asyncio.Task[None]", asyncio.current_task())
    crashes = 0
    while True:
        started = clock.monotonic()
        # A copy of the supervisor's clean context: a run's changes stay its own.
        run = loop.create_task(factory(), name=name)
        try:
            await run
        except asyncio.CancelledError:
            if supervisor.cancelling():
                raise
            ended = "was cancelled"
        except Exception:
            if supervisor.cancelling():  # Cancelled, and it crashed stopping.
                logger.exception("Task %r crashed while stopping", name)
                return
            ended = "crashed"
        else:
            return
        healthy = clock.monotonic() - started >= max_backoff
        crashes = 1 if healthy else crashes + 1
        delay = backoff_delay(crashes, backoff, max_backoff)
        _count_restart(name)
        logger.error(
            "Task %r %s; restarting it in %.1fs (%d in a row)",
            name,
            ended,
            delay,
            crashes,
            exc_info=run.exception() if not run.cancelled() else None,
        )
        await asyncio.sleep(delay)
