"""Settings of ``domainkit.contrib.evidence``, read from ``EVIDENCE``."""

from typing import Annotated, Final, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

DEVELOPMENT_SUBJECT_KEY: Final = b"domainkit-evidence-development-subject-key"
"""Used when ``SUBJECT_KEY`` is unset; public, so staging and production refuse it."""
NAMESPACE_PATTERN: Final = r"^[a-z][a-z0-9_-]{0,31}$"
"""Namespaces: a lower-case letter, then up to 31 letters, digits, ``_``, ``-``."""
MAX_PREVIOUS_NAMESPACES: Final = 8
MAX_SUBJECT_KEYS: Final = 8
KEY_ID_PATTERN: Final = r"^[A-Za-z0-9_-]{1,32}$"
"""A subject key's id: 1 to 32 letters, digits, ``_``, ``-`` (``2027``)."""
DEFAULT_KEY_ID: Final = "default"
"""The id ``SUBJECT_KEY`` has in the ring of subject keys."""
DEVELOPMENT_KEY_ID: Final = "development"
"""The id of the development key, used when no subject key is set."""

type Namespace = Annotated[str, Field(pattern=NAMESPACE_PATTERN)]
type KeyId = Annotated[str, Field(pattern=KEY_ID_PATTERN)]
type SubjectKey = Annotated[SecretStr, Field(min_length=32)]


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
    """Secret for the subject digests in keys: 32 characters or more.

    Staging and production need it, or ``SUBJECT_KEYS``. In the ring of
    subject keys it has the id ``default``. Changing it without keeping
    it in the ring hides earlier records from ``for_subject``; the keys in
    their receipts still work.
    """
    SUBJECT_KEYS: dict[KeyId, SubjectKey] = Field(
        default_factory=dict, max_length=MAX_SUBJECT_KEYS
    )
    """More subject keys by id (at most 8): the current one makes the keys of
    new records, and the others still find the records they made."""
    SUBJECT_KEY_ID: KeyId | None = None
    """The id of the current subject key; needed when the ring holds several."""

    @model_validator(mode="after")
    def _ring(self) -> Self:
        """Refuse a ring without a current key, or one that names none of its keys.

        Raises:
            ValueError: ``SUBJECT_KEYS`` uses the id ``default``, the ring
                holds several keys and ``SUBJECT_KEY_ID`` names none, or
                it names one the ring does not hold.
        """
        if DEFAULT_KEY_ID in self.SUBJECT_KEYS:
            raise ValueError(
                f"SUBJECT_KEYS may not use the id {DEFAULT_KEY_ID!r}: it names "
                "SUBJECT_KEY."
            )
        ids = self._secrets().keys()
        if self.SUBJECT_KEY_ID is None and len(ids) > 1:
            raise ValueError("SUBJECT_KEY_ID must name the current subject key.")
        if self.SUBJECT_KEY_ID is not None and self.SUBJECT_KEY_ID not in ids:
            raise ValueError(
                f"SUBJECT_KEY_ID {self.SUBJECT_KEY_ID!r} names no key of SUBJECT_KEYS "
                "or SUBJECT_KEY."
            )
        return self

    def _secrets(self) -> dict[str, SecretStr]:
        """``SUBJECT_KEYS``, and ``SUBJECT_KEY`` as ``default``."""
        found = dict(self.SUBJECT_KEYS)
        if self.SUBJECT_KEY is not None:
            found[DEFAULT_KEY_ID] = self.SUBJECT_KEY
        return found

    @property
    def has_subject_key(self) -> bool:
        """Whether a subject key is set, rather than the development key."""
        return bool(self._secrets())

    @property
    def subject_keys(self) -> dict[str, bytes]:
        """Every subject key by id, the current one first.

        The development key alone (``development``) when none is set.
        """
        found = {
            key_id: secret.get_secret_value().encode()
            for key_id, secret in self._secrets().items()
        }
        if not found:
            return {DEVELOPMENT_KEY_ID: DEVELOPMENT_SUBJECT_KEY}
        current = self.SUBJECT_KEY_ID or next(iter(found))
        return {current: found.pop(current), **found}

    @property
    def subject_key(self) -> bytes:
        """The current subject key, as bytes: what new records' keys are made with."""
        return next(iter(self.subject_keys.values()))
