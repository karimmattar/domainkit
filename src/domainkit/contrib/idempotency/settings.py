"""Settings of ``domainkit.contrib.idempotency``: ``IDEMPOTENCY = {...}``."""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from domainkit.conf import DEFAULT_DB_ALIAS


class IdempotencySettings(BaseModel):
    """How long keys live and which requests may carry one."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    DATABASE: str = DEFAULT_DB_ALIAS
    TTL_SECONDS: float = Field(default=24 * 3600.0, gt=0, le=30 * 24 * 3600)
    """How long a stored response is replayed for the same key; at least
    ``LEASE_SECONDS``."""
    LEASE_SECONDS: float = Field(default=60.0, gt=0, le=24 * 3600)
    """How long a request holds its key before a retry may take it over.

    It must be above ``REQUEST_TIMEOUT`` (checked at startup), and above the
    ``RequestTimeout`` of keyed routes that set their own (``check``).
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

    @model_validator(mode="after")
    def check_lease(self) -> Self:
        """Keep a key until its lease runs out.

        A key that expired while its request still held it could be claimed
        again: a retry would run the request a second time.
        """
        if self.TTL_SECONDS < self.LEASE_SECONDS:
            raise ValueError(
                "TTL_SECONDS must be at least LEASE_SECONDS: a key that expired "
                "while its request still held it could be claimed again, and "
                "the request run twice"
            )
        return self
