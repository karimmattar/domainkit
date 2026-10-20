"""Shutting a process down, in phases, within one budget (``SHUTDOWN_TIMEOUT``).

The application's lifespan (``serving``) and management commands
(``close_resources``) stop in this order:

1. ``domains``: each started domain's ``DomainConfig.shutdown``, in reverse
   order (the application only).
2. ``drain``: closers registered to finish work in flight, such as a
   publisher sending the messages it holds.
3. ``tasks``: the loop's background tasks (``domainkit.tasks.spawn`` and
   ``supervise``) are cancelled and awaited.
4. ``resources``: caches, databases, ledgers, brokers, and the kinds others
   register (an HTTP client) close, all at once.
5. ``flush``: closers registered to run last, such as logs.

Each phase may run until its share of the budget has passed, counted from
the start of the shutdown (``PHASE_ENDS``: 30%, 50%, 60%, 90%, then 100%),
so time one phase does not use goes to the next, and every phase keeps its
own share whatever the ones before it did. Inside a phase, a closer that
fails or overruns is logged and the others still close: one hung item never
keeps the rest open. Domain hooks run one after another, each within a fair
share of what is left of their phase.

Other parts of the framework add their closers with ``register``::

    lifecycle.register("http clients", clients.close_all)
    lifecycle.register("publishers", close_publishers, phase=Phase.DRAIN)
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Self

from domainkit import tasks
from domainkit.apps import registry
from domainkit.broker import brokers
from domainkit.cache import caches
from domainkit.conf import settings
from domainkit.conf.global_settings import Settings
from domainkit.db.connections import connections
from domainkit.exceptions import ImproperlyConfigured
from domainkit.ledger import ledgers

logger = logging.getLogger(__name__)

type Closer = Callable[[], Awaitable[object]]
"""Closes one kind of thing for the running loop (``caches.close_all``)."""


class Phase(StrEnum):
    """The steps of a shutdown, in order."""

    DOMAINS = "domains"
    DRAIN = "drain"
    TASKS = "tasks"
    RESOURCES = "resources"
    FLUSH = "flush"


PHASE_ENDS: Final[Mapping[Phase, float]] = {
    Phase.DOMAINS: 0.3,
    Phase.DRAIN: 0.5,
    Phase.TASKS: 0.6,
    Phase.RESOURCES: 0.9,
    Phase.FLUSH: 1.0,
}
"""The share of the budget, from the start of the shutdown, each phase ends at."""
REGISTERED_PHASES: Final = frozenset({Phase.DRAIN, Phase.RESOURCES, Phase.FLUSH})
"""The phases ``register`` adds closers to."""
DEFAULT_SHUTDOWN_TIMEOUT: Final[float] = Settings.model_fields[
    "SHUTDOWN_TIMEOUT"
].default
"""The budget when no settings are loaded."""

_registered: Final[dict[Phase, dict[str, Closer]]] = {
    phase: {} for phase in REGISTERED_PHASES
}


def _builtin() -> dict[str, Closer]:
    """The framework's own kinds of resources, looked up when shutting down."""
    return {
        "caches": caches.close_all,
        "databases": connections.close_all,
        "ledgers": ledgers.close_all,
        "brokers": brokers.close_all,
    }


def register(name: str, close: Closer, *, phase: Phase = Phase.RESOURCES) -> None:
    """Call ``close`` for the running loop when it shuts down, in ``phase``.

    ``name`` labels log lines; registering a name again replaces its closer.

    Raises:
        ValueError: ``phase`` is not ``drain``, ``resources``, or ``flush``,
            or ``name`` is one of the framework's own kinds.
    """
    if phase not in REGISTERED_PHASES:
        raise ValueError(
            f"Closers go in the drain, resources, or flush phase, not {phase}."
        )
    if name in _builtin():
        raise ValueError(f"{name!r} is closed by the framework already.")
    unregister(name)
    _registered[phase][name] = close


def unregister(name: str) -> None:
    """Stop closing ``name`` at shutdown; an unknown name is ignored."""
    for closers in _registered.values():
        closers.pop(name, None)


def shutdown_timeout() -> float:
    """``SHUTDOWN_TIMEOUT``, or its default where no settings are loaded."""
    try:
        timeout: float = settings.SHUTDOWN_TIMEOUT
    except ImproperlyConfigured:
        return DEFAULT_SHUTDOWN_TIMEOUT
    return timeout


@dataclass(frozen=True, slots=True)
class Budget:
    """A shutdown's time: when it began, on the loop's clock, and for how long."""

    started: float
    seconds: float

    @classmethod
    def begin(cls, seconds: float | None = None) -> Self:
        """Start counting now; ``seconds`` defaults to ``SHUTDOWN_TIMEOUT``."""
        loop = asyncio.get_running_loop()
        return cls(loop.time(), shutdown_timeout() if seconds is None else seconds)

    def end(self, phase: Phase) -> float:
        """When ``phase`` must be over, on the loop's clock (``loop.time()``)."""
        return self.started + self.seconds * PHASE_ENDS[phase]


@asynccontextmanager
async def serving() -> AsyncIterator[None]:
    """Start the installed domains; at the end, shut everything down in budget.

    The application's lifespan runs inside it. A domain that fails to start
    stops the ones started before it, and the rest of the shutdown still runs.
    """
    budget: Budget | None = None
    domains = shutdown_timeout() * PHASE_ENDS[Phase.DOMAINS]
    try:
        async with registry.lifespan(stop_within=domains):
            try:
                yield
            finally:
                budget = Budget.begin()
    finally:
        await close_resources(budget)


async def close_resources(budget: Budget | None = None) -> None:
    """Finish the running loop's work and close what it opened, in budget.

    Runs the ``drain``, ``tasks``, ``resources``, and ``flush`` phases of
    ``budget`` (default: one that begins now). Nothing here raises: every
    failure is logged, and every closer is tried.
    """
    budget = budget or Budget.begin()
    await _close_all(_registered[Phase.DRAIN], budget.end(Phase.DRAIN))
    if stuck := await tasks.shutdown(budget.end(Phase.TASKS)):
        logger.error(
            "Background tasks still running at the end of their shutdown phase: %s",
            ", ".join(stuck),
        )
    resources = _builtin() | _registered[Phase.RESOURCES]
    await _close_all(resources, budget.end(Phase.RESOURCES))
    await _close_all(_registered[Phase.FLUSH], budget.end(Phase.FLUSH))


async def _close_all(closers: Mapping[str, Closer], deadline: float) -> None:
    """Run ``closers`` at once until ``deadline``; log what failed or overran.

    Closers still running at the deadline are cancelled and left: one that
    ignores cancellation cannot hold the shutdown.
    """
    if not closers:
        return

    async def close(closer: Closer) -> None:
        await closer()

    running = {
        name: asyncio.ensure_future(close(closer)) for name, closer in closers.items()
    }
    loop = asyncio.get_running_loop()
    await asyncio.wait(running.values(), timeout=max(deadline - loop.time(), 0))
    for name, task in running.items():
        if not task.done():
            task.cancel()
            logger.error(
                "Could not close the %s within the shutdown budget; closing the rest",
                name,
            )
        elif not task.cancelled() and (exc := task.exception()) is not None:
            logger.error("Could not close the %s; closing the rest", name, exc_info=exc)
