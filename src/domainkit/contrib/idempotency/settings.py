"""Settings of ``domainkit.contrib.idempotency``: ``IDEMPOTENCY = {...}``."""

from pydantic import BaseModel, ConfigDict, Field

from domainkit.conf import DEFAULT_DB_ALIAS


class IdempotencySettings(BaseModel):
    """How long keys live and which requests may carry one."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    DATABASE: str = DEFAULT_DB_ALIAS
    TTL_SECONDS: float = Field(default=24 * 3600.0, gt=0, le=30 * 24 * 3600)
    """How long a stored response is replayed for the same key."""
    LEASE_SECONDS: float = Field(default=60.0, gt=0, le=24 * 3600)
    """How long a request holds its key before a retry may take it over.

    Set it above the longest a request may run (the server's timeout).
    """
    METHODS: tuple[str, ...] = ("POST", "PATCH")
    MAX_RESPONSE_BYTES: int = Field(default=1_048_576, ge=1)
    """Larger responses are not stored: a retry runs again if nothing committed."""
    CREDENTIAL_HEADERS: tuple[str, ...] = (
        "authorization",
        "proxy-authorization",
        "cookie",
        "x-api-key",
    )
    """Headers that name a caller without an actor: their keys are apart."""
