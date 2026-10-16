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
"""

import ipaddress
import logging
import math
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from fastapi import status
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from domainkit.cache import CacheUnavailable, caches
from domainkit.conf import settings
from domainkit.conf.global_settings import REDIS_SCHEMES
from domainkit.context import current_request
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.errors import error_response
from domainkit.utils.module_loading import import_string
from domainkit.utils.rates import parse_rate

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


class Backend(Protocol):
    """Where rate-limit state lives."""

    async def hit(self, key: str, limit: int, period: float) -> Decision:
        """Count one request for ``key`` and decide whether to allow it."""
        ...


class MemoryBackend:
    """GCRA state in this process, for at most ``max_keys`` callers.

    Callers are kept in the order they were last seen. Each request forgets
    the longest-unseen callers whose quota is full again, which changes no
    decision. Past ``max_keys``, the longest-unseen caller is forgotten
    anyway and starts afresh. Memory and the cost of a request stay bounded
    however many callers there are.
    """

    def __init__(
        self, max_keys: int = 100_000, clock: Callable[[], float] = time.monotonic
    ) -> None:
        """Keep at most ``max_keys`` callers; ``clock`` reads seconds."""
        self.max_keys = max_keys
        self.clock = clock
        self._arrival: OrderedDict[str, float] = OrderedDict()

    async def hit(self, key: str, limit: int, period: float) -> Decision:
        """Apply GCRA: allow if the theoretical arrival time is within the burst."""
        now = self.clock()
        self._forget_idle(now)
        interval = period / limit
        tolerance = period - interval
        arrival = max(self._arrival.get(key, now), now)
        # Compare the time already spent, not now with now + interval - period:
        # that sum rounds up whenever now + interval crosses a power of two,
        # which refused new callers at a limit of 1.
        debt = arrival - now
        if debt > tolerance:
            self._arrival.move_to_end(key)
            return Decision(
                allowed=False,
                remaining=0,
                reset_after=debt,
                retry_after=debt - tolerance,
            )
        self._arrival[key] = arrival + interval
        self._arrival.move_to_end(key)
        if len(self._arrival) > self.max_keys:
            del self._arrival[next(iter(self._arrival))]  # The longest unseen.
        return Decision(
            allowed=True,
            remaining=math.floor((tolerance - debt) / interval),
            reset_after=debt + interval,
            retry_after=0.0,
        )

    def _forget_idle(self, now: float) -> None:
        """Forget the longest-unseen callers while their quota is full again."""
        while self._arrival:
            key, arrival = next(iter(self._arrival.items()))
            if arrival > now:
                return
            del self._arrival[key]


class RedisBackend:
    """GCRA in Redis or Valkey, shared by every replica (``RATE_LIMIT_CACHE``).

    One Lua script reads and updates the caller's state on the server's
    clock, so replicas never race and their clocks do not matter. Keys live
    in the cache's namespace (``<KEY_PREFIX>:ratelimit:<caller>``) only until
    the caller's quota is full again. If Redis fails, requests are let
    through: the limit protects the service, and its outage must not stop
    it. The cache is then skipped for ``FAILURE_COOLDOWN`` seconds.
    """

    def __init__(self, cache: str | None = None) -> None:
        """Count in ``cache`` (default: ``RATE_LIMIT_CACHE``), a Redis cache."""
        self.cache = cache or settings.RATE_LIMIT_CACHE
        if caches.config(self.cache).scheme not in REDIS_SCHEMES:
            raise ImproperlyConfigured(
                f"RedisBackend needs a redis:// or rediss:// cache; "
                f"CACHES[{self.cache!r}] is not one."
            )

    async def hit(self, key: str, limit: int, period: float) -> Decision:
        """Apply GCRA in Redis; let the request through if Redis fails."""
        from domainkit.cache.redis import RedisStore

        cache = caches[self.cache]
        store = cache.store
        if not isinstance(store, RedisStore):  # Settings changed since __init__.
            raise ImproperlyConfigured(f"CACHES[{self.cache!r}] is not Redis.")
        interval = period * 1_000_000 / limit
        tolerance = period * 1_000_000 - interval
        try:
            allowed, debt = await cache.call(
                store.gcra, f"{cache.namespace}ratelimit:{key}", interval, tolerance
            )
        except CacheUnavailable:
            return Decision(
                allowed=True, remaining=limit - 1, reset_after=0.0, retry_after=0.0
            )
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


class RateLimitMiddleware:
    """Refuse callers over ``RATE_LIMIT``; place it after RequestContextMiddleware."""

    def __init__(self, app: ASGIApp) -> None:
        """Read the rate, the exempt paths, and the backend from settings."""
        self.app = app
        self.rate = parse_rate(settings.RATE_LIMIT) if settings.RATE_LIMIT else None
        self.exempt = frozenset(settings.RATE_LIMIT_EXEMPT_PATHS)
        try:
            backend = import_string(settings.RATE_LIMIT_BACKEND)
        except ImportError as exc:
            raise ImproperlyConfigured(f"RATE_LIMIT_BACKEND: {exc}") from exc
        self.backend: Backend = backend()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Count the request; answer 429 or add the quota headers."""
        if self.rate is None or scope["type"] != "http" or scope["path"] in self.exempt:
            await self.app(scope, receive, send)
            return
        limit, period = self.rate
        decision = await self.backend.hit(_caller(scope), limit, period)
        headers = {
            "RateLimit-Limit": str(limit),
            "RateLimit-Remaining": str(decision.remaining),
            "RateLimit-Reset": str(math.ceil(decision.reset_after)),
        }
        if not decision.allowed:
            response = error_response(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Too many requests; slow down.",
                code="rate_limited",
            )
            response.headers.update(
                {**headers, "Retry-After": str(math.ceil(decision.retry_after))}
            )
            await response(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message).update(headers)
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _caller(scope: Scope) -> str:
    """Identify the caller: the actor if authenticated, else the client address."""
    request = current_request()
    if request is not None and request.actor:
        return f"actor:{request.actor}"
    client = scope.get("client")
    return f"ip:{client_group(client[0]) if client else 'unknown'}"


def client_group(host: str) -> str:
    """The address a client is counted by: IPv4 as is, IPv6 by its network.

    IPv4-mapped IPv6 addresses count as their IPv4 address. Hosts that are
    not addresses (a test client's name) are returned unchanged.
    """
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        prefix = settings.RATE_LIMIT_IPV6_PREFIX
        return str(ipaddress.IPv6Interface((address, prefix)).network)
    return str(address)
