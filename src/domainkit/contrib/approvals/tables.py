"""The requests and the decisions about them.

A decision is unique per request and person: one person approves (or
rejects) a request once, however many sessions or tokens they hold, and
the database refuses a second row even from two transactions at once.
No table is in a fixture, and ``flush`` keeps them: they are the record
of who approved what.
"""

from typing import Final

from sqlalchemy import (
    JSON,
    Column,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    UniqueConstraint,
    Uuid,
)

from domainkit.db import EnumValue, UTCDateTime, metadata
from domainkit.db.orm import children, map_imperatively

from .exceptions import AlreadyDecided
from .models import (
    MAX_ACTION,
    MAX_PERSON,
    MAX_REASON,
    MAX_TENANT,
    ApprovalRequest,
    Decision,
    RequestStatus,
    Verdict,
)

INFO: Final = {"domain": "approvals", "fixtures": False, "flush": False}

requests = Table(
    "domainkit_approval_requests",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("action", String(MAX_ACTION), nullable=False),
    Column("arguments", JSON, nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("seal", String(128), nullable=False),
    Column("maker", String(MAX_PERSON), nullable=False, index=True),
    Column("policy", String(MAX_ACTION), nullable=False),
    Column("required", Integer, nullable=False),
    Column("approvals", Integer, nullable=False),
    Column("permission", String(MAX_PERSON)),
    Column("group", String(MAX_PERSON)),
    Column("status", EnumValue(RequestStatus), nullable=False),
    Column("reason", String(MAX_REASON), nullable=False),
    Column("submitted_at", UTCDateTime, nullable=False),
    Column("expires_at", UTCDateTime, nullable=False),
    Column("decided_at", UTCDateTime),
    Column("executed_at", UTCDateTime),
    Column("run_token", String(64)),
    Column("tenant_id", String(MAX_TENANT)),
    Column("version", Integer, nullable=False),
    Index("ix_domainkit_approval_requests_status", "status", "expires_at"),
    Index("ix_domainkit_approval_requests_submitted_at", "submitted_at"),
    Index(
        "ix_domainkit_approval_requests_scope",
        "tenant_id",
        "action",
        "policy",
        "submitted_at",
    ),
    info=INFO,
)
"""Commands waiting for approval, and what became of them.

``submitted_at`` is indexed for the listing's default order, newest first
(PERF-29). ``tenant_id`` is the tenant a request was submitted in, null
for none (ADR 0183). ``(tenant_id, action, policy, submitted_at)`` is
indexed for an approver's list, which reads, of the tenant served, the
requests of each action and policy that names them, newest first
(A28-PERF-04).
"""

decisions = Table(
    "domainkit_approval_decisions",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("request_id", Uuid, ForeignKey(requests.c.id), nullable=False),
    Column("approver", String(MAX_PERSON), nullable=False, index=True),
    Column("verdict", EnumValue(Verdict), nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("seal", String(128), nullable=False),
    Column("decided_at", UTCDateTime, nullable=False),
    UniqueConstraint("request_id", "approver", info={"duplicate": AlreadyDecided}),
    info=INFO,
)
"""Each person's approval or rejection of a request, once per request."""


def start_mappers() -> None:
    """Map the request and its decisions (idempotent)."""
    map_imperatively(Decision, decisions)
    map_imperatively(
        ApprovalRequest,
        requests,
        properties={"decisions": children(Decision, order_by=decisions.c.decided_at)},
    )
