"""Parse rates such as ``"100/minute"``."""

import re
from typing import Final

PERIODS: Final = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}
_RATE: Final = re.compile(r"\s*(\d+)\s*/\s*(second|minute|hour|day)\s*")


def parse_rate(rate: str) -> tuple[int, int]:
    """Return ``(count, period in seconds)``; raise ValueError when malformed."""
    match = _RATE.fullmatch(rate)
    if match is None or int(match[1]) < 1:
        raise ValueError(
            f"rate {rate!r} must look like '100/minute' (second, minute, hour, day)"
        )
    return int(match[1]), PERIODS[match[2]]
