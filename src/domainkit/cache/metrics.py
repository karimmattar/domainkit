"""Cache metrics, when ``prometheus_client`` is installed."""

from typing import Final

from prometheus_client import Counter

LOOKUPS: Final = Counter(
    "cache_lookups_total",
    "Cache lookups: hit, miss, invalid (unfit for its type), early (XFetch).",
    ["cache", "result"],
)
ERRORS: Final = Counter(
    "cache_errors_total", "Cache operations that failed or were refused.", ["cache"]
)


def observe(cache: str, result: str) -> None:
    """Count one lookup of ``cache``."""
    LOOKUPS.labels(cache, result).inc()


def observe_error(cache: str) -> None:
    """Count one failed operation of ``cache``."""
    ERRORS.labels(cache).inc()
