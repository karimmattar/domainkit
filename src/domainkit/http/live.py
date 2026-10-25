"""Long-lived connections: WebSockets and event streams, bounded and re-checked.

WebSockets (``WebSocketLimitsMiddleware``) and event streams
(``EventSourceResponse``) share what this module keeps:

- **Caps.** ``LIVE_MAX_CONNECTIONS`` open connections per process, and
  ``LIVE_MAX_CONNECTIONS_PER_CALLER`` per user (or per address, for
  anonymous callers). One more is refused at once: a 503 with
  ``Retry-After``, or a 1013 close where a WebSocket handshake cannot be
  answered with a status.
- **Clocks.** A connection ends when it reaches its lifetime, or a
  WebSocket when it is idle, on domainkit's clock (``domainkit.clock``), so
  ``freeze_time`` and ``tick`` move them in tests.
- **Re-authorization.** Every ``LIVE_REAUTH_INTERVAL`` seconds, the
  credentials of the handshake are checked again, and the access rules of
  the route (``Require``) with them. A token revoked or expired, a user
  deactivated, or a permission removed ends the connection with 1008.

A route may set its own limits with ``LiveLimits``::

    @router.websocket(
        "/chat", dependencies=[Depends(Require()), Depends(LiveLimits(idle_timeout=60))]
    )
    async def chat(websocket: WebSocket) -> None: ...
"""

import logging
import math
import random
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final, TypedDict, Unpack

import anyio
from fastapi import status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.requests import HTTPConnection
from starlette.types import Message, Receive, Scope, Send

from domainkit.clock import frozen, monotonic
from domainkit.conf import settings
from domainkit.conf.global_settings import MAX_REAUTH_INTERVAL
from domainkit.context import current_request
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.errors import error_response
from domainkit.http.ratelimit import address_key
from domainkit.log import SECURITY_LOGGER, ThrottledLog
from domainkit.utils.rates import parse_rate

__all__ = [
    "Closed",
    "Closure",
    "LiveLimits",
]

logger = logging.getLogger(__name__)
security_logger = logging.getLogger(SECURITY_LOGGER)

LIMITS: Final = "domainkit.live.limits"
"""Scope key: the limits a route sets for itself (``LiveLimits``)."""
REAUTHENTICATE: Final = "domainkit.live.reauthenticate"
"""Scope key: checks the credentials again (set by ``AuthenticationMiddleware``)."""
RULES: Final = "domainkit.live.rules"
"""Scope key: the access checks the route passed, to run again (``Require``)."""
LONG_LIVED: Final = "domainkit.long_lived"
"""Scope key, True for a connection that may outlast any request deadline."""
DENIAL: Final = "websocket.http.response"
"""The ASGI extension that lets a WebSocket handshake be refused with a status."""

TRY_AGAIN_LATER: Final = status.WS_1013_TRY_AGAIN_LATER
"""WebSocket close code: the server is busy; try again later."""
REAUTH_TIMEOUT: Final = 10.0
"""Seconds a re-check of credentials and rules may take; then it counts as failed."""
FROZEN_STEP: Final = 0.005
"""Seconds between readings of a frozen clock, which moves only when ticked."""
RETRY_AFTER: Final = (5, 15)
"""Seconds a refused caller is told to wait: a random whole number in this range."""
TOO_MANY_CONNECTIONS: Final = "Too many open connections; try again later."


@dataclass(frozen=True, slots=True)
class Closure:
    """Why the framework ended a connection: the close code and its reason."""

    code: int
    """The WebSocket close code (RFC 6455); an event stream just ends."""
    reason: str
    """Said in the close frame, or in a comment at the end of an event stream."""


IDLE: Final = Closure(status.WS_1001_GOING_AWAY, "Idle for too long.")
LIFETIME: Final = Closure(status.WS_1001_GOING_AWAY, "Connection lifetime reached.")
TOO_SLOW: Final = Closure(TRY_AGAIN_LATER, "Client too slow.")
TOO_MANY_MESSAGES: Final = Closure(
    status.WS_1008_POLICY_VIOLATION, "Too many messages."
)
NOT_AUTHORIZED: Final = Closure(status.WS_1008_POLICY_VIOLATION, "Not authorized.")
UNVERIFIED: Final = Closure(TRY_AGAIN_LATER, "Authorization not confirmed.")
GONE: Final = Closure(status.WS_1006_ABNORMAL_CLOSURE, "Client gone.")
"""The client went away; nothing more can be sent."""
NORMAL_ENDS: Final = frozenset({IDLE, LIFETIME, GONE})
"""Closures that end a connection in the normal course, logged at INFO."""


class Closed(Exception):
    """Raised by an event source to end its connection, saying why."""

    def __init__(self, closure: Closure) -> None:
        """End the connection with ``closure``."""
        super().__init__(closure.reason)
        self.closure = closure


# Limits ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Limits:
    """The limits one connection runs under: the settings, then its route's."""

    max_lifetime: float
    """Seconds the connection may stay open."""
    send_queue: int
    """Messages that may wait to be sent; a WebSocket's sender waits for room."""
    send_timeout: float
    """Seconds one message may take to reach the client; then it is too slow."""
    reauth_interval: float | None
    """Seconds between re-checks of the credentials and rules; None: never."""
    idle_timeout: float | None = None
    """Seconds without a message either way before a WebSocket ends; None: never."""
    message_rate: tuple[int, float] | None = None
    """Messages a WebSocket client may send, per period in seconds; None: any."""
    keepalive: float | None = None
    """Seconds of silence before an event stream sends a keep-alive comment."""


class LimitOverrides(TypedDict, total=False):
    """The limits a route may set for itself (``LiveLimits``)."""

    max_lifetime: float
    send_queue: int
    send_timeout: float
    reauth_interval: float | None
    idle_timeout: float | None
    message_rate: str | None
    keepalive: float


class _Overrides(BaseModel):
    """``LimitOverrides``, checked as the settings they replace are."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    max_lifetime: float = Field(default=1.0, gt=0)
    send_queue: int = Field(default=1, ge=1, le=10_000)
    send_timeout: float = Field(default=1.0, gt=0)
    reauth_interval: float | None = Field(default=None, gt=0, le=MAX_REAUTH_INTERVAL)
    idle_timeout: float | None = Field(default=None, gt=0)
    message_rate: str | None = None
    keepalive: float = Field(default=1.0, gt=0)

    @field_validator("message_rate")
    @classmethod
    def _rate(cls, rate: str | None) -> str | None:
        """Refuse a rate ``parse_rate`` cannot read."""
        if rate is not None:
            parse_rate(rate)
        return rate


class LiveLimits:
    """A FastAPI dependency: a route's own limits for its connections.

    ``Depends(LiveLimits(idle_timeout=60, max_lifetime=600))`` replaces the
    settings for the WebSockets or event streams of that route only. The
    keys are those of ``Limits`` (``message_rate`` as text, ``"5/second"``);
    the caps on open connections are the process's and cannot be changed
    here. It is not an access rule: the route still needs one.
    """

    def __init__(self, **overrides: Unpack[LimitOverrides]) -> None:
        """Keep ``overrides``, checked now so a bad value fails at import."""
        try:
            checked = _Overrides.model_validate(overrides)
        except ValueError as exc:
            raise ImproperlyConfigured(f"LiveLimits: {exc}") from None
        self.overrides = checked.model_dump(include=checked.model_fields_set)

    async def __call__(self, connection: HTTPConnection) -> None:
        """Leave the overrides where the connection's limits are read."""
        connection.scope.setdefault(LIMITS, {}).update(self.overrides)


def websocket_limits(scope: Scope) -> Limits:
    """A WebSocket's limits: ``WEBSOCKET_*`` and ``LIVE_*``, or its route's."""
    given = scope.get(LIMITS, {})
    rate = given.get("message_rate", settings.WEBSOCKET_MESSAGE_RATE)
    return Limits(
        max_lifetime=given.get("max_lifetime", settings.WEBSOCKET_MAX_LIFETIME),
        idle_timeout=given.get("idle_timeout", settings.WEBSOCKET_IDLE_TIMEOUT),
        message_rate=None if rate is None else parse_rate(rate),
        **_shared(given),
    )


def stream_limits(scope: Scope) -> Limits:
    """An event stream's limits: ``SSE_*`` and ``LIVE_*``, or its route's."""
    given = scope.get(LIMITS, {})
    return Limits(
        max_lifetime=given.get("max_lifetime", settings.SSE_MAX_LIFETIME),
        keepalive=given.get("keepalive", settings.SSE_KEEPALIVE),
        **_shared(given),
    )


def send_queue(scope: Scope) -> int:
    """How many messages or events the connection of ``scope`` may buffer."""
    size: int = _shared(scope.get(LIMITS, {}))["send_queue"]
    return size


def _shared(given: dict[str, Any]) -> dict[str, Any]:
    """The limits WebSockets and event streams share, from ``LIVE_*`` or ``given``."""
    return {
        "send_queue": given.get("send_queue", settings.LIVE_SEND_QUEUE),
        "send_timeout": given.get("send_timeout", settings.LIVE_SEND_TIMEOUT),
        "reauth_interval": given.get("reauth_interval", settings.LIVE_REAUTH_INTERVAL),
    }


# Caps --------------------------------------------------------------------------------


class Slots:
    """Open connections of the process, and of each caller, under their caps.

    Event loops on several threads (free-threaded Python, or a server with
    a loop per thread) share one application's slots, so they are counted
    under a lock.
    """

    def __init__(self, total: int, per_caller: int) -> None:
        """Allow ``total`` open connections, ``per_caller`` of them per caller."""
        self.total = total
        self.per_caller = per_caller
        self.open = 0
        self.callers: dict[str, int] = {}
        """Open connections per caller; callers with none are forgotten."""
        self.log = ThrottledLog(logger)
        """Refusals of connections, at most a line per caller a while."""
        self._lock = threading.Lock()

    def acquire(self, caller: str) -> str | None:
        """Take a slot for ``caller``; if none is free, say which cap is full."""
        with self._lock:
            if self.open >= self.total:
                return "LIVE_MAX_CONNECTIONS"
            held = self.callers.get(caller, 0)
            if held >= self.per_caller:
                return "LIVE_MAX_CONNECTIONS_PER_CALLER"
            self.open += 1
            self.callers[caller] = held + 1
            return None

    def release(self, caller: str) -> None:
        """Give back a slot ``caller`` took; a caller holding none changes nothing."""
        with self._lock:
            held = self.callers.pop(caller, 0)
            if not held:
                return
            self.open -= 1
            if held > 1:
                self.callers[caller] = held - 1


_making_slots: Final = threading.Lock()
"""Held while an application's slots are looked up, and made on first use."""


def slots_for(scope: Scope) -> Slots:
    """The application's slots, made from the settings on first use.

    Loops on several threads may open the first connections at once; they
    all get the one ``Slots``.
    """
    state = scope["app"].state
    with _making_slots:
        slots: Slots | None = getattr(state, "live_slots", None)
        if slots is None:
            slots = state.live_slots = Slots(
                settings.LIVE_MAX_CONNECTIONS, settings.LIVE_MAX_CONNECTIONS_PER_CALLER
            )
    return slots


def caller_of(scope: Scope) -> str:
    """Who holds a connection: the actor if authenticated, else the address."""
    request = current_request()
    if request is not None and request.actor:
        return f"actor:{request.actor}"
    return address_key(scope)


async def refuse(
    scope: Scope,
    receive: Receive,
    send: Send,
    cap: str,
    *,
    slots: Slots | None = None,
) -> None:
    """Refuse a connection over ``cap``: 503 with ``Retry-After``, or close 1013.

    A WebSocket handshake gets the 503 where the server can answer one with
    a status, and a 1013 (try again later) close where it cannot. With the
    ``slots`` that refused it, the warning is throttled per caller
    (``ThrottledLog``).
    """
    message = "Refused a live connection: %s is reached"
    if slots is None:
        logger.warning(message, cap)
    else:
        slots.log.warning("live_refused", caller_of(scope), message, cap)
    if scope["type"] == "websocket" and DENIAL not in scope.get("extensions", {}):
        await send({"type": "websocket.close", "code": TRY_AGAIN_LATER})
        return
    response = error_response(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        TOO_MANY_CONNECTIONS,
        code="too_many_connections",
    )
    # Spread the retries: a full process should not hear them all at once.
    response.headers["Retry-After"] = str(random.randint(*RETRY_AFTER))  # noqa: S311
    await response(scope, receive, send)


# Re-authorization --------------------------------------------------------------------


type Reauthenticate = Callable[[Scope], Awaitable[bool]]
"""Whether a connection's credentials still name the same principal."""
type AccessCheck = Callable[[HTTPConnection], Awaitable[bool]]
"""Whether a route's access rules still allow the connection."""


def record_check(connection: HTTPConnection, check: AccessCheck) -> None:
    """Keep ``check`` to run again while the connection stays open."""
    connection.scope.setdefault(RULES, []).append(check)


async def reauthorize(scope: Scope) -> bool:
    """Whether the credentials still hold, and the route's rules still allow.

    The credentials are checked first, which reloads the user (with its
    permissions) that the rules then see.
    """
    reauthenticate: Reauthenticate | None = scope.get(REAUTHENTICATE)
    if reauthenticate is not None and not await reauthenticate(scope):
        return False
    connection = HTTPConnection(scope)
    checks: list[AccessCheck] = scope.get(RULES, [])
    for allows in checks:
        if not await allows(connection):
            security_logger.warning(
                "An open connection's access rules no longer allow it"
            )
            return False
    return True


# Clocks ------------------------------------------------------------------------------


async def sleep_until(deadline: float) -> None:
    """Wait until ``monotonic()`` reaches ``deadline``.

    A frozen clock moves only when ticked, so under one the time is read
    again every ``FROZEN_STEP`` seconds.
    """
    while (left := deadline - monotonic()) > 0:  # noqa: ASYNC110 - see above.
        await anyio.sleep(min(left, FROZEN_STEP) if frozen() is not None else left)


def _spread(interval: float) -> float:
    """``interval``, shortened by up to a quarter, so re-checks do not bunch."""
    return interval * random.uniform(0.75, 1.0)  # noqa: S311 - not a secret.


def _nothing() -> None:
    """Do nothing: the keep-alive of connections that send none."""


class LiveConnection:
    """One open connection's clocks, and the watch that ends it on time.

    Times are ``monotonic()`` readings: the connection opened at ``opened``
    and was last active (a message either way, or a write) at ``active``.
    """

    def __init__(self, scope: Scope, limits: Limits) -> None:
        """Start the clocks of the connection of ``scope`` now."""
        self.scope = scope
        self.limits = limits
        self.opened = self.active = now = monotonic()
        self.next_check = math.inf
        self._schedule(now)

    def expired(self, now: float) -> Closure | None:
        """The closure that is due at ``now``, if any."""
        if now >= self.opened + self.limits.max_lifetime:
            return LIFETIME
        idle = self.limits.idle_timeout
        if idle is not None and now >= self.active + idle:
            return IDLE
        return None

    def deadline(self) -> float:
        """When the watch must look again."""
        limits = self.limits
        times = [self.opened + limits.max_lifetime, self.next_check]
        pauses = (limits.idle_timeout, limits.keepalive)
        times.extend(self.active + pause for pause in pauses if pause is not None)
        return min(times)

    async def watch(
        self,
        close: Callable[[Closure], None],
        keepalive: Callable[[], None] = _nothing,
    ) -> None:
        """Close the connection when a limit is reached, or authorization ends.

        ``keepalive`` is called after ``limits.keepalive`` seconds of silence.
        """
        pause = self.limits.keepalive
        while True:
            now = monotonic()
            closure = self.expired(now)
            if closure is None and now >= self.next_check:
                closure = await self.recheck()
            if closure is not None:
                close(closure)
                return
            if pause is not None and now >= self.active + pause:
                self.active = now
                keepalive()
            await sleep_until(self.deadline())

    async def deliver(self, send: Send, message: Message) -> Closure | None:
        """Send ``message`` within the send timeout; else the closure that ends it.

        A client that is gone (the server raises ``OSError``, as ASGI asks) is
        ``GONE``; one that takes longer to receive the message is
        ``TOO_SLOW``. The connection is active from when the message set out.
        """
        started = monotonic()
        with anyio.move_on_after(self.limits.send_timeout) as timer:
            try:
                await send(message)
            except OSError:
                return GONE
        if timer.cancelled_caught:
            return TOO_SLOW
        self.active = max(self.active, started)
        return None

    async def recheck(self) -> Closure | None:
        """Check the credentials and rules again; the closure if they fail.

        A check that fails or takes longer than ``REAUTH_TIMEOUT`` cannot
        confirm anything, so it ends the connection too, with 1013. The next
        check is an interval after this one began. A connection that ends
        meanwhile waits for its check, so no query is cut off halfway.
        """
        started = monotonic()
        try:
            with anyio.CancelScope(shield=True), anyio.fail_after(REAUTH_TIMEOUT):
                allowed = await reauthorize(self.scope)
        except TimeoutError:
            logger.warning(
                "Re-checking an open connection took over %.0fs", REAUTH_TIMEOUT
            )
            return UNVERIFIED
        except Exception:
            logger.exception("Could not re-check an open connection")
            return UNVERIFIED
        if not allowed:
            return NOT_AUTHORIZED
        self._schedule(started)
        return None

    def _schedule(self, now: float) -> None:
        """Set when the credentials and rules are next checked."""
        interval = self.limits.reauth_interval
        if interval is not None:
            self.next_check = now + _spread(interval)
