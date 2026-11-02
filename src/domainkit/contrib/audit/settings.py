"""Settings of ``domainkit.contrib.audit``: ``AUDIT = {...}``."""

from datetime import timedelta
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from domainkit.conf import DEFAULT_DB_ALIAS

AUTH: Final = "domainkit.contrib.auth"
PCI_DSS_4: Final = "pci-dss-4"

PCI_RETENTION: Final = timedelta(days=365)
"""The least ``MIN_RETENTION`` under ``AUTH.PRESET = "pci-dss-4"``:
PCI DSS v4.0 10.5.1 keeps audit logs twelve months (FIN-95)."""
NAMESPACE_PATTERN: Final = r"^[a-z][a-z0-9_-]{0,31}$"
"""Namespaces: a lower-case letter, then up to 31 letters, digits, ``_``, ``-``."""
FIELD_PATTERN: Final = r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"
"""A field of an audit row's ``details`` that names an aggregate."""


class AuditSettings(BaseModel):
    """What to record; every key is optional."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    DATABASE: str = DEFAULT_DB_ALIAS
    """Alias holding the audit trail; event audits need units of work on it."""
    EVENTS: bool = True
    """Record every committed domain event, in the same transaction."""
    EXCLUDE: tuple[str, ...] = ()
    """Actions to skip, as shell patterns (``catalog.*``, ``*.viewed``)."""
    HISTORY_FIELDS: tuple[Annotated[str, Field(pattern=FIELD_PATTERN)], ...] = ()
    """Fields of ``details`` whose histories are read by aggregate
    (``history(aggregate=(field, id))``, ``GET /history?field=...``): on
    PostgreSQL, ``migrate`` keeps an index of each, and drops those of fields
    no longer named. A field not named is read by scanning the trail."""
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
    """``audit seal`` runs on a schedule of the project's own (cron), not in
    the worker: staging and production then do not warn that nothing seals."""
    REQUIRE_BASELINE: bool = True
    """Refuse to start in staging and production without a ledger, a
    namespace, a sealing schedule, and the ``protect()`` triggers (the
    default since 0.10); False only warns, as 0.9 did."""
    SEAL_LATE_WINDOW: timedelta = Field(
        default=timedelta(minutes=15), gt=timedelta(0), le=timedelta(days=1)
    )
    """How far before the newest sealed row sealing looks for rows that
    committed late. A row later than this stays unsealed, and ``audit verify``
    reports it; keep it above the longest transaction and the clock skew."""
    SEAL_BATCHES: int = Field(default=100, ge=1, le=10_000)
    """Batches of 1,000 rows one run seals at most; the rest wait for the next."""
    SEAL_BACKLOG_WARNING: int = Field(default=100_000, ge=1)
    """Rows left unsealed after a run past which it logs a warning."""
    CHECKPOINT_KEY: SecretStr | None = Field(default=None, max_length=1024)
    """The Ed25519 private key, in PEM, that signs checkpoints of the chain
    (``checkpoints``): those ``audit prune`` prunes below, and those
    ``audit verify --export`` writes for a verifier holding the public key.
    Read by those two commands alone: set it in their environment, not the
    application's, which verifies with ``CHECKPOINT_PUBLIC_KEY``. None:
    neither runs."""
    CHECKPOINT_PUBLIC_KEY: str | None = Field(default=None, max_length=1024)
    """The public half of ``CHECKPOINT_KEY``, in PEM: what ``audit verify``
    checks stored checkpoints with. Checked when the application starts.
    None: no stored checkpoint verifies, so a pruned trail does not."""
    MIN_RETENTION: timedelta = Field(
        default=timedelta(days=30),
        ge=timedelta(days=1),
        le=timedelta(days=3650),
    )
    """How old a seal must be before ``audit prune`` may cut there; a
    checkpoint of a younger one starts nothing in ``audit verify``. Raising
    it reports the prunes made below it before. Under the PCI preset
    (``AUTH.PRESET = "pci-dss-4"``) it is at least ``PCI_RETENTION``,
    365 days, whatever it says (``min_retention``)."""


def min_retention() -> timedelta:
    """``AUDIT.MIN_RETENTION``, raised to ``PCI_RETENTION`` under the PCI preset."""
    from domainkit.apps import registry

    kept = registry.settings(AuditSettings).MIN_RETENTION
    if registry.is_installed(AUTH):
        from domainkit.contrib.auth import AuthSettings

        if registry.settings(AuthSettings).PRESET == PCI_DSS_4:
            return max(kept, PCI_RETENTION)
    return kept
