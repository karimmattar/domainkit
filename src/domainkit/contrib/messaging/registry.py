"""Registered tasks and durable handlers, and their (de)serialization.

The worker only runs code registered here: it never imports a name read
from the database. Events and task arguments travel as JSON, validated
against the event class or the task signature on the way back.
"""

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from functools import cache
from typing import Any, overload

from pydantic import TypeAdapter, validate_call
from pydantic_core import to_jsonable_python

from domainkit.apps import registry
from domainkit.conf.global_settings import NAME_PATTERN
from domainkit.ddd import DomainEvent, EventBus
from domainkit.exceptions import ImproperlyConfigured

from .tables import DEFAULT_QUEUE

type Task = Callable[..., Awaitable[None]]


@dataclass(frozen=True, slots=True)
class TaskSpec:
    """A registered task."""

    key: str
    function: Task
    max_attempts: int | None = None
    every: timedelta | None = None
    """Set for periodic tasks."""
    queue: str = DEFAULT_QUEUE
    """The queue whose workers run it (``runworker --queue``)."""


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


@overload
def task(function: Task, /) -> Task: ...


@overload
def task(
    *,
    name: str | None = None,
    max_attempts: int | None = None,
    queue: str | None = None,
) -> Callable[[Task], Task]: ...


def task(
    function: Task | None = None,
    /,
    *,
    name: str | None = None,
    max_attempts: int | None = None,
    queue: str | None = None,
) -> Task | Callable[[Task], Task]:
    """Register an async function as a background task (``uow.defer(task)``).

    Arguments must be JSON-friendly or Pydantic-validatable (UUIDs, dates,
    models); they are checked against the signature when the task runs.
    ``queue`` (default: ``default``) picks the workers that run it.
    """
    checked = checked_queue(queue)

    def register(func: Task) -> Task:
        key = name or qualified_name(func)
        _register(TaskSpec(key, func, max_attempts, queue=checked))
        return func

    return register(function) if function is not None else register


def periodic(
    every: timedelta,
    *,
    name: str | None = None,
    max_attempts: int | None = None,
    queue: str | None = None,
) -> Callable[[Task], Task]:
    """Register an argument-free task that runs once per ``every``, cluster-wide."""
    if every <= timedelta(0):
        raise ImproperlyConfigured("Periodic tasks need a positive interval.")
    checked = checked_queue(queue)

    def register(func: Task) -> Task:
        key = name or qualified_name(func)
        _register(TaskSpec(key, func, max_attempts, every, checked))
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
    """Return the periodic tasks to schedule, with their intervals.

    Tasks shipped by a ``domainkit.contrib`` app run only while that app is
    installed; project tasks are imported only by installed domains.
    """
    return [
        (spec, spec.every)
        for spec in _tasks.values()
        if spec.every is not None and _owner_installed(spec.function)
    ]


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
    """Serialize ``event`` to JSON-compatible values."""
    payload: dict[str, Any] = _adapter(type(event)).dump_python(event, mode="json")
    return payload


def load_event[E: DomainEvent](event_type: type[E], payload: Mapping[str, Any]) -> E:
    """Rebuild an event of ``event_type`` from its JSON payload."""
    event: E = _adapter(event_type).validate_python(payload)
    return event


def dump_arguments(arguments: Mapping[str, object]) -> dict[str, Any]:
    """Serialize task arguments to JSON-compatible values."""
    payload: dict[str, Any] = to_jsonable_python(dict(arguments))
    return payload


@cache
def _validated(function: Task) -> Task:
    """Wrap ``function`` so JSON arguments are converted to its parameter types."""
    validated: Task = validate_call(function)
    return validated


async def call_task(function: Task, payload: Mapping[str, Any]) -> None:
    """Run ``function`` with arguments converted back from JSON."""
    await _validated(function)(**payload)
