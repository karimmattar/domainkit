"""Settings of ``domainkit.contrib.evidence``, read from ``EVIDENCE``."""

from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field, SecretStr

DEVELOPMENT_SUBJECT_KEY: Final = b"domainkit-evidence-development-subject-key"
"""Used when ``SUBJECT_KEY`` is unset; public, so staging and production refuse it."""
NAMESPACE_PATTERN: Final = r"^[a-z][a-z0-9_-]{0,31}$"
"""Namespaces: a lower-case letter, then up to 31 letters, digits, ``_``, ``-``."""
MAX_PREVIOUS_NAMESPACES: Final = 8

type Namespace = Annotated[str, Field(pattern=NAMESPACE_PATTERN)]


class EvidenceSettings(BaseModel):
    """Where evidence is kept, how large a record may be, how keys hide subjects."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    LEDGER: str = "evidence"
    """Alias in ``LEDGERS`` (``immudb://``; ``memory://`` in tests)."""
    NAMESPACE: Namespace = "evidence"
    """Key prefix, so several projects or environments can share one ledger."""
    PREVIOUS_NAMESPACES: tuple[Namespace, ...] = Field(
        default=(), max_length=MAX_PREVIOUS_NAMESPACES
    )
    """Namespaces used before ``NAMESPACE``, whose records ``get`` still
    reads; keys under any other namespace are not found (at most 8)."""
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
