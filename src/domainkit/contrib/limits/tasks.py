"""The purge of old usage on a schedule (needs ``domainkit.contrib.messaging``)."""

from typing import Final

from domainkit.contrib.messaging import periodic

from .settings import LimitsSettings
from .usage import purge_usage

PURGE_USAGE: Final = "domainkit.limits.purge_usage"
"""The periodic task that deletes usage older than ``LIMITS.KEEP_USAGE``."""


async def purge_old_usage() -> None:
    """Delete usage no window reaches any more (the periodic task ``PURGE_USAGE``)."""
    await purge_usage()


def register(config: LimitsSettings) -> None:
    """Purge old usage every ``LIMITS.PURGE_EVERY``."""
    periodic(config.PURGE_EVERY, name=PURGE_USAGE)(purge_old_usage)
