"""Settings of ``domainkit.contrib.audit``: ``AUDIT = {...}``."""

from datetime import timedelta
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from domainkit.conf import DEFAULT_DB_ALIAS

NAMESPACE_PATTERN: Final = r"^[a-z][a-z0-9_-]{0,31}$"
"""Namespaces: a lower-case letter, then up to 31 letters, digits, ``_``, ``-``."""


class AuditSettings(BaseModel):
    """What to record; every key is optional."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    DATABASE: str = DEFAULT_DB_ALIAS
    """Alias holding the audit trail; event audits need units of work on it."""
    EVENTS: bool = True
    """Record every committed domain event, in the same transaction."""
    EXCLUDE: tuple[str, ...] = ()
    """Actions to skip, as shell patterns (``catalog.*``, ``*.viewed``)."""
    LEDGER: str | None = None
    """Alias in ``LEDGERS`` to anchor the chain's head on; None anchors nowhere."""
    NAMESPACE: str | None = Field(default=None, pattern=NAMESPACE_PATTERN)
    """Part of the ledger key anchors go under, ``domainkit/audit/<NAMESPACE>/<alias>``,
    so several projects or environments can share one ledger. None keeps the
    key of 0.9.0, ``domainkit/audit/<alias>``, which they would all share;
    staging and production warn about it."""
    PREVIOUS_ANCHORS: bool = True
    """With ``NAMESPACE``, also verify the heads anchored before it was set,
    under ``domainkit/audit/<alias>``, so the trail stays verified across
    the change. Turn it off when other projects anchored under that key too."""
    ANCHOR_EVERY: timedelta = timedelta(hours=1)
    """Anchor a new head at most this often (the window a rewrite can hide in)."""
    SEAL_EVERY: timedelta = timedelta(minutes=5)
    """How often the worker seals new rows (with ``domainkit.contrib.messaging``)."""
    SEALED_EXTERNALLY: bool = False
    """``auditseal`` runs on a schedule of the project's own (cron), not in
    the worker: staging and production then do not warn that nothing seals."""
    REQUIRE_BASELINE: bool = False
    """Refuse to start in staging and production without a ledger, a
    namespace, a sealing schedule, and the ``protect()`` triggers, instead
    of warning (0.10 refuses by default)."""
    SEAL_LATE_WINDOW: timedelta = Field(
        default=timedelta(minutes=15), gt=timedelta(0), le=timedelta(days=1)
    )
    """How far before the newest sealed row sealing looks for rows that
    committed late. A row later than this stays unsealed, and ``auditverify``
    reports it; keep it above the longest transaction and the clock skew."""
    SEAL_BATCHES: int = Field(default=100, ge=1, le=10_000)
    """Batches of 1,000 rows one run seals at most; the rest wait for the next."""
    SEAL_BACKLOG_WARNING: int = Field(default=100_000, ge=1)
    """Rows left unsealed after a run past which it logs a warning."""
    CHECKPOINT_KEY: SecretStr | None = Field(default=None, max_length=1024)
    """The Ed25519 private key, in PEM, that signs checkpoints of the chain
    (``checkpoints``): those ``auditprune`` prunes below, and those
    ``auditverify --export`` writes for a verifier holding the public key.
    None: neither runs."""
