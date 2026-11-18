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
it survives crashes and is retried. A durable handler's messages carry its
key: the ``name=`` it was subscribed with, or else ``module.QualName``.

Each event names its ``actor``: who caused it, read from the request, job,
or command it was made in (ADR 0147). The domain layer reads no request:
``domainkit.context`` tells this module where to ask when it is imported.
"""

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, fields
from datetime import datetime
from functools import cache
from typing import Any, Final, NamedTuple
from uuid import UUID, uuid4

from domainkit import clock
from domainkit.ddd._dataclass import MessageMeta
from domainkit.exceptions import DomainkitError

logger = logging.getLogger(__name__)

_MAX_DURABLE_NAME: Final = 255
"""The longest key of a durable handler: the outbox's ``target`` column."""

DEFAULT_HANDLER_TIMEOUT: Final = 10.0
"""Seconds an in-process handler may run, unless settings or ``subscribe`` say."""


SYSTEM_ACTOR: Final = "system"
"""The actor of work no caller asked for: periodic tasks, commands' messages."""


def _now() -> datetime:
    """Return the current time in UTC."""
    return clock.now()


@dataclass(frozen=True, slots=True)
class Actor:
    """Who caused an event: a user, staff acting as one, an operator, or the system.

    ``id`` is the request's actor, as logs and the audit trail write it: a
    user's id, ``operator:<name>`` for a command an operator ran, or
    ``SYSTEM_ACTOR`` for work no caller asked for (periodic tasks).
    """

    id: str
    impersonating: str | None = None
    """The id of the user ``id`` acted as (staff impersonating them); None
    when ``id`` acted in their own name."""

    @property
    def is_system(self) -> bool:
        """Whether no one asked for the work: a periodic task, say."""
        return self.id == SYSTEM_ACTOR

    def is_self(self, subject: object) -> bool:
        """Whether ``subject``, an id, acted in their own name.

        False for staff who acted on them, directly or as them, for an
        operator, and for the system.
        """
        return self.impersonating is None and self.id == str(subject)


type ActorSource = Callable[[], Actor | None]
"""Who acts in the current context: a request's user, a job's, a command's."""

_actor_source: list[ActorSource] = [lambda: None]
"""Where events made without an ``actor`` ask (``read_actors_from``): no one
until ``domainkit.context`` is imported, and no request is bound before."""


def read_actors_from(source: ActorSource) -> None:
    """Make events made without an ``actor`` ask ``source`` who acts.

    ``domainkit.context`` gives its ``current_actor`` when it is imported,
    which every request, job, and command does first: the domain layer
    imports no request (ADR 0147).
    """
    _actor_source[0] = source


def _acting() -> Actor | None:
    """Who acts where an event is made; None outside requests, jobs, commands."""
    return _actor_source[0]()


class DomainEvent(metaclass=MessageMeta):
    """Something that happened in the domain, named in the past tense.

    Subclasses are frozen, keyword-only dataclasses. Prefer plain values
    (ids, strings, numbers) over domain objects, so events stay valid
    messages after the model changes.
    """

    event_id: UUID = field(default_factory=uuid4)
    occurred_at: datetime = field(default_factory=_now)
    actor: Actor | None = field(default_factory=_acting)
    """Who caused it, as the request, job, or command it was made in says
    (ADR 0147); None for an anonymous caller, or outside all three."""


_ACTOR: Final = next(each for each in fields(DomainEvent) if each.name == "actor")
"""``DomainEvent.actor``: every subclass that declares none shares this field."""


@cache
def inherits_actor(event_type: type[DomainEvent]) -> bool:
    """Whether ``event_type``'s ``actor`` is ``DomainEvent.actor``.

    False for a class that declares an ``actor`` of its own, from before
    events had one: webhooks, live events, and the audit trail keep that
    field as they did, and leave ``DomainEvent.actor`` out.
    """
    return any(each is _ACTOR for each in fields(event_type))


type EventHandler[E: DomainEvent] = Callable[[E], Awaitable[None]]


def _handler_name(handler: Callable[..., object]) -> str:
    """``module.QualName`` of ``handler``: its messages' key when it is durable."""
    module = getattr(handler, "__module__", "?")
    return f"{module}.{getattr(handler, '__qualname__', handler)}"


def _checked_key(key: str, what: str) -> str:
    """Return ``key`` if the outbox can store it as a message's target.

    Raises:
        ValueError: ``key`` is empty, or longer than the outbox stores.
    """
    if not key:
        raise ValueError(f"{what} is the key of its messages: it cannot be empty.")
    if len(key) > _MAX_DURABLE_NAME:
        raise ValueError(
            f"{what} is the key of its messages, of at most {_MAX_DURABLE_NAME} "
            f"characters; {key[:60]!r}... has {len(key)}: define it in a shorter "
            f"module or name."
        )
    return key


class HandlerTimedOut(TimeoutError, DomainkitError):
    """An in-process handler ran past its timeout and was cut off."""


class HandlerCancelled(DomainkitError):
    """In-process handlers did not finish: the work they ran in was cancelled.

    Reported, never raised: the cancellation goes on. Its message names the
    handler cut off, if one was running, and those that did not run.
    """


class HandlerKey(NamedTuple):
    """A key a worker runs messages of: the handler, and the event type it takes."""

    key: str
    event_type: type[DomainEvent]
    handler: EventHandler[Any]
    named: bool
    """Subscribed with ``name=``: its key does not follow its module's path."""
    current: bool
    """Its key now, which new events queue messages under; not one of ``formerly``."""


@dataclass(frozen=True, slots=True)
class _Subscription:
    """One handler of an event type, and how it runs."""

    handler: EventHandler[Any]
    durable: bool
    timeout: float | None
    """Seconds it may run in-process; None means the bus's ``handler_timeout``."""
    key: str | None = None
    """A durable handler's key: its ``name=``, or ``module.QualName``."""
    named: bool = False
    formerly: tuple[str, ...] = ()
    """Keys its messages were queued under before; a worker still runs them."""

    @property
    def answers_to(self) -> set[str]:
        """Every key a worker runs its messages under: its own, and its former ones."""
        return {*self.formerly, *([] if self.key is None else [self.key])}

    def keys(self, event_type: type[DomainEvent]) -> Iterator[HandlerKey]:
        """Its key, if durable, then its former ones."""
        if self.key is not None:
            yield HandlerKey(self.key, event_type, self.handler, self.named, True)
        for key in self.formerly:
            yield HandlerKey(key, event_type, self.handler, self.named, False)


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
        name: str | None = None,
        formerly: Iterable[str] = (),
    ) -> None:
        """Call ``handler`` for every ``event_type`` event; repeats are ignored.

        ``timeout`` gives an in-process handler its own limit in seconds
        (default: the bus's ``handler_timeout``). Durable handlers follow the
        worker's task policies instead. Threads that subscribe the same
        handler at once subscribe it once.

        A durable handler's key is what its messages carry in the outbox, at
        most 255 characters: ``name``, or else ``module.QualName``, which
        moving or renaming the function changes. ``formerly`` lists keys its
        messages were queued under before: an older name, its module path
        before it moved or was named, or a durable handler's key now that it
        runs in-process. A worker runs the messages still queued under them
        with ``handler``; new events queue none under them.

        Raises:
            ValueError: ``timeout`` is not positive, or the handler is durable;
                ``name`` for an in-process handler; a key the outbox cannot
                store; a key another handler answers to, or this handler
                durable under another key.
        """
        if timeout is not None and (durable or not 0 < timeout < float("inf")):
            raise ValueError(
                "timeout is a positive number of seconds, for in-process handlers."
            )
        if name is not None and not durable:
            raise ValueError(
                "name is the key of a durable handler's messages; an in-process "
                "handler has none."
            )
        key = None
        if durable:
            key = _checked_key(
                _handler_name(handler) if name is None else name,
                "A durable handler's name",
            )
        former = tuple(
            _checked_key(each, "A former name")
            for each in dict.fromkeys(formerly)
            if each != key
        )
        entry = _Subscription(handler, durable, timeout, key, name is not None, former)
        with self._lock:
            self._check_keys(entry)
            subscriptions = self._handlers.setdefault(event_type, [])
            if not any(
                each.handler == handler and each.durable is durable
                for each in subscriptions
            ):
                subscriptions.append(entry)

    def _check_keys(self, entry: _Subscription) -> None:
        """Refuse keys another handler answers to, or a second key for one handler.

        Raises:
            ValueError: ``entry`` answers to a key another handler answers
                to, or is durable under a key other than the one its handler
                already has.
        """
        keys = entry.answers_to
        for subscriptions in self._handlers.values():
            for each in subscriptions:
                if each.handler == entry.handler:
                    if entry.durable and each.durable and each.key != entry.key:
                        raise ValueError(
                            f"{_handler_name(entry.handler)} is a durable handler "
                            f"named {each.key!r}; one handler has one name, for "
                            f"every event it handles (formerly= keeps old ones)."
                        )
                    continue
                if clash := keys & each.answers_to:
                    raise ValueError(
                        f"{sorted(clash)[0]!r} is a key of "
                        f"{_handler_name(each.handler)} already; a worker could not "
                        f"tell their messages apart."
                    )

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

    def durable_targets(
        self, event_type: type[DomainEvent]
    ) -> list[tuple[str, EventHandler[Any]]]:
        """Return ``(key, handler)`` for each durable handler, most specific first.

        What a commit of an ``event_type`` event queues: one message per
        handler, under its key.
        """
        return [
            (entry.key, entry.handler)
            for entry in self._subscriptions(event_type, durable=True)
            if entry.key is not None
        ]

    def handler_keys(self) -> list[HandlerKey]:
        """Return every key a worker runs messages of, with its handler.

        Each durable handler's key, for each event type it is subscribed
        to, then each handler's ``formerly`` ones, durable or not.
        """
        with self._lock:
            return [
                key
                for event_type, subscriptions in self._handlers.items()
                for entry in subscriptions
                for key in entry.keys(event_type)
            ]

    def handler_key(self, handler: EventHandler[Any]) -> str:
        """Return the key durable ``handler``'s messages carry.

        Its ``name=``, or ``module.QualName`` when it was given none or is
        not subscribed.
        """
        for key in self.handler_keys():
            if key.current and key.handler == handler:
                return key.key
        return _handler_name(handler)

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
        When the work itself is cancelled (a shutdown, a task cancelled),
        the handler cut off and those after it are logged and reported as
        ``HandlerCancelled``, and the cancellation goes on.
        """
        entries = self._subscriptions(type(event), durable=False)
        for index, entry in enumerate(entries):
            name = getattr(entry.handler, "__qualname__", entry.handler)
            seconds = entry.timeout or self.handler_timeout
            try:
                async with asyncio.timeout(seconds) as limit:
                    await entry.handler(event)
            except asyncio.CancelledError:
                self._cut_off(event, entries[index:], running=True)
                raise
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
        """Publish ``events`` one after another, in order.

        When the work is cancelled, the handlers of the events not reached
        are logged and reported too (``HandlerCancelled``).
        """
        pending = list(events)
        for index, event in enumerate(pending):
            try:
                await self.publish(event)
            except asyncio.CancelledError:
                for later in pending[index + 1 :]:
                    entries = self._subscriptions(type(later), durable=False)
                    self._cut_off(later, entries, running=False)
                raise

    def _cut_off(
        self, event: DomainEvent, entries: list[_Subscription], *, running: bool
    ) -> None:
        """Log and report the handlers of ``event`` a cancellation kept from finishing.

        With ``running``, the first was cut off as it ran; the others did
        not run. Nothing is said when there are none.
        """
        names = [
            str(getattr(each.handler, "__qualname__", each.handler)) for each in entries
        ]
        if not names:
            return
        about = f"{type(event).__name__} {event.event_id}"
        if running:
            message = f"Handler {names[0]} was cancelled for {about}"
            if names[1:]:
                message += f"; {', '.join(names[1:])} did not run"
        else:
            message = (
                f"{', '.join(names)} did not run for {about}: the work was cancelled"
            )
        failure = HandlerCancelled(message)
        logger.error("%s", failure)
        if self.error_hook is not None:
            self.error_hook(failure, event)

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
    name: str | None = None
    formerly: tuple[str, ...] = ()


def handles[H: Callable[..., Awaitable[None]]](
    event_type: type[DomainEvent],
    *,
    durable: bool = False,
    timeout: float | None = None,
    name: str | None = None,
    formerly: Iterable[str] = (),
) -> Callable[[H], H]:
    """Mark a handler for ``event_type`` events (Django's ``@receiver``).

    The mark subscribes nothing by itself: a four-file domain's
    ``controllers.py`` is read when the domain is ready, and its marked
    handlers are subscribed to ``event_bus`` then (``subscribe_marked``),
    as ``subscribe(event_type, handler, durable=..., timeout=...,
    name=..., formerly=...)`` would. A handler may handle several events.
    Give a durable one a ``name`` of its own, which its messages carry
    whatever becomes of the function.
    """

    def mark(handler: H) -> H:
        marks: list[_Mark] = handler.__dict__.setdefault(_HANDLES, [])
        marks.append(_Mark(event_type, durable, timeout, name, tuple(formerly)))
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
                mark.event_type,
                value,
                durable=mark.durable,
                timeout=mark.timeout,
                name=mark.name,
                formerly=mark.formerly,
            )
            made += 1
    return made
