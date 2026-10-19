"""Outbox and worker metrics, when ``prometheus_client`` is installed."""

from typing import Final

from prometheus_client import Counter, Gauge, Histogram

from domainkit.apps import registry
from domainkit.http.metrics import register_refresher

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


async def refresh_outbox() -> None:
    """Update the outbox gauges before a scrape (while messaging is installed).

    They describe the home outbox (``MESSAGING.DATABASE``).
    """
    if not registry.is_installed("domainkit.contrib.messaging"):
        return
    config = registry.settings(MessagingSettings)
    counts = await queue_counts(config)
    for status in STATUSES:
        QUEUED.labels(status).set(counts.get(status, 0))
    lag = await queue_lag(config)
    for queue in queues() | lag.keys():
        LAG.labels(queue).set(lag.get(queue, 0.0))


register_refresher(refresh_outbox)
