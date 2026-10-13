"""Settings of ``domainkit.contrib.idempotency``: ``IDEMPOTENCY = {...}``."""

from pydantic import BaseModel, ConfigDict, Field

from domainkit.conf import DEFAULT_DB_ALIAS


class IdempotencySettings(BaseModel):
    """How long keys live and which requests may carry one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    DATABASE: str = DEFAULT_DB_ALIAS
    TTL_SECONDS: float = Field(default=24 * 3600.0, gt=0)
    """How long a stored response is replayed for the same key."""
    METHODS: tuple[str, ...] = ("POST", "PATCH")
    MAX_RESPONSE_BYTES: int = Field(default=1_048_576, ge=1)
    """Larger responses are not stored; a retry then runs again."""
