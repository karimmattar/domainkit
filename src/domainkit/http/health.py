"""Liveness and readiness endpoints; mount ``router`` in the root router.

``/ready`` is reachable by anyone who can reach the service, so it says only
"ready" or "unavailable" unless ``READINESS_DETAILS`` is on; failing checks
are always logged. Results are reused for ``READINESS_CACHE_SECONDS``, so a
flood of probes costs at most one round of checks per interval.
"""

import time

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from domainkit.apps import registry
from domainkit.cache import caches
from domainkit.conf import settings
from domainkit.db.connections import connections
from domainkit.ledger import ledgers

router = APIRouter(tags=["health"])


@router.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    """Report that the process is up (liveness probe); checks nothing else."""
    return {"status": "ok"}


@router.get("/ready", include_in_schema=False)
async def ready(request: Request) -> JSONResponse:
    """Report whether every domain and database can serve traffic."""
    failing = await _failing_checks(request)
    if not failing:
        return JSONResponse({"status": "ready"})
    body: dict[str, object] = {"status": "unavailable"}
    if settings.READINESS_DETAILS:
        body["failing"] = failing
    return JSONResponse(body, status_code=status.HTTP_503_SERVICE_UNAVAILABLE)


async def _failing_checks(request: Request) -> list[str]:
    """Run the checks, or reuse this application's recent result."""
    state = request.app.state
    cached: tuple[float, list[str]] | None = getattr(state, "readiness", None)
    now = time.monotonic()
    if cached is not None and now - cached[0] < settings.READINESS_CACHE_SECONDS:
        return cached[1]
    seconds = settings.READINESS_TIMEOUT
    failing = [
        *await registry.check(seconds),
        *await connections.check_all(seconds),
        *await ledgers.check_all(seconds),
        *await caches.check_all(seconds),
    ]
    state.readiness = (now, failing)
    return failing
