"""Prometheus metrics (``domainkit[metrics]``).

Add ``MetricsMiddleware`` to ``MIDDLEWARE`` and mount ``router`` for
``/metrics``. Requests are labelled by route template, never by raw path, so
ids in URLs cannot explode the number of series. In staging and production
``/metrics`` needs ``Authorization: Bearer <METRICS_TOKEN>``. Run one process
per container (or set up prometheus_client's multiprocess mode).
"""

import hmac
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Final

from fastapi import APIRouter, Request, status
from fastapi.responses import Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from domainkit.conf import settings
from domainkit.conf.global_settings import DEPLOYED_ENVIRONMENTS
from domainkit.db.connections import connections
from domainkit.http.errors import APIError

METHODS: Final = frozenset(
    {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "CONNECT", "TRACE"}
)
"""Methods counted by name; any other is ``OTHER``, so labels stay few."""
REQUESTS: Final = Counter(
    "http_requests_total", "HTTP requests.", ["method", "route", "status"]
)
DURATION: Final = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration.",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
IN_PROGRESS: Final = Gauge(
    "http_requests_in_progress", "HTTP requests being served.", ["method"]
)
DB_POOL: Final = Gauge(
    "db_pool_connections", "Database pool connections.", ["database", "state"]
)
ADMITTED: Final = Gauge(
    "http_requests_admitted",
    "HTTP requests holding one of the MAX_CONCURRENT_REQUESTS slots.",
)
QUEUED: Final = Gauge(
    "http_requests_queued", "HTTP requests waiting for a slot under the cap."
)
SHED: Final = Counter(
    "http_requests_shed_total",
    "HTTP requests refused 503 overloaded: the queue was full, or the wait ran out.",
    ["reason"],
)
DEADLINES: Final = Counter(
    "http_request_deadlines_exceeded_total",
    "HTTP requests answered 503 deadline_exceeded.",
)

_refreshers: list[Callable[[], Awaitable[None]]] = []
logger = logging.getLogger(__name__)


def register_refresher(refresh: Callable[[], Awaitable[None]]) -> None:
    """Run ``refresh`` before each scrape (to update gauges from a database)."""
    if refresh not in _refreshers:
        _refreshers.append(refresh)


def observe_admitted(change: int) -> None:
    """Count requests taking (1) or giving back (-1) a slot under the cap."""
    ADMITTED.inc(change)


def observe_queued(change: int) -> None:
    """Count requests starting (1) or ending (-1) a wait for a slot."""
    QUEUED.inc(change)


def observe_shed(reason: str) -> None:
    """Count one request refused for want of a slot."""
    SHED.labels(reason).inc()


def observe_deadline_exceeded() -> None:
    """Count one request answered 503 because its deadline passed."""
    DEADLINES.inc()


class MetricsMiddleware:
    """Count requests and time them, by method, route template, and status."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app``."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Record one request."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method = scope["method"] if scope["method"] in METHODS else "OTHER"
        status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
        started = time.perf_counter()

        async def send_with_status(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        IN_PROGRESS.labels(method).inc()
        try:
            await self.app(scope, receive, send_with_status)
        finally:
            IN_PROGRESS.labels(method).dec()
            route = route_template(scope)
            REQUESTS.labels(method, route, str(status_code)).inc()
            DURATION.labels(method, route).observe(time.perf_counter() - started)


def route_template(scope: Scope) -> str:
    """Return the matched route's full path template, or ``unmatched``.

    FastAPI keeps nested routers and records the effective, prefixed route in
    ``scope["fastapi"]``; ``scope["route"]`` alone would drop the prefixes.
    """
    fastapi_scope = scope.get("fastapi")
    effective = (
        fastapi_scope.get("effective_route_context")
        if isinstance(fastapi_scope, dict)
        else None
    )
    for path in (
        getattr(effective, "path", None),
        getattr(scope.get("route"), "path", None),
    ):
        if isinstance(path, str):
            return path
    return "unmatched"


router = APIRouter()


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    """Expose metrics in the Prometheus text format."""
    _authorize(request)
    for refresh in _refreshers:
        try:
            await refresh()
        except Exception:
            # One broken gauge must not hide every other metric.
            logger.exception("Metrics refresher %r failed", refresh)
    for alias, stats in connections.pool_stats().items():
        for state, value in stats.items():
            DB_POOL.labels(alias, state).set(value)
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)


def _authorize(request: Request) -> None:
    """Require the bearer token when one is set; deployments must set one."""
    token = settings.METRICS_TOKEN
    if token is None:
        if settings.ENVIRONMENT in DEPLOYED_ENVIRONMENTS:
            raise APIError(
                status.HTTP_403_FORBIDDEN,
                "Set METRICS_TOKEN to expose metrics in staging and production.",
                code="metrics_disabled",
            )
        return
    supplied = request.headers.get("authorization", "")
    expected = f"Bearer {token.get_secret_value()}"
    if not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise APIError(
            status.HTTP_401_UNAUTHORIZED,
            "A valid metrics token is required.",
            code="unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )
