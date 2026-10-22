"""Metrics of ``domainkit.contrib.auth``, when ``prometheus_client`` is installed."""

from typing import Final

from prometheus_client import Counter

PRINCIPALS: Final = Counter(
    "auth_principal_cache_total",
    "Principal cache events: hit, miss, invalidation, eviction.",
    ["authentication", "event"],
)


def observe_principals(authentication: str, event: str, count: int = 1) -> None:
    """Count ``count`` principal cache events of one authentication class."""
    PRINCIPALS.labels(authentication, event).inc(count)
