"""Request rate limiting (``RATE_LIMIT = "300/minute"``).

Each caller, the authenticated actor or else the client address, has a
token bucket: up to ``limit`` requests at once, refilled smoothly at ``limit``
per period. It is implemented with GCRA (the generic cell rate algorithm),
which keeps one timestamp per caller. Between any two moments a caller gets
at most ``limit`` plus the refill for the time between them, so a window of
one period can see up to about twice ``limit`` after an idle stretch.
Responses carry ``RateLimit-Limit``, ``RateLimit-Remaining``, and
``RateLimit-Reset``; refused ones get 429 with ``Retry-After``.

The default backend lives in process memory, so each replica counts on its
own. Behind several replicas, enforce limits at the gateway or point
``RATE_LIMIT_BACKEND`` at a shared store (Redis) implementing ``Backend``.
"""

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from fastapi import status
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from domainkit.conf import settings
from domainkit.context import current_request
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.errors import error_response
from domainkit.utils.module_loading import import_string
from domainkit.utils.rates import parse_rate


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
    """GCRA state in this process; stale callers are purged as it grows."""

    def __init__(
        self, max_keys: int = 100_000, clock: Callable[[], float] = time.monotonic
    ) -> None:
        """Keep at most about ``max_keys`` callers; ``clock`` reads seconds."""
        self.max_keys = max_keys
        self.clock = clock
        self._arrival: dict[str, float] = {}

    async def hit(self, key: str, limit: int, period: float) -> Decision:
        """Apply GCRA: allow if the theoretical arrival time is within the burst."""
        now = self.clock()
        interval = period / limit
        arrival = max(self._arrival.get(key, now), now)
        allowed_at = arrival + interval - period
        if now < allowed_at:
            return Decision(
                allowed=False,
                remaining=0,
                reset_after=arrival - now,
                retry_after=allowed_at - now,
            )
        self._arrival[key] = arrival + interval
        if len(self._arrival) > self.max_keys:
            self._purge(now)
        return Decision(
            allowed=True,
            remaining=math.floor((period - (arrival + interval - now)) / interval),
            reset_after=arrival + interval - now,
            retry_after=0.0,
        )

    def _purge(self, now: float) -> None:
        """Forget callers whose quota is already full again."""
        self._arrival = {k: v for k, v in self._arrival.items() if v > now}


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
    return f"ip:{client[0] if client else 'unknown'}"
