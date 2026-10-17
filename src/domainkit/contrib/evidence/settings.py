"""Settings of ``domainkit.contrib.evidence``, read from ``EVIDENCE``."""

from typing import Final

from pydantic import BaseModel, ConfigDict, Field, SecretStr

DEVELOPMENT_SUBJECT_KEY: Final = b"domainkit-evidence-development-subject-key"
"""Used when ``SUBJECT_KEY`` is unset; public, so staging and production refuse it."""


class EvidenceSettings(BaseModel):
    """Where evidence is kept, how large a record may be, how keys hide subjects."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    LEDGER: str = "evidence"
    """Alias in ``LEDGERS`` (``immudb://``; ``memory://`` in tests)."""
    NAMESPACE: str = Field(default="evidence", pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    """Key prefix, so several projects or environments can share one ledger."""
    MAX_BYTES: int = Field(default=262_144, ge=1024, le=4_000_000)
    """Largest record in bytes; keep documents elsewhere and record their digests."""
    SUBJECT_KEY: SecretStr | None = Field(default=None, min_length=32)
    """Secret for the subject digests in keys: 32 characters or more, kept forever.

    Required in staging and production. Changing it hides earlier records
    from ``for_subject``; the keys in their receipts still work.
    """

    @property
    def subject_key(self) -> bytes:
        """``SUBJECT_KEY`` as bytes, or the development key when it is unset."""
        if self.SUBJECT_KEY is None:
            return DEVELOPMENT_SUBJECT_KEY
        return self.SUBJECT_KEY.get_secret_value().encode()
