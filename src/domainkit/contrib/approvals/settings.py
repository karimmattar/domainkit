"""Settings of ``domainkit.contrib.approvals``: ``APPROVALS = {...}``."""

from datetime import timedelta
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

LONGEST_WAIT: Final = timedelta(days=90)
"""The longest a request may wait for its approvals."""
MAX_APPROVALS: Final = 10
"""The most approvals a policy may ask for."""

Permission = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*\.[a-z0-9_]+$")]
"""A permission key: ``<domain>.<codename>``."""
AdminAction = Literal[
    "auth.set_staff",
    "auth.set_groups",
    "auth.set_permissions",
    "auth.set_password",
    "auth.impersonate",
]
"""A change of auth's admin API that ``ADMIN_APPROVALS`` may hold for approval."""
Approvals = Annotated[int, Field(ge=1, le=MAX_APPROVALS)]
"""How many approvals a policy asks for: 1 to ``MAX_APPROVALS``."""


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
    APPROVE_ACTION: bool = False
    """Approving asks for an authorization of its own (``approvals.approve``,
    ADR 0144), bound to the request and its fingerprint, in place of a
    step-up window; needs ``domainkit.contrib.auth``."""
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
    SUPERUSER_APPROVERS: Permission | None = None
    """The permission whoever approves making a superuser holds; None:
    superusers approve, and only they. An empty text is None: what an
    unset variable gives in a compose file."""
    ADMIN_APPROVALS: dict[AdminAction, Approvals] = Field(default_factory=dict)
    """The changes of auth's admin API held for approval, each with the
    approvals it needs: making staff, adding groups or permissions,
    setting a password, impersonating. Empty: none is."""
    ADMIN_APPROVERS: Permission | None = None
    """The permission whoever approves those changes holds; None:
    superusers approve, and only they. An empty text is None."""

    @field_validator("SUPERUSER_APPROVERS", "ADMIN_APPROVERS", mode="before")
    @classmethod
    def _empty_is_none(cls, value: object) -> object:
        """Read ``""`` as None (user report P3)."""
        return None if value == "" else value
