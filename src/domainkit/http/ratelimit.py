"""Request rate limiting (``RATE_LIMIT = "300/minute"``).

Each caller, the authenticated actor or else the client address, has a
token bucket: up to ``limit`` requests at once, refilled smoothly at ``limit``
per period. It is implemented with GCRA (the generic cell rate algorithm),
which keeps one timestamp per caller. Between any two moments a caller gets
at most ``limit`` plus the refill for the time between them, so a window of
one period can see up to about twice ``limit`` after an idle stretch.
Responses carry ``RateLimit-Limit``, ``RateLimit-Remaining``, and
``RateLimit-Reset``; refused ones get 429 with ``Retry-After``.

Callers without an actor are counted by client address, IPv6 ones by their
network (``RATE_LIMIT_IPV6_PREFIX``, a /64): one customer usually holds a
whole /64, and could otherwise change address to escape the limit.

The default backend lives in process memory, so each replica counts on its
own. Behind several replicas, use ``RedisBackend``: one count per caller in
the Redis cache ``RATE_LIMIT_CACHE``, shared by every replica (ADR 0013).

``Throttle`` limits one route (or a group of routes) on its own, on top of
the global limit, with the same backends::

    @router.post("/exports", dependencies=[Require(), Depends(Throttle("5/hour"))])
    async def export(...): ...
"""

import contextlib
import dataclasses
import functools
import hashlib
import inspect
import ipaddress
import logging
import math
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final, Literal, Protocol

from fastapi import status
from starlette.exceptions import WebSocketException
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from domainkit.cache import CacheError, CacheUnavailable, caches
from domainkit.clock import monotonic
from domainkit.conf import settings
from domainkit.conf.global_settings import REDIS_SCHEMES
from domainkit.context import current_request
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.errors import APIError, error_response
from domainkit.http.middleware import (
    ADDRESS_CACHE,
    MAX_CACHED_ADDRESS,
    request_path,
    set_headers,
)
from domainkit.utils.module_loading import import_string
from domainkit.utils.rates import parse_rate

__all__ = [
    "FALLBACK_KEYS",
    "Backend",
    "Decision",
    "MemoryBackend",
    "RateLimitMiddleware",
    "RedisBackend",
    "RefundingBackend",
    "Throttle",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Decision:
    """Whether a request may proceed, and the numbers for the headers."""

    allowed: bool
    remaining: int
    reset_after: float
    """Seconds until the caller's full quota is available again."""
    retry_after: float
    """Seconds until the next request would be allowed (0 if allowed)."""
    counted: bool = True
    """False: the backend could not count the request (Redis failed) and
    let it through; ``AUTH.THROTTLE_FAIL_CLOSED`` refuses such attempts."""


class Backend(Protocol):
    """Where rate-limit state lives.

    A backend may also give a counted request back, with ``refund(key,
    limit, period)`` (``RefundingBackend``): the attempt throttles of
    ``domainkit.contrib.auth`` then count only the attempts that fail.
    Without it, they count every attempt.
    """

    async def hit(self, key: str, limit: int, period: float) -> Decision:
        """Count one request for ``key`` and decide whether to allow it."""
        ...


class RefundingBackend(Backend, Protocol):
    """A backend that can give a counted request back."""

    async def refund(self, key: str, limit: int, period: float) -> None:
        """Give back one request ``hit`` counted for ``key`` at that rate."""
        ...


@dataclass(frozen=True, slots=True)
class _Caller:
    """A caller's GCRA state: requests not yet refilled, as of a reading."""

    seen: float
    """The clock when the caller was last let through."""
    owed: float
    """Requests not yet refilled then, in emission intervals."""
    interval: float
    """The emission interval (``period / limit``) they were counted in."""


EVICTIONS_PER_HIT: Final = 64
"""Idle callers ``MemoryBackend`` forgets at most per request."""


class MemoryBackend:
    """GCRA state in this process, for at most ``max_keys`` callers.

    Callers are kept in the order they were last seen. Each request forgets
    up to ``EVICTIONS_PER_HIT`` of the longest-unseen callers whose quota is
    full again, which changes no decision; after a lull, the idle ones go a
    few at a time, over the next requests. Past ``max_keys``, the
    longest-unseen caller is forgotten anyway and starts afresh
    (``full="forget"``). Memory and the cost of a request stay bounded
    however many callers there are.

    With ``full="refuse"``, a caller who still owes requests is never
    forgotten: while every place holds one, a caller not counted yet is
    refused until the longest-unseen one's quota is full again. It fails
    closed: a flood of new keys cannot buy a throttled one a fresh quota,
    but it can keep new callers out for as long as it keeps the places
    owing. ``RedisBackend``'s fallback counts so, for the minutes Redis is
    down.
    """

    def __init__(
        self,
        max_keys: int = 100_000,
        clock: Callable[[], float] = monotonic,
        *,
        full: Literal["forget", "refuse"] = "forget",
    ) -> None:
        """Keep at most ``max_keys`` callers; ``clock`` reads seconds.

        ``full`` says what a new caller does to a full backend: forget the
        longest-unseen caller, or be refused while that one still owes.
        """
        self.max_keys = max_keys
        self.clock = clock
        self.full = full
        self._callers: OrderedDict[str, _Caller] = OrderedDict()
        self._lock = threading.Lock()

    async def hit(self, key: str, limit: int, period: float) -> Decision:
        """Apply GCRA: allow if the requests owed stay within the burst.

        One lock guards the callers, so event loops on several threads (a
        server's threads, or free-threaded Python) never interleave.
        """
        with self._lock:
            return self._hit(key, limit, period)

    def _hit(self, key: str, limit: int, period: float) -> Decision:
        """``hit``, with the lock held."""
        now = self.clock()
        self._forget_idle(now)
        interval = period / limit
        burst = limit - 1
        # Count what the caller owes in requests, not in seconds of the
        # clock: requests at one reading (a frozen clock) then add whole
        # numbers, and exactly ``limit`` pass. Seconds added to the reading
        # round with its magnitude and with ``period / limit``, which let
        # 8 of 9 a second through, or 1 of 2 near a power of two.
        owed = 0.0
        caller = self._callers.get(key)
        if caller is not None:
            owed = caller.owed
            if caller.interval != interval:  # The key's rate changed.
                owed = owed * caller.interval / interval
            owed = max(owed - (now - caller.seen) / interval, 0.0)
        if owed > burst:
            self._callers.move_to_end(key)
            return Decision(
                allowed=False,
                remaining=0,
                reset_after=owed * interval,
                retry_after=(owed - burst) * interval,
            )
        if (
            caller is None
            and self.full == "refuse"
            and len(self._callers) >= self.max_keys
        ):
            oldest = next(iter(self._callers.values()))
            # _forget_idle has let go of the oldest if it owed nothing.
            wait = oldest.seen + oldest.owed * oldest.interval - now
            if wait > 0:
                return Decision(
                    allowed=False, remaining=0, reset_after=wait, retry_after=wait
                )
        self._callers[key] = _Caller(now, owed + 1, interval)
        self._callers.move_to_end(key)
        if len(self._callers) > self.max_keys:
            del self._callers[next(iter(self._callers))]  # The longest unseen.
        return Decision(
            allowed=True,
            remaining=math.floor(burst - owed),
            reset_after=(owed + 1) * interval,
            retry_after=0.0,
        )

    async def refund(self, key: str, limit: int, period: float) -> None:
        """Give back one request counted for ``key``, as if it never came.

        A caller left owing nothing is forgotten.
        """
        with self._lock:
            caller = self._callers.get(key)
            if caller is None:
                return
            now = self.clock()
            interval = period / limit
            owed = caller.owed
            if caller.interval != interval:  # The key's rate changed.
                owed = owed * caller.interval / interval
            owed = max(owed - (now - caller.seen) / interval, 0.0) - 1
            if owed <= 0:
                del self._callers[key]
            else:
                self._callers[key] = _Caller(now, owed, interval)

    def _forget_idle(self, now: float) -> None:
        """Forget the longest-unseen callers while their quota is full again.

        At most ``EVICTIONS_PER_HIT``, so no one request pays for a crowd.
        """
        for _ in range(EVICTIONS_PER_HIT):
            if not self._callers:
                return
            key, caller = next(iter(self._callers.items()))
            if caller.seen + caller.owed * caller.interval > now:
                return
            del self._callers[key]


FALLBACK_KEYS: Final = 10_000
"""Callers the local fallback of ``RedisBackend`` counts at most (``MemoryBackend``)."""


class RedisBackend:
    """GCRA in Redis or Valkey, shared by every replica (``RATE_LIMIT_CACHE``).

    One Lua script reads and updates the caller's state on the server's
    clock, so replicas never race and their clocks do not matter. Keys live
    in the cache's namespace (``<KEY_PREFIX>:ratelimit:<caller>``) only until
    the caller's quota is full again. If Redis fails, or refuses the script
    (out of memory, a read-only replica after a failover), it is skipped for
    ``FAILURE_COOLDOWN`` seconds, and requests are let through: the limit
    protects the service, and its outage must not stop it.

    With a ``fallback`` (``RATE_LIMIT_FALLBACK_REPLICAS``), they are counted
    by it instead while Redis is down: a backend of this process, at the
    limit divided by ``replicas``, so all the replicas together let about
    the limit through. Its decisions say they were not counted in Redis
    (``Decision.counted`` is False), so ``AUTH.THROTTLE_FAIL_CLOSED`` still
    refuses the attempts it lets through.
    """

    def __init__(
        self,
        cache: str | None = None,
        *,
        fallback: Backend | None = None,
        replicas: int | None = None,
    ) -> None:
        """Count in ``cache`` (default: ``RATE_LIMIT_CACHE``), a Redis cache.

        ``fallback`` counts while Redis is down, at the limit divided by
        ``replicas``. Without one, ``RATE_LIMIT_FALLBACK_REPLICAS`` (unless
        None) makes it a ``MemoryBackend`` of ``FALLBACK_KEYS`` callers that
        refuses new ones while every place owes (``full="refuse"``).

        Raises:
            ImproperlyConfigured: The cache is not Redis.
            ValueError: ``replicas`` is under 1.
        """
        self.cache = cache or settings.RATE_LIMIT_CACHE
        self._skip_until = 0.0
        if caches.config(self.cache).scheme not in REDIS_SCHEMES:
            raise ImproperlyConfigured(
                f"RedisBackend needs a redis:// or rediss:// cache; "
                f"CACHES[{self.cache!r}] is not one."
            )
        configured = settings.RATE_LIMIT_FALLBACK_REPLICAS
        if fallback is None and replicas is None and configured is not None:
            fallback = MemoryBackend(max_keys=FALLBACK_KEYS, full="refuse")
            replicas = configured
        if replicas is not None and replicas < 1:
            raise ValueError(f"replicas is 1 or more, not {replicas}.")
        self.fallback = fallback
        """What counts while Redis is down; None lets requests through."""
        self.replicas = replicas or 1
        """How many replicas share the limit while each counts on its own."""

    async def _fall_back(
        self, key: str, limit: int, period: float, let_through: Decision
    ) -> Decision:
        """The fallback's decision at its share of ``limit``; else ``let_through``."""
        if self.fallback is None:
            return let_through
        decision = await self.fallback.hit(key, max(1, limit // self.replicas), period)
        return dataclasses.replace(decision, counted=False)

    async def hit(self, key: str, limit: int, period: float) -> Decision:
        """Apply GCRA in Redis; let the request through if Redis fails."""
        from domainkit.cache.redis import RedisStore

        cache = caches[self.cache]
        store = cache.store
        if not isinstance(store, RedisStore):  # Settings changed since __init__.
            raise ImproperlyConfigured(f"CACHES[{self.cache!r}] is not Redis.")
        interval = period * 1_000_000 / limit
        tolerance = period * 1_000_000 - interval
        let_through = Decision(
            allowed=True,
            remaining=limit - 1,
            reset_after=0.0,
            retry_after=0.0,
            counted=False,
        )
        if time.monotonic() < self._skip_until:
            return await self._fall_back(key, limit, period, let_through)
        try:
            allowed, debt = await cache.call(
                store.gcra, f"{cache.namespace}ratelimit:{key}", interval, tolerance
            )
        except CacheUnavailable:  # The cache cools down and says so itself.
            return await self._fall_back(key, limit, period, let_through)
        except CacheError as exc:
            cooldown = cache.config.FAILURE_COOLDOWN
            self._skip_until = time.monotonic() + cooldown
            logger.warning(
                "Redis refused the rate limit; %s for %.1fs: %s",
                "letting requests through"
                if self.fallback is None
                else "counting in this process",
                cooldown,
                exc,
            )
            return await self._fall_back(key, limit, period, let_through)
        if not allowed:
            return Decision(
                allowed=False,
                remaining=0,
                reset_after=debt / 1_000_000,
                retry_after=(debt - tolerance) / 1_000_000,
            )
        return Decision(
            allowed=True,
            remaining=math.floor((tolerance - debt) / interval),
            reset_after=(debt + interval) / 1_000_000,
            retry_after=0.0,
        )

    async def refund(self, key: str, limit: int, period: float) -> None:
        """Give back one request counted for ``key``; nothing if Redis fails.

        A refund missed counts a request that should not count, which only
        errs toward the limit. While Redis is skipped, the fallback gives
        it back, if it can.
        """
        from domainkit.cache.redis import RedisStore

        cache = caches[self.cache]
        store = cache.store
        if not isinstance(store, RedisStore):  # Settings changed since __init__.
            raise ImproperlyConfigured(f"CACHES[{self.cache!r}] is not Redis.")
        if time.monotonic() < self._skip_until:
            refund = getattr(self.fallback, "refund", None)
            if refund is not None:
                await refund(key, max(1, limit // self.replicas), period)
            return
        interval = period * 1_000_000 / limit
        with contextlib.suppress(CacheError):
            await cache.call(
                store.gcra_refund, f"{cache.namespace}ratelimit:{key}", interval
            )


def load_backend() -> Backend:
    """A new instance of the ``RATE_LIMIT_BACKEND`` class."""
    try:
        backend = import_string(settings.RATE_LIMIT_BACKEND)
    except ImportError as exc:
        raise ImproperlyConfigured(f"RATE_LIMIT_BACKEND: {exc}") from exc
    instance: Backend = backend()
    return instance


TRY_AGAIN_LATER: Final = 1013
"""WebSocket close code for a handshake refused because of the rate limit."""
TOO_MANY_REQUESTS: Final = "Too many requests; slow down."
"""The detail of every 429 a limit answers with."""
_DENIAL: Final = "websocket.http.response"
"""The ASGI extension that lets a WebSocket handshake be refused with a 429."""
ADDRESS: Final = "domainkit.address"
"""Scope key: the request's client host and ``address_key`` for it, once worked out."""


class RateLimitMiddleware:
    """Refuse callers over ``RATE_LIMIT``; place it after RequestContextMiddleware.

    A WebSocket handshake counts as one request. Over the limit, it is refused
    with the same 429 where the server supports denial responses, and closed
    with 1013 (try again later) where it does not.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Read the rate, the exempt paths, and the backend from settings."""
        self.app = app
        self.rate = parse_rate(settings.RATE_LIMIT) if settings.RATE_LIMIT else None
        self.exempt = frozenset(settings.RATE_LIMIT_EXEMPT_PATHS)
        self.backend = load_backend()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Count the request; answer 429 or add the quota headers."""
        if (
            self.rate is None
            or scope["type"] not in {"http", "websocket"}
            or request_path(scope) in self.exempt
        ):
            await self.app(scope, receive, send)
            return
        limit, period = self.rate
        decision = await self.backend.hit(_caller(scope), limit, period)
        if not decision.allowed:
            await refuse(scope, receive, send, limit, decision)
            return
        headers = [
            (name.lower().encode("latin-1"), value.encode("latin-1"))
            for name, value in _quota(limit, decision).items()
        ]

        async def send_with_headers(message: Message) -> None:
            if message["type"] in {"http.response.start", "websocket.accept"}:
                message["headers"] = set_headers(message.get("headers", ()), headers)
            await send(message)

        await self.app(scope, receive, send_with_headers)


async def refuse(
    scope: Scope, receive: Receive, send: Send, limit: int, decision: Decision
) -> None:
    """Answer a refused caller: 429, or close a WebSocket with 1013 if need be."""
    if scope["type"] == "websocket" and _DENIAL not in scope.get("extensions", {}):
        await send({"type": "websocket.close", "code": TRY_AGAIN_LATER})
        return
    response = error_response(
        status.HTTP_429_TOO_MANY_REQUESTS, TOO_MANY_REQUESTS, code="rate_limited"
    )
    response.headers.update(
        {**_quota(limit, decision), "Retry-After": str(math.ceil(decision.retry_after))}
    )
    await response(scope, receive, send)


def address_key(scope: Scope) -> str:
    """The key a caller without an actor is counted by: its address.

    It is worked out once per request: the scope keeps it (``ADDRESS``)
    with the client host it is for, and it is used again only for that
    host. A key worked out before ``ProxyHeadersMiddleware`` put the real
    client in is not used for the real client.
    """
    client = scope.get("client")
    host = client[0] if client else None
    kept = scope.get(ADDRESS)
    if kept is not None and kept[0] == host:
        known: str = kept[1]
        return known
    key = f"ip:{'unknown' if host is None else client_group(host)}"
    scope[ADDRESS] = (host, key)
    return key


def _caller(scope: Scope) -> str:
    """Identify the caller: the actor if authenticated, else the client address."""
    request = current_request()
    if request is not None and request.actor:
        return f"actor:{request.actor}"
    return address_key(scope)


def _quota(limit: int, decision: Decision) -> dict[str, str]:
    """The ``RateLimit-*`` headers of ``decision``."""
    return {
        "RateLimit-Limit": str(limit),
        "RateLimit-Remaining": str(decision.remaining),
        "RateLimit-Reset": str(math.ceil(decision.reset_after)),
    }


def client_group(host: str) -> str:
    """The address a client is counted by: IPv4 as is, IPv6 by its network.

    IPv4-mapped IPv6 addresses count as their IPv4 address. Hosts that are
    not addresses (a test client's name) are returned unchanged.

    Groups are remembered for the latest ``ADDRESS_CACHE`` hosts, so a
    client seen again is not parsed again (an IPv6 network takes about
    25 µs to work out): by host, and for IPv6 by host and
    ``RATE_LIMIT_IPV6_PREFIX``, which only IPv6 hosts read.
    """
    remembered = len(host) <= MAX_CACHED_ADDRESS
    group = _cached_address_group(host) if remembered else _address_group(host)
    if group is not None:
        return group
    prefix = settings.RATE_LIMIT_IPV6_PREFIX
    return _cached_network(host, prefix) if remembered else _network(host, prefix)


@functools.lru_cache(maxsize=ADDRESS_CACHE)
def _cached_address_group(host: str) -> str | None:
    """``_address_group``, remembered for the latest hosts."""
    return _address_group(host)


@functools.lru_cache(maxsize=ADDRESS_CACHE)
def _cached_network(host: str, prefix: int) -> str:
    """``_network``, remembered for the latest IPv6 hosts and prefixes."""
    return _network(host, prefix)


def _address_group(host: str) -> str | None:
    """``host``'s group if no prefix is needed; None for an IPv6 address."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host
    if isinstance(address, ipaddress.IPv6Address):
        return None if address.ipv4_mapped is None else str(address.ipv4_mapped)
    return str(address)


def _network(host: str, prefix: int) -> str:
    """The network of ``prefix`` bits that IPv6 ``host`` is in."""
    address = ipaddress.IPv6Address(host)
    return str(ipaddress.IPv6Interface((address, prefix)).network)


_SCOPE: Final = re.compile(r"[A-Za-z0-9_.-]{1,128}")
"""A throttle's scope: part of every key it counts under."""

type ThrottleKey = (
    Literal["user", "address"]
    | Callable[[HTTPConnection], str | None]
    | Callable[[HTTPConnection], Awaitable[str | None]]
)
"""Who a throttle counts: the user, the address, or what a function returns."""


class Throttled(APIError):
    """A caller went over a route's ``Throttle`` (429, with ``Retry-After``)."""

    def __init__(self, limit: int, decision: Decision) -> None:
        """Answer 429 ``throttled`` with the quota headers of ``decision``."""
        super().__init__(
            status.HTTP_429_TOO_MANY_REQUESTS,
            TOO_MANY_REQUESTS,
            code="throttled",
            headers={
                **_quota(limit, decision),
                "Retry-After": str(math.ceil(decision.retry_after)),
            },
        )


class Throttle:
    """A FastAPI dependency: at most ``rate`` requests per caller to a route.

    It counts separately from ``RATE_LIMIT``, with the ``RATE_LIMIT_BACKEND``
    (one per application), so a costly route can have a tighter limit than
    the rest::

        export_limit = Throttle("5/hour")

        @router.post("/exports", dependencies=[Require(), Depends(export_limit)])
        async def export(...): ...

    ``key`` says who is counted: ``"user"`` (the actor, or the address of
    anonymous callers), ``"address"`` (the client address, IPv6 per
    network, even for users), or a function of the request, sync or async,
    returning a key (hashed before it is stored) or None for the address.
    ``scope`` names the count: routes with the same scope share it. By
    default each route counts on its own. A refused request gets 429
    ``throttled`` with ``Retry-After``; a refused WebSocket handshake is
    closed with 1013 (try again later).

    A throttle is not an access rule: the route still needs ``Require(...)``
    or ``Public``.
    """

    def __init__(
        self,
        rate: str,
        *,
        key: ThrottleKey = "user",
        scope: str | None = None,
        backend: Backend | None = None,
    ) -> None:
        """Allow ``rate`` (``"10/minute"``) per caller, counted in ``backend``."""
        self.limit, self.period = parse_rate(rate)
        if not callable(key) and key not in {"user", "address"}:
            raise ValueError(
                f"Throttle key {key!r} must be 'user', 'address', or a function."
            )
        if scope is not None and not _SCOPE.fullmatch(scope):
            raise ValueError(
                f"Throttle scope {scope!r} must be 1 to 128 letters, digits, or _.-"
            )
        self.key = key
        self.scope = scope
        self.backend = backend

    async def __call__(self, connection: HTTPConnection) -> None:
        """Count the request; refuse it over the rate.

        Raises:
            Throttled: An HTTP request went over the rate.
            WebSocketException: A WebSocket handshake went over the rate.
        """
        backend = self.backend or _app_backend(connection)
        key = f"throttle:{self.scope or _view(connection)}:"
        key += await self._caller(connection)
        decision = await backend.hit(key, self.limit, self.period)
        if decision.allowed:
            return
        if connection.scope["type"] == "websocket":
            raise WebSocketException(TRY_AGAIN_LATER)
        raise Throttled(self.limit, decision)

    async def _caller(self, connection: HTTPConnection) -> str:
        """The part of the key that names the caller."""
        if isinstance(self.key, str):
            if self.key == "user":
                return _caller(connection.scope)
            return address_key(connection.scope)
        found = self.key(connection)
        if inspect.isawaitable(found):
            found = await found
        if found is None:
            return address_key(connection.scope)
        return f"key:{hashlib.sha256(found.encode()).hexdigest()[:32]}"


_app_backend_lock: Final = threading.Lock()
"""Held to make an application's throttle backend: loops on threads ask at once."""


def _app_backend(connection: HTTPConnection) -> Backend:
    """The application's throttle backend (``RATE_LIMIT_BACKEND``), made once.

    Loops on two threads that ask at once get the same backend, so a
    memory backend counts every request in one place.
    """
    state = connection.app.state
    backend: Backend | None = getattr(state, "throttle_backend", None)
    if backend is None:
        with _app_backend_lock:
            backend = getattr(state, "throttle_backend", None)
            if backend is None:
                backend = state.throttle_backend = load_backend()
    return backend


def _view(connection: HTTPConnection) -> str:
    """The default scope: the import path of the route's endpoint."""
    endpoint = connection.scope.get("endpoint")
    if endpoint is None:
        raise ImproperlyConfigured("Throttle without a scope must guard a route.")
    return f"{endpoint.__module__}.{endpoint.__qualname__}"
