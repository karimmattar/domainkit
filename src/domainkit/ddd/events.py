"""Domain events and the in-process bus that delivers them.

Aggregates record events; a unit of work publishes them after commit; handlers
subscribed in ``DomainConfig.ready()`` react. This is how one aggregate or
bounded context reacts to another without importing its internals (Django
signals play the same role).

Delivery is in-process and best effort: a failing handler is logged and does
not stop the others, and one that runs past its timeout (``EVENT_HANDLER_TIMEOUT``,
or ``timeout=`` when subscribing) is cut off, logged, and reported, so it
cannot hold the request that committed for ever. Subscribe with
``durable=True`` to run a handler from the outbox instead, in a worker, where
it survives crashes and is retried.
"""

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final
from uuid import UUID, uuid4

from domainkit import clock
from domainkit.ddd._dataclass import MessageMeta
from domainkit.exceptions import DomainkitError

logger = logging.getLogger(__name__)

_MAX_DURABLE_NAME: Final = 255
"""The longest name of a durable handler: the outbox's ``target`` column."""

DEFAULT_HANDLER_TIMEOUT: Final = 10.0
"""Seconds an in-process handler may run, unless settings or ``subscribe`` say."""


def _now() -> datetime:
    """Return the current time in UTC."""
    return clock.now()


class DomainEvent(metaclass=MessageMeta):
    """Something that happened in the domain, named in the past tense.

    Subclasses are frozen, keyword-only dataclasses. Prefer plain values
    (ids, strings, numbers) over domain objects, so events stay valid
    messages after the model changes.
    """

    event_id: UUID = field(default_factory=uuid4)
    occurred_at: datetime = field(default_factory=_now)


type EventHandler[E: DomainEvent] = Callable[[E], Awaitable[None]]


def _handler_name(handler: Callable[..., object]) -> str:
    """``module.QualName`` of ``handler``: its messages' key when it is durable."""
    module = getattr(handler, "__module__", "?")
    return f"{module}.{getattr(handler, '__qualname__', handler)}"


class HandlerTimedOut(TimeoutError, DomainkitError):
    """An in-process handler ran past its timeout and was cut off."""


@dataclass(frozen=True, slots=True)
class _Subscription:
    """One handler of an event type, and how it runs."""

    handler: EventHandler[Any]
    durable: bool
    timeout: float | None
    """Seconds it may run in-process; None means the bus's ``handler_timeout``."""


class EventBus:
    """Deliver events to handlers subscribed to their class or a base class.

    In-process handlers run right after the commit, inside the request,
    each within its timeout; a failure or an overrun is logged and reported,
    then skipped. Durable handlers (``durable=True``) run later in a worker,
    from the outbox written in the same transaction: they survive crashes
    and are retried, so they must be idempotent. Durable delivery needs
    ``domainkit.contrib.messaging``.
    """

    def __init__(self, handler_timeout: float = DEFAULT_HANDLER_TIMEOUT) -> None:
        """Start with no subscriptions; handlers run ``handler_timeout`` at most.

        Raises:
            ValueError: ``handler_timeout`` is not a positive number of seconds.
        """
        if not 0 < handler_timeout < float("inf"):
            raise ValueError("handler_timeout is a positive number of seconds.")
        self._handlers: dict[type[DomainEvent], list[_Subscription]] = {}
        self._lock = threading.Lock()
        """Guards changes to the subscriptions: loops on several threads subscribe."""
        self.error_hook: Callable[[BaseException, DomainEvent], None] | None = None
        """Called with each in-process handler failure (``setup()`` wires it)."""
        self.handler_timeout = handler_timeout
        """Seconds an in-process handler may run (``setup()`` sets
        ``EVENT_HANDLER_TIMEOUT``); ``subscribe(timeout=...)`` overrides it."""

    def subscribe[E: DomainEvent](
        self,
        event_type: type[E],
        handler: EventHandler[E],
        *,
        durable: bool = False,
        timeout: float | None = None,
    ) -> None:
        """Call ``handler`` for every ``event_type`` event; repeats are ignored.

        ``timeout`` gives an in-process handler its own limit in seconds
        (default: the bus's ``handler_timeout``). Durable handlers follow the
        worker's task policies instead. Threads that subscribe the same
        handler at once subscribe it once.

        A durable handler's name, ``module.QualName``, is the key of its
        messages in the outbox, which stores at most
        255 characters of it.

        Raises:
            ValueError: ``timeout`` is not positive, or the handler is durable;
                or a durable handler's name is longer than the outbox stores.
        """
        if timeout is not None and (durable or not 0 < timeout < float("inf")):
            raise ValueError(
                "timeout is a positive number of seconds, for in-process handlers."
            )
        if durable:
            name = _handler_name(handler)
            if len(name) > _MAX_DURABLE_NAME:
                raise ValueError(
                    f"A durable handler's name is the key of its messages, of at "
                    f"most {_MAX_DURABLE_NAME} characters; {name[:60]!r}... "
                    f"has {len(name)}: define it in a shorter module or name."
                )
        with self._lock:
            subscriptions = self._handlers.setdefault(event_type, [])
            if not any(
                entry.handler == handler and entry.durable is durable
                for entry in subscriptions
            ):
                subscriptions.append(_Subscription(handler, durable, timeout))

    def unsubscribe[E: DomainEvent](
        self, event_type: type[E], handler: EventHandler[E]
    ) -> None:
        """Stop calling ``handler``, durable or not; unknown handlers are ignored."""
        with self._lock:
            subscriptions = self._handlers.get(event_type, [])
            subscriptions[:] = [
                entry for entry in subscriptions if entry.handler != handler
            ]

    def clear(self) -> None:
        """Remove every subscription."""
        with self._lock:
            self._handlers.clear()

    @contextmanager
    def subscriptions_kept(self) -> Iterator[None]:
        """Put the subscriptions back as they were when the block ends.

        What the block subscribed is dropped, and what it unsubscribed or
        cleared comes back: the test plugin keeps each test's to itself.
        """
        with self._lock:
            kept = {kind: list(entries) for kind, entries in self._handlers.items()}
        try:
            yield
        finally:
            with self._lock:
                self._handlers.clear()
                self._handlers.update(kept)

    def handlers_for(
        self, event_type: type[DomainEvent], *, durable: bool = False
    ) -> list[EventHandler[Any]]:
        """Return in-process (or durable) handlers, most specific first."""
        return [entry.handler for entry in self._subscriptions(event_type, durable)]

    def durable_subscriptions(
        self,
    ) -> list[tuple[type[DomainEvent], EventHandler[Any]]]:
        """Return every durable ``(event type, handler)`` pair."""
        with self._lock:  # A type subscribed meanwhile would change the dict.
            return [
                (event_type, entry.handler)
                for event_type, subscriptions in self._handlers.items()
                for entry in subscriptions
                if entry.durable
            ]

    def subscriptions(
        self,
    ) -> list[tuple[type[DomainEvent], EventHandler[Any], bool]]:
        """Return every ``(event type, handler, durable)`` subscription."""
        with self._lock:
            return [
                (event_type, entry.handler, entry.durable)
                for event_type, subscriptions in self._handlers.items()
                for entry in subscriptions
            ]

    async def publish(self, event: DomainEvent) -> None:
        """Run the in-process handlers for ``event`` in order, isolating failures.

        Each runs within its timeout; one still running then is cancelled,
        logged, and reported as ``HandlerTimedOut``, and the next one runs.
        """
        for entry in self._subscriptions(type(event), durable=False):
            name = getattr(entry.handler, "__qualname__", entry.handler)
            seconds = entry.timeout or self.handler_timeout
            try:
                async with asyncio.timeout(seconds) as limit:
                    await entry.handler(event)
            except Exception as exc:
                failure: Exception = exc
                if limit.expired():
                    failure = HandlerTimedOut(
                        f"Handler {name} took more than {seconds:g}s for "
                        f"{type(event).__name__} {event.event_id}"
                    )
                    logger.error("%s; cut off", failure)
                else:
                    logger.exception(
                        "Handler %s failed for %s %s",
                        name,
                        type(event).__name__,
                        event.event_id,
                    )
                if self.error_hook is not None:
                    self.error_hook(failure, event)

    async def publish_all(self, events: Iterable[DomainEvent]) -> None:
        """Publish ``events`` one after another, in order."""
        for event in events:
            await self.publish(event)

    def _subscriptions(
        self, event_type: type[DomainEvent], durable: bool
    ) -> list[_Subscription]:
        """In-process (or durable) subscriptions, most specific event type first."""
        return [
            entry
            for cls in event_type.__mro__
            for entry in self._handlers.get(cls, ())
            if entry.durable is durable
        ]


event_bus: Final = EventBus()
"""Process-wide bus used by units of work unless they are given another."""

_HANDLES: Final = "__domainkit_handles__"
"""Where a handler keeps the events ``@handles`` marks it for."""


@dataclass(frozen=True, slots=True)
class _Mark:
    """One ``@handles``: the event, and how the handler runs."""

    event_type: type[DomainEvent]
    durable: bool
    timeout: float | None


def handles[H: Callable[..., Awaitable[None]]](
    event_type: type[DomainEvent],
    *,
    durable: bool = False,
    timeout: float | None = None,
) -> Callable[[H], H]:
    """Mark a handler for ``event_type`` events (Django's ``@receiver``).

    The mark subscribes nothing by itself: a four-file domain's
    ``controllers.py`` is read when the domain is ready, and its marked
    handlers are subscribed to ``event_bus`` then (``subscribe_marked``),
    as ``subscribe(event_type, handler, durable=..., timeout=...)`` would.
    A handler may handle several events.
    """

    def mark(handler: H) -> H:
        marks: list[_Mark] = handler.__dict__.setdefault(_HANDLES, [])
        marks.append(_Mark(event_type, durable, timeout))
        return handler

    return mark


def subscribe_marked(module: Any, bus: EventBus | None = None) -> int:  # noqa: ANN401 - any module.
    """Subscribe the handlers ``module`` defines and ``@handles`` marks.

    Handlers it only imports are left to the module that defines them.
    Returns how many subscriptions it made.

    Raises:
        ValueError: A mark ``subscribe`` refuses (see there).
    """
    target = event_bus if bus is None else bus
    made = 0
    for value in vars(module).values():
        if getattr(value, "__module__", None) != module.__name__:
            continue
        for mark in getattr(value, _HANDLES, ()):
            target.subscribe(
                mark.event_type, value, durable=mark.durable, timeout=mark.timeout
            )
            made += 1
    return made
