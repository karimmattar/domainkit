"""Settings of ``domainkit.contrib.limits``: ``LIMITS = {...}``."""

from datetime import timedelta
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field

LONGEST_WINDOW: Final = timedelta(days=31)
"""The longest any window reaches back: a calendar month."""
AmountText = Annotated[str, Field(pattern=r"^\d{1,14}(\.\d{1,4})?$")]
"""An amount as fixed-point text (``"5000.00"``): never a float."""


class LimitDefault(BaseModel):
    """A limit the settings give, unless a stored limit of the same key replaces it.

    Its fields are a stored limit's (``Limit``), its amounts text in its
    ``currency``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    kind: str
    currency: str
    subject: str = ""
    direction: str = "out"
    per_transaction: AmountText | None = None
    window: str | None = None
    max_amount: AmountText | None = None
    max_count: int | None = None
    time_zone: str | None = None
    """None: ``TIME_ZONE``."""
    note: str = ""


class LimitsSettings(BaseModel):
    """Default limits, and how long usage is kept; every key is optional."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    DEFAULTS: tuple[LimitDefault, ...] = ()
    """Limits that apply unless a stored limit has the same code, kind,
    direction, currency, and subject (one disabled switches a default off)."""
    KEEP_USAGE: timedelta = Field(
        default=timedelta(days=35),
        ge=LONGEST_WINDOW + timedelta(days=1),
        le=timedelta(days=3660),
    )
    """How long a movement's usage is kept; longer than any window."""
    PURGE_EVERY: timedelta = Field(
        default=timedelta(hours=1), ge=timedelta(minutes=1), le=timedelta(days=1)
    )
    """How often the worker deletes usage older than ``KEEP_USAGE``
    (with messaging)."""
