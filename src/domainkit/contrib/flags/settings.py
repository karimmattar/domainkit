"""Settings of ``domainkit.contrib.flags``: ``FLAGS = {...}``."""

from pydantic import BaseModel, ConfigDict, Field

from domainkit.conf import DEFAULT_CACHE_ALIAS


class FlagsSettings(BaseModel):
    """Where flags are cached, and for how long; every key is optional."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    CACHE: str = DEFAULT_CACHE_ALIAS
    """The cache alias holding every flag and dynamic setting, as one entry."""
    CACHE_TTL: float = Field(default=30.0, gt=0)
    """Seconds a change can take to reach other processes (their own cache
    entry expiring); this process, and every process sharing a Redis
    cache, see it at once."""
