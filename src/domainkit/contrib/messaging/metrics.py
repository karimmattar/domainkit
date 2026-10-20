"""Outbox and worker metrics, when ``prometheus_client`` is installed.

The outbox gauges count the table, which grows with retention: they are
counted at most once per ``METRICS_TTL`` per process, and scrapes that
arrive while a count runs share it instead of starting their own.
"""

import asyncio
from typing import Final
from weakref import WeakKeyDictionary

from prometheus_client import Counter, Gauge, Histogram
from sqlalchemy.ext.asyncio import AsyncEngine

from domainkit import clock
from domainkit.apps import registry
from domainkit.db import connections
from domainkit.http.metrics import register_refresher
from domainkit.tasks import spawn

from .admin import queue_counts, queue_lag
from .registry import queues
from .settings import MessagingSettings
from .tables import STATUSES

PROCESSED: Final = Counter(
    "messaging_messages_total", "Messages run by workers.", ["kind", "outcome"]
)
DURATION: Final = Histogram(
    "messaging_message_duration_seconds", "Time spent running a message.", ["kind"]
)
QUEUED: Final = Gauge("messaging_outbox_messages", "Outbox messages.", ["status"])
LAG: Final = Gauge(
    "messaging_outbox_lag_seconds",
    "Seconds the oldest due message has waited for a worker (0: none waits).",
    ["queue"],
)
RELAYED: Final = Counter(
    "messaging_relayed_total",
    "Messages the relay handed to the broker (published) or could not (failed).",
    ["outcome"],
)
SKIPPED: Final = Counter(
    "messaging_deliveries_skipped_total",
    "Broker deliveries not run: duplicates, or messages already done or running.",
)
BREAKER: Final = Gauge(
    "messaging_publisher_breaker_open",
    "1 while publishing on commit is stopped because the broker keeps failing.",
    ["broker"],
)
LEFT: Final = Counter(
    "messaging_left_to_relay_total",
    "Committed messages the publisher left to the relay, by reason.",
    ["reason"],
)


def observe(kind: str, outcome: str, seconds: float) -> None:
    """Record one processed message."""
    PROCESSED.labels(kind, outcome).inc()
    DURATION.labels(kind).observe(seconds)


def observe_relay(outcome: str, count: int = 1) -> None:
    """Record messages the relay published or could not publish."""
    RELAYED.labels(outcome).inc(count)


def observe_skipped() -> None:
    """Record a delivery that was not run."""
    SKIPPED.inc()


def observe_breaker(broker: str, *, is_open: bool) -> None:
    """Record whether the publishing breaker of ``broker`` is open."""
    BREAKER.labels(broker).set(1 if is_open else 0)


def observe_left(reason: str, count: int) -> None:
    """Record committed messages left to the relay (``breaker``, ``backlog``...)."""
    LEFT.labels(reason).inc(count)


type _Gauges = tuple[dict[str, int], dict[str, float]]
"""Messages by status, and the lag of each queue."""

_counted: WeakKeyDictionary[AsyncEngine, tuple[float, _Gauges]] = WeakKeyDictionary()
"""Each outbox's last count, and until when it may be reused (monotonic)."""
_counting: WeakKeyDictionary[AsyncEngine, asyncio.Task[_Gauges]] = WeakKeyDictionary()
"""The count under way for each outbox, shared by the scrapes waiting on it."""


async def outbox_gauges(config: MessagingSettings) -> _Gauges:
    """Messages by status and each queue's lag in the home outbox, recently counted.

    A count is reused for ``METRICS_TTL`` seconds. Callers that arrive
    while one runs wait for it; one that gives up (a scrape cancelled)
    does not stop it for the others. A failed count is not kept.
    """
    engine = connections.engine(config.DATABASE)
    cached = _counted.get(engine)
    if cached is not None and clock.monotonic() < cached[0]:
        return cached[1]
    counting = _counting.get(engine)
    if counting is None:
        expires = clock.monotonic() + config.METRICS_TTL
        counting = spawn(
            _count(config, engine, expires),
            name="messaging.outbox_gauges",
            log_errors=False,  # The scrapes waiting on it get the error.
        )
        _counting[engine] = counting
        counting.add_done_callback(lambda done: _counted_up(engine, done))
    # Not shield(): a count that fails after this caller gave up is the other
    # callers' to handle, and shield() would log it as well.
    await asyncio.wait({counting})
    return counting.result()


def _counted_up(engine: AsyncEngine, counting: asyncio.Task[_Gauges]) -> None:
    """Forget the count under way; its error is for the callers that waited."""
    _counting.pop(engine, None)
    if not counting.cancelled():
        counting.exception()  # Retrieved: no caller may be left to take it.


async def _count(
    config: MessagingSettings, engine: AsyncEngine, expires: float
) -> _Gauges:
    """Count the outbox, and keep the count until ``expires``."""
    gauges = (await queue_counts(config), await queue_lag(config))
    _counted[engine] = (expires, gauges)
    return gauges


async def refresh_outbox() -> None:
    """Update the outbox gauges before a scrape (while messaging is installed).

    They describe the home outbox (``MESSAGING.DATABASE``).
    """
    if not registry.is_installed("domainkit.contrib.messaging"):
        return
    counts, lag = await outbox_gauges(registry.settings(MessagingSettings))
    for status in STATUSES:
        QUEUED.labels(status).set(counts.get(status, 0))
    for queue in queues() | lag.keys():
        LAG.labels(queue).set(lag.get(queue, 0.0))


register_refresher(refresh_outbox)
