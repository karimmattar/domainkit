"""Outbox and worker metrics, when ``prometheus_client`` is installed."""

from typing import Final

from prometheus_client import Counter, Gauge, Histogram

from domainkit.apps import registry
from domainkit.http.metrics import register_refresher

from .admin import queue_counts
from .settings import MessagingSettings
from .tables import DEAD, DONE, PENDING

PROCESSED: Final = Counter(
    "messaging_messages_total", "Messages run by workers.", ["kind", "outcome"]
)
DURATION: Final = Histogram(
    "messaging_message_duration_seconds", "Time spent running a message.", ["kind"]
)
QUEUED: Final = Gauge("messaging_outbox_messages", "Outbox messages.", ["status"])


def observe(kind: str, outcome: str, seconds: float) -> None:
    """Record one processed message."""
    PROCESSED.labels(kind, outcome).inc()
    DURATION.labels(kind).observe(seconds)


async def refresh_outbox() -> None:
    """Update the outbox gauges before a scrape (while messaging is installed)."""
    if not registry.is_installed("domainkit.contrib.messaging"):
        return
    counts = await queue_counts(registry.settings(MessagingSettings))
    for status in (PENDING, DONE, DEAD):
        QUEUED.labels(status).set(counts.get(status, 0))


register_refresher(refresh_outbox)
