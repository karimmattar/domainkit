"""Registered tasks and durable handlers, and their (de)serialization.

The worker only runs code registered here: it never imports a name read
from the database. Events and task arguments travel as JSON, validated
against the event class or the task signature on the way back.
"""

import math
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from functools import cache
from typing import Any, Final, overload

from pydantic import TypeAdapter, validate_call

from domainkit.apps import registry
from domainkit.conf.global_settings import NAME_PATTERN
from domainkit.ddd import DomainEvent, EventBus
from domainkit.exceptions import ImproperlyConfigured
from domainkit.utils.decimals import jsonable, with_decimal_text
from domainkit.utils.rates import parse_rate

from .cron import Cron, CronError
from .retry import Backoff, Errors, exception_types
from .tables import DEFAULT_QUEUE

type Task = Callable[..., Awaitable[Any]]
"""A task: an async function; its result matters only in a chain."""

PRIORITY_RANGE: Final = range(-100, 101)
"""Task priorities, as in Django's tasks: higher runs first, 0 by default."""


@dataclass(frozen=True, slots=True)
class TaskSpec:
    """A registered task."""

    key: str
    function: Task
    max_attempts: int | None = None
    every: timedelta | None = None
    """Set for periodic tasks that run once per interval."""
    queue: str = DEFAULT_QUEUE
    """The queue whose workers run it (``runworker --queue``)."""
    cron: Cron | None = None
    """Set for periodic tasks that run on a cron schedule."""
    timeout: float | None = None
    """Seconds a run may take; None: ``MESSAGING.TASK_TIMEOUT``."""
    backoff: Backoff | None = None
    """Delays between retries; None: ``MESSAGING``'s."""
    retry_on: Errors | None = None
    """Only these errors are retried; None: any."""
    dont_retry_on: Errors = ()
    """These errors dead-letter at once."""
    priority: int = 0
    """Higher runs first, among the due messages of a queue."""
    rate: tuple[int, int] | None = None
    """At most ``(count, seconds)`` runs, enforced by the workers."""
    sensitive: bool = False
    """Its arguments hold secrets or personal data (a link, a code, an
    address): the worker blanks them once it dies, as it does for every
    message once it is done or cancelled, so it cannot be retried."""


_tasks: dict[str, TaskSpec] = {}


def qualified_name(obj: object) -> str:
    """Return ``module.QualName``, the stable key of a function or class."""
    return f"{getattr(obj, '__module__', '?')}.{getattr(obj, '__qualname__', obj)}"


def checked_queue(queue: str | None) -> str:
    """Return ``queue`` (default: ``default``) if it is a valid queue name."""
    if queue is None:
        return DEFAULT_QUEUE
    if not re.fullmatch(NAME_PATTERN, queue):
        raise ImproperlyConfigured(
            f"Queue names have 1 to 64 letters, digits, '_', '.', or '-': {queue!r}"
        )
    return queue


def _register(spec: TaskSpec) -> None:
    """Add ``spec``, refusing two different functions under one key."""
    existing = _tasks.get(spec.key)
    if existing is not None and existing.function is not spec.function:
        raise ImproperlyConfigured(f"Two tasks are registered as {spec.key!r}.")
    _tasks[spec.key] = spec


def _options(
    *,
    max_attempts: int | None,
    queue: str | None,
    timeout: float | None,
    backoff: Backoff | None,
    retry_on: Iterable[type[Exception]] | None,
    dont_retry_on: Iterable[type[Exception]],
    priority: int,
    rate: str | None,
) -> dict[str, Any]:
    """Check the options ``task`` and ``periodic`` share; return TaskSpec fields."""
    if max_attempts is not None and max_attempts < 1:
        raise ImproperlyConfigured("max_attempts must be 1 or more.")
    if timeout is not None and not (math.isfinite(timeout) and timeout > 0):
        raise ImproperlyConfigured(
            f"timeout must be a positive number, not {timeout!r}."
        )
    if priority not in PRIORITY_RANGE:
        raise ImproperlyConfigured(
            f"priority must be from -100 to 100, not {priority!r}."
        )
    try:
        parsed_rate = None if rate is None else parse_rate(rate)
    except ValueError as exc:
        raise ImproperlyConfigured(str(exc)) from None
    return {
        "max_attempts": max_attempts,
        "queue": checked_queue(queue),
        "timeout": timeout,
        "backoff": backoff,
        "retry_on": None if retry_on is None else exception_types(retry_on, "retry_on"),
        "dont_retry_on": exception_types(dont_retry_on, "dont_retry_on"),
        "priority": priority,
        "rate": parsed_rate,
    }


@overload
def task(function: Task, /) -> Task: ...


@overload
def task(
    *,
    name: str | None = None,
    max_attempts: int | None = None,
    queue: str | None = None,
    timeout: float | None = None,
    backoff: Backoff | None = None,
    retry_on: Iterable[type[Exception]] | None = None,
    dont_retry_on: Iterable[type[Exception]] = (),
    priority: int = 0,
    rate: str | None = None,
    sensitive: bool = False,
) -> Callable[[Task], Task]: ...


def task(
    function: Task | None = None,
    /,
    *,
    name: str | None = None,
    max_attempts: int | None = None,
    queue: str | None = None,
    timeout: float | None = None,
    backoff: Backoff | None = None,
    retry_on: Iterable[type[Exception]] | None = None,
    dont_retry_on: Iterable[type[Exception]] = (),
    priority: int = 0,
    rate: str | None = None,
    sensitive: bool = False,
) -> Task | Callable[[Task], Task]:
    """Register an async function as a background task (``uow.defer(task)``).

    Arguments must be JSON-friendly or Pydantic-validatable (UUIDs, dates,
    models); they are checked against the signature when the task runs.
    ``queue`` (default: ``default``) picks the workers that run it, and
    ``priority`` (-100 to 100, default 0) the order among its due messages:
    higher first. The other options override ``MESSAGING`` for this task:
    ``max_attempts``, ``timeout`` (seconds per run), ``backoff`` (the delays
    between retries), ``retry_on`` (retry only these errors),
    ``dont_retry_on`` (dead-letter these at once), and ``rate``
    (``"10/minute"``: at most that many runs, across the workers that share
    ``RATE_LIMIT_BACKEND``).

    Every message's arguments are blanked in the outbox once it is done or
    cancelled. ``sensitive`` blanks them once it dies too, for arguments
    that hold secrets or personal data; such a dead message cannot be
    retried.
    """
    options = _options(
        max_attempts=max_attempts,
        queue=queue,
        timeout=timeout,
        backoff=backoff,
        retry_on=retry_on,
        dont_retry_on=dont_retry_on,
        priority=priority,
        rate=rate,
    )

    def register(func: Task) -> Task:
        key = name or qualified_name(func)
        _register(TaskSpec(key, func, sensitive=sensitive, **options))
        return func

    return register(function) if function is not None else register


def periodic(
    every: timedelta | None = None,
    *,
    cron: str | None = None,
    tz: str | None = None,
    name: str | None = None,
    max_attempts: int | None = None,
    queue: str | None = None,
    timeout: float | None = None,
    backoff: Backoff | None = None,
    retry_on: Iterable[type[Exception]] | None = None,
    dont_retry_on: Iterable[type[Exception]] = (),
    priority: int = 0,
    rate: str | None = None,
) -> Callable[[Task], Task]:
    """Register an argument-free task that runs on a schedule, cluster-wide.

    Give ``every`` (once per interval) or ``cron`` (a five-field expression,
    in time zone ``tz``, UTC by default; see ``domainkit.contrib.messaging.
    cron`` for the syntax and the daylight saving rule). The other options
    are those of ``task``.
    """
    if (every is None) == (cron is None):
        raise ImproperlyConfigured("Periodic tasks need either every= or cron=.")
    if every is not None and every <= timedelta(0):
        raise ImproperlyConfigured("Periodic tasks need a positive interval.")
    if tz is not None and cron is None:
        raise ImproperlyConfigured("tz= goes with cron=.")
    try:
        schedule = None if cron is None else Cron.parse(cron, tz or "UTC")
    except CronError as exc:
        raise ImproperlyConfigured(f"Invalid cron schedule: {exc}") from None
    options = _options(
        max_attempts=max_attempts,
        queue=queue,
        timeout=timeout,
        backoff=backoff,
        retry_on=retry_on,
        dont_retry_on=dont_retry_on,
        priority=priority,
        rate=rate,
    )

    def register(func: Task) -> Task:
        key = name or qualified_name(func)
        _register(TaskSpec(key, func, every=every, cron=schedule, **options))
        return func

    return register


def queues() -> frozenset[str]:
    """Every queue this process knows: ``default`` and the tasks' queues."""
    return frozenset({DEFAULT_QUEUE, *(spec.queue for spec in _tasks.values())})


def get_task(key: str) -> TaskSpec | None:
    """Return the task registered as ``key``."""
    return _tasks.get(key)


def spec_for(function: Task) -> TaskSpec:
    """Return the registration of ``function``; unregistered tasks are refused."""
    for spec in _tasks.values():
        if spec.function is function:
            return spec
    raise ImproperlyConfigured(
        f"{qualified_name(function)} is not a task; decorate it with @task."
    )


def periodic_tasks() -> list[tuple[TaskSpec, timedelta]]:
    """Return the tasks to run once per interval, with their intervals.

    Tasks shipped by a ``domainkit.contrib`` app run only while that app is
    installed; project tasks are imported only by installed domains.
    """
    return [
        (spec, spec.every)
        for spec in _tasks.values()
        if spec.every is not None and _owner_installed(spec.function)
    ]


def cron_tasks() -> list[tuple[TaskSpec, Cron]]:
    """Return the tasks to run on a cron schedule, with their schedules."""
    return [
        (spec, spec.cron)
        for spec in _tasks.values()
        if spec.cron is not None and _owner_installed(spec.function)
    ]


def all_tasks() -> list[TaskSpec]:
    """Every registered task."""
    return list(_tasks.values())


def _owner_installed(function: Task) -> bool:
    """Report whether the framework app that defines ``function`` is installed."""
    parts = getattr(function, "__module__", "").split(".")
    if parts[:2] != ["domainkit", "contrib"] or len(parts) < 3:
        return True
    return registry.is_installed(".".join(parts[:3]))


def durable_handlers(
    bus: EventBus,
) -> dict[str, tuple[tuple[type[DomainEvent], ...], Callable[[Any], Awaitable[None]]]]:
    """Map each durable handler key to the event types it handles and its function.

    One function may be subscribed to several types (or to a base class).
    """
    handlers: dict[
        str, tuple[tuple[type[DomainEvent], ...], Callable[[Any], Awaitable[None]]]
    ] = {}
    for event_type, handler in bus.durable_subscriptions():
        handled, _ = handlers.get(qualified_name(handler), ((), handler))
        handlers[qualified_name(handler)] = ((*handled, event_type), handler)
    return handlers


@cache
def _adapter(event_type: type[DomainEvent]) -> TypeAdapter[Any]:
    """Build (once) the validator for ``event_type``."""
    return TypeAdapter(event_type)


def dump_event(event: DomainEvent) -> dict[str, Any]:
    """Serialize ``event`` to JSON-compatible values, amounts in fixed point."""
    payload: dict[str, Any] = with_decimal_text(
        event, _adapter(type(event)).dump_python(event, mode="json")
    )
    return payload


def load_event[E: DomainEvent](event_type: type[E], payload: Mapping[str, Any]) -> E:
    """Rebuild an event of ``event_type`` from its JSON payload."""
    event: E = _adapter(event_type).validate_python(payload)
    return event


def dump_arguments(arguments: Mapping[str, object]) -> dict[str, Any]:
    """Serialize task arguments to JSON-compatible values, amounts in fixed point."""
    payload: dict[str, Any] = jsonable(dict(arguments))
    return payload


@cache
def _validated(function: Task) -> Task:
    """Wrap ``function`` so JSON arguments are converted to its parameter types."""
    validated: Task = validate_call(function)
    return validated


async def call_task(function: Task, payload: Mapping[str, Any]) -> object:
    """Run ``function`` with arguments converted back from JSON; return its result."""
    return await _validated(function)(**payload)


CHAIN: Final = "chain"
"""The header listing the tasks that run after a message's own, in order."""


@dataclass(frozen=True, slots=True)
class Chain:
    """Tasks that run one after another, each once the one before succeeds.

    Defer it as a task (``uow.defer(chain(a, b), **a_kwargs)``): the first
    task gets the keyword arguments. Each next task is called with the
    result of the one before: a mapping gives its keyword arguments, ``None``
    passes the previous task's arguments on, and any other value is passed
    as ``result=``. Results must be JSON-friendly. Only the next step is in
    the outbox at any time: it is written in the transaction that records
    the previous one as done, so a crash never loses or repeats a step's
    successor.
    """

    steps: tuple[Task, ...]

    @property
    def keys(self) -> list[str]:
        """The registered keys of the steps."""
        return [spec_for(step).key for step in self.steps]

    async def __call__(self, **kwargs: object) -> None:
        """Run every step here, in order (what a worker does over time)."""
        payload: dict[str, Any] = dump_arguments(kwargs)
        for step in self.steps:
            payload = next_arguments(payload, await call_task(step, payload))


def chain(first: Task, *then: Task) -> Chain:
    """Run ``first``, then each task of ``then`` once the one before succeeds.

    Every step must be a registered task.
    """
    steps = (first, *then)
    for step in steps:
        spec_for(step)  # Refuse unregistered tasks now, not in the worker.
    return Chain(steps)


def next_arguments(arguments: Mapping[str, Any], result: object) -> dict[str, Any]:
    """The JSON arguments of a chain's next step, from the previous one's run."""
    if result is None:
        return dict(arguments)
    if isinstance(result, Mapping):
        return dump_arguments(result)
    return dump_arguments({"result": result})
