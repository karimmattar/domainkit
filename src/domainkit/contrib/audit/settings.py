"""Settings of ``domainkit.contrib.audit``: ``AUDIT = {...}``."""

from datetime import timedelta

from pydantic import BaseModel, ConfigDict, Field

from domainkit.conf import DEFAULT_DB_ALIAS


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
    ANCHOR_EVERY: timedelta = timedelta(hours=1)
    """Anchor a new head at most this often (the window a rewrite can hide in)."""
    SEAL_EVERY: timedelta = timedelta(minutes=5)
    """How often the worker seals new rows (with ``domainkit.contrib.messaging``)."""
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
