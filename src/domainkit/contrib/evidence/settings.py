"""Settings of ``domainkit.contrib.evidence``, read from ``EVIDENCE``."""

from pydantic import BaseModel, ConfigDict, Field


class EvidenceSettings(BaseModel):
    """Where evidence is kept, and how large one record may be."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    LEDGER: str = "evidence"
    """Alias in ``LEDGERS`` (``immudb://``; ``memory://`` in tests)."""
    NAMESPACE: str = Field(default="evidence", pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    """Key prefix, so several projects or environments can share one ledger."""
    MAX_BYTES: int = Field(default=262_144, ge=1024, le=4_000_000)
    """Largest record in bytes; keep documents elsewhere and record their digests."""
