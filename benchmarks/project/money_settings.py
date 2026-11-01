"""What the money rungs install and set, beside the catalog's project.

Read without importing the domains, so ``rung`` and ``database`` can name
them before settings are configured (PERF-32).
"""

from typing import Any, Final

DOMAINS: Final = (
    "domainkit.contrib.keys",
    "domainkit.contrib.accounting",
    "domainkit.contrib.limits",
    "benchmarks.project.vault",
)
"""Installed after the catalog's domains, on the money rungs only."""
SETTINGS: Final[dict[str, Any]] = {
    # The local key ring derives its data key from SECRET_KEY: a bench's own.
    "SECRET_KEY": "domainkit-bench-only-key-that-signs-nothing-anyone-keeps",
    "KEYS": {"PROVIDER": "local"},
    "LIMITS": {
        "DEFAULTS": [
            {
                "code": "bench.daily",
                "kind": "holder",
                "direction": "in",
                "currency": "USD",
                "window": "day",
                "max_amount": "99999999999999.00",
            }
        ]
    },
}
"""Their settings: a development key ring, and a daily limit on the holder."""
