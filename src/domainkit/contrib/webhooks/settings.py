"""Settings of ``domainkit.contrib.webhooks``: ``WEBHOOKS = {...}``."""

from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, IPvAnyNetwork, model_validator

from domainkit.conf.global_settings import NAME_PATTERN
from domainkit.contrib.messaging.retry import MAX_DELAY


class WebhookSettings(BaseModel):
    """Where deliveries run, how often they are tried, and what they may reach."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    QUEUE: str = Field(default="webhooks", pattern=NAME_PATTERN)
    """The messaging queue of deliveries, so slow receivers hold up no other work."""
    MAX_ATTEMPTS: int = Field(default=12, ge=1, le=100)
    """Tries per delivery before it is marked failed."""
    RETRY_BASE_SECONDS: float = Field(default=30.0, gt=0, le=MAX_DELAY)
    """The first retry's delay; it doubles per attempt, with jitter."""
    RETRY_MAX_SECONDS: float = Field(default=6 * 3600.0, gt=0, le=MAX_DELAY)
    TIMEOUT: float = Field(default=15.0, gt=0, le=120)
    """Seconds one attempt may take in all: lookup, connection, and answer."""
    CONNECT_TIMEOUT: float = Field(default=5.0, gt=0, le=60)
    MAX_RESPONSE_BYTES: int = Field(default=65_536, ge=0, le=10_485_760)
    """Most bytes of an answer read; the rest is dropped with the connection."""
    MAX_PAYLOAD_BYTES: int = Field(default=262_144, ge=1024, le=10_485_760)
    """Largest body sent; a larger event is refused (and dead-lettered)."""
    ALLOWED_PRIVATE_NETWORKS: tuple[IPvAnyNetwork, ...] = ()
    """Private networks receivers may be in (``10.20.0.0/16``); none by default.

    Loopback and link-local networks are refused in staging and production,
    and cloud metadata addresses always.
    """
    CA_BUNDLE: Path | None = None
    """Certificates to trust instead of the system's (a private CA)."""
    SECRET_OVERLAP_SECONDS: float = Field(default=24 * 3600.0, ge=0, le=MAX_DELAY)
    """After a rotation, deliveries are also signed with the old secret this long."""
    RETENTION_SECONDS: float = Field(default=30 * 24 * 3600.0, gt=0)
    """How long finished deliveries and their attempts are kept."""
    USER_AGENT: str = Field(default="domainkit-webhooks", min_length=1, max_length=200)
    MAX_SUBSCRIPTIONS_PER_OWNER: int = Field(default=10, ge=1, le=1000)
    """Subscriptions one owner may make through the self-service API (``owned``)."""

    @model_validator(mode="after")
    def check_timeouts(self) -> Self:
        """Connect within the attempt's time."""
        if self.CONNECT_TIMEOUT > self.TIMEOUT:
            raise ValueError("CONNECT_TIMEOUT must not exceed TIMEOUT")
        return self
