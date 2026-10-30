"""Settings of ``domainkit.contrib.accounting``: ``ACCOUNTING = {...}``."""

from datetime import timedelta

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_RULES = (
    "domainkit.contrib.accounting.reconciliation.ByReference",
    "domainkit.contrib.accounting.reconciliation.ByAmountAndDate",
)
"""The matching rules a reconciliation tries, in order."""


class AccountingSettings(BaseModel):
    """How the ledger posts, holds, and reconciles; every key is optional."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    MAX_LINES: int = Field(default=100, ge=2, le=1000)
    """Lines one entry may have."""
    REQUIRE_PERIOD: bool = True
    """Refuse an entry dated where no period is open (``no_open_period``);
    False books it without a period, unless a closed one covers its date."""
    RETRY_ATTEMPTS: int = Field(default=5, ge=1, le=10)
    """Runs of a posting, the first included, when its transaction lost a
    race (a deadlock or a serialization failure; ``UnitOfWork.run``)."""
    HOLD_TTL: timedelta = Field(
        default=timedelta(days=7), gt=timedelta(0), le=timedelta(days=366)
    )
    """How long a hold lasts when it is placed without ``expires_at``."""
    EXPIRE_HOLDS_EVERY: timedelta = Field(
        default=timedelta(minutes=1), ge=timedelta(seconds=1), le=timedelta(days=1)
    )
    """How often the worker releases expired holds (with messaging); until
    then, an expired hold still counts against what is available."""
    MATCH_WINDOW_DAYS: int = Field(default=3, ge=0, le=31)
    """Days a statement line's date may be from its posting's and match."""
    MATCHING_RULES: tuple[str, ...] = Field(default=DEFAULT_RULES, min_length=1)
    """Import paths of the rules a reconciliation tries, in order: each a
    ``MatchingRule``. Add a project's own to extend matching."""
    MAX_STATEMENT_LINES: int = Field(default=10_000, ge=1, le=1_000_000)
    """Lines one statement may have."""
    REQUIRE_PROTECTION: bool = True
    """In staging and production, refuse to start on PostgreSQL when the
    triggers that keep entries and postings append-only are missing
    (``accounting.E001``); False only warns (``accounting.W001``)."""
