"""Settings of ``domainkit.contrib.approvals``: ``APPROVALS = {...}``."""

from datetime import timedelta
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field

LONGEST_WAIT: Final = timedelta(days=90)
"""The longest a request may wait for its approvals."""
MAX_APPROVALS: Final = 10
"""The most approvals a policy may ask for."""

Permission = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*\.[a-z0-9_]+$")]
"""A permission key: ``<domain>.<codename>``."""


class ApprovalsSettings(BaseModel):
    """How requests wait, who approves them, what is guarded; every key optional."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    EXPIRES_AFTER: timedelta = Field(
        default=timedelta(days=3), ge=timedelta(minutes=1), le=LONGEST_WAIT
    )
    """How long a request waits for its approvals, unless its policy says."""
    EXPIRE_EVERY: timedelta = Field(
        default=timedelta(minutes=5), ge=timedelta(seconds=1), le=timedelta(days=1)
    )
    """How often the worker expires the requests whose time is up."""
    APPROVE_RULE: str | None = None
    """Import path of an access rule (a class, or an instance) that whoever
    approves must pass as well, unless a request's policy names its own:
    a project's step-up rule, for one."""
    MAX_ARGUMENTS_BYTES: int = Field(default=65_536, ge=1024, le=1_048_576)
    """The largest command a request may hold, as JSON."""
    ENTRY_THRESHOLDS: dict[str, str] = Field(default_factory=dict)
    """Per currency, the amount (text) over which ``accounting.post_entry``
    needs approval. Empty: postings are not guarded."""
    ENTRY_APPROVALS: int = Field(default=2, ge=1, le=MAX_APPROVALS)
    """Approvals a posting over its threshold needs."""
    ENTRY_APPROVERS: Permission = "accounting.add_entry"
    """The permission whoever approves a posting holds."""
    SUPERUSER_APPROVALS: int = Field(default=0, ge=0, le=MAX_APPROVALS)
    """Approvals making a superuser needs; 0: it is not guarded."""
    SUPERUSER_APPROVERS: Permission = "auth.change_user"
    """The permission whoever approves making a superuser holds."""
