"""Liveness and readiness endpoints; mount ``router`` in the root router.

``/ready`` is reachable by anyone who can reach the service, so it says only
"ready" or "unavailable" unless ``READINESS_DETAILS`` is on; failing checks
are always logged. Results are reused for ``READINESS_CACHE_SECONDS`` from
when their round of checks ended, and probes that come during a round wait
for it, so a flood of probes costs at most one round of checks per interval.

A round checks domains, databases, ledgers, and caches at once, each check
within ``READINESS_TIMEOUT``, so a round takes about that long at worst. It
runs as a background task in a clean context (``domainkit.tasks.spawn``):
it serves every waiting probe, so it carries none's request. A group of
checks that raises instead of reporting (a bug in a domain's ``check``)
counts as failing, under its group's name (``GROUPS``), and is logged:
the round is still kept, so probes get a 503, never a 500 each.
"""

import asyncio
import logging
import time
from typing import Final

from fastapi import APIRouter, Request, status
from starlette.datastructures import State

from domainkit.apps import registry
from domainkit.cache import caches
from domainkit.conf import settings
from domainkit.db.connections import connections
from domainkit.http.responses import BuiltJSONResponse, JSONResponse
from domainkit.ledger import ledgers
from domainkit.tasks import spawn

READINESS_TASK: Final = "domainkit.readiness"
"""The name of a round of readiness checks' task."""
GROUPS: Final = ("domains", "databases", "ledgers", "caches")
"""What a round checks, in order: how a group that raised is named as failing."""

logger = logging.getLogger(__name__)

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
        return BuiltJSONResponse({"status": "ready"})
    body: dict[str, object] = {"status": "unavailable"}
    if settings.READINESS_DETAILS:
        body["failing"] = failing
    return BuiltJSONResponse(body, status_code=status.HTTP_503_SERVICE_UNAVAILABLE)


async def _failing_checks(request: Request) -> list[str]:
    """Reuse this application's recent result, or share one round of checks."""
    state = request.app.state
    cached: tuple[float, list[str]] | None = getattr(state, "readiness", None)
    if (
        cached is not None
        and time.monotonic() - cached[0] < settings.READINESS_CACHE_SECONDS
    ):
        return cached[1]
    running: asyncio.Future[list[str]] | None = getattr(state, "readiness_round", None)
    if (
        running is None
        or running.done()
        or running.get_loop() is not asyncio.get_running_loop()
    ):
        running = state.readiness_round = spawn(
            _check(state), name=READINESS_TASK, log_errors=False
        )
    # A probe that gives up does not cancel the round the others wait for.
    return await asyncio.shield(running)


async def _check(state: State) -> list[str]:
    """Run every check; keep the result, timed from when the round ended."""
    seconds = settings.READINESS_TIMEOUT
    groups = await asyncio.gather(  # Each bounded: wait for all, leave none running.
        registry.check(seconds),
        connections.check_all(seconds),
        ledgers.check_all(seconds),
        caches.check_all(seconds),
        return_exceptions=True,
    )
    failing: list[str] = []
    for name, group in zip(GROUPS, groups, strict=True):
        if isinstance(group, Exception):
            logger.error("Readiness checks of %s raised", name, exc_info=group)
            failing.append(name)
        elif isinstance(group, BaseException):
            raise group
        else:
            failing.extend(group)
    state.readiness = (time.monotonic(), failing)
    return failing
