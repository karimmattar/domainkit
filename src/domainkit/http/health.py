"""Liveness and readiness endpoints; mount ``router`` in the root router.

``/ready`` is reachable by anyone who can reach the service, so it says only
"ready" or "unavailable" unless ``READINESS_DETAILS`` is on; failing checks
are always logged. Results are reused for ``READINESS_CACHE_SECONDS`` from
when their round of checks ended, and probes that come during a round wait
for it, so a flood of probes costs at most one round of checks per interval.

A round checks domains, databases, ledgers, and caches at once (and each
broker, with ``READINESS_BROKERS``), each check within ``READINESS_TIMEOUT``,
so a round takes about that long at worst. It runs as a background task in
a clean context (``domainkit.tasks.spawn``): it serves every waiting probe,
so it carries none's request. A group of checks that raises instead of
reporting (a bug in a domain's ``check``) counts as failing, under its
group's name (``GROUPS``), and is logged: the round is still kept, so
probes get a 503, never a 500 each.
"""

import asyncio
import logging
import time
from collections.abc import Iterable
from typing import Final

from fastapi import APIRouter, Request, status
from starlette.datastructures import State

from domainkit.apps import registry
from domainkit.cache import caches
from domainkit.conf import settings
from domainkit.db.connections import connections
from domainkit.http.responses import BuiltJSONResponse, JSONResponse
from domainkit.tasks import spawn

__all__ = ["broker_failures", "readiness_failures", "router"]

READINESS_TASK: Final = "domainkit.readiness"
"""The name of a round of readiness checks' task."""
GROUPS: Final = ("domains", "databases", "ledgers", "caches", "brokers")
"""What a round checks, in order: how a group that raised is named as failing."""

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])
"""``GET /health`` and ``GET /ready``, which guard themselves; mount it at the root."""


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


async def _check_ledgers(seconds: float) -> list[str]:
    """Check the ledgers, if any is configured.

    Without one, ``domainkit.ledger`` is never imported (PERF-15).
    """
    if not settings.LEDGERS:
        return []
    from domainkit.ledger import ledgers

    return await ledgers.check_all(seconds)


async def _check(state: State) -> list[str]:
    """Run every check; keep the result, timed from when the round ended."""
    failing = await readiness_failures()
    state.readiness = (time.monotonic(), failing)
    return failing


async def readiness_failures(seconds: float | None = None) -> list[str]:
    """What ``/ready`` finds failing: domains, databases, ledgers, caches, brokers.

    Each check is bounded by ``seconds`` (``READINESS_TIMEOUT``), and all
    run at once. A group that raises counts as failing under its name
    (``GROUPS``), and is logged. For a process that serves no ``/ready``
    of its own, such as the worker (``runworker --heartbeat-file``).
    """
    within = settings.READINESS_TIMEOUT if seconds is None else seconds
    brokers = settings.BROKERS if settings.READINESS_BROKERS else {}
    groups = await asyncio.gather(  # Each bounded: wait for all, leave none running.
        registry.check(within),
        connections.check_all(within),
        _check_ledgers(within),
        caches.check_all(within),
        broker_failures(brokers, within),
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
    return failing


async def broker_failures(aliases: Iterable[str], seconds: float) -> list[str]:
    """Each broker of ``aliases`` that does not answer within ``seconds``.

    Named ``broker:<alias>``, and logged. On RabbitMQ a check opens and
    closes a channel: a round trip.
    """
    names = list(aliases)
    if not names:  # The served application loads no broker (PERF-15).
        return []
    from domainkit.broker import BrokerError, brokers

    async def check(alias: str) -> str | None:
        try:
            async with asyncio.timeout(seconds):
                await brokers[alias].check()
        except (BrokerError, TimeoutError, OSError) as exc:
            logger.warning(
                "Broker %r is not ready: %s", alias, exc or type(exc).__name__
            )
            return f"broker:{alias}"
        return None

    found = await asyncio.gather(*(check(alias) for alias in names))
    return [each for each in found if each is not None]
