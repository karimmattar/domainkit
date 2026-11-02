"""The audit trail as tests read it: its rows, as records.

A test of a control checks who did what, and what it was refused, from the
trail itself rather than from ``audit log``'s text (TEST-21)::

    from domainkit.contrib.audit.testing import audit_entries

    def test_the_maker_is_named(client, maker):
        ...
        [made] = client.run(audit_entries, action="payments.transfer_made")
        assert made.actor == maker.id

In an async test, ``await audit_entries(...)``. The rows are read from
``AUDIT.DATABASE`` (or ``database``), oldest first.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select

from domainkit.apps import registry
from domainkit.db import connections

from .settings import AuditSettings
from .tables import audit_log

__all__ = ["RecordedEntry", "audit_entries"]


@dataclass(frozen=True, slots=True)
class RecordedEntry:
    """One row of the audit trail, as it was written."""

    occurred_at: datetime
    actor: str
    """Who acted: a user's id, ``operator:<name>``, ``system``, ..."""
    action: str
    outcome: str
    """``success``, ``denied``, or ``failure``."""
    target: str | None
    request_id: str | None
    details: Mapping[str, Any]
    """What the entry recorded, personal data as pseudonyms."""


async def audit_entries(
    *,
    action: str | None = None,
    actor: str | None = None,
    target: str | None = None,
    database: str | None = None,
) -> list[RecordedEntry]:
    """The trail's entries, oldest first, those matching every filter given."""
    statement = select(audit_log).order_by(audit_log.c.occurred_at)
    for column, value in (
        (audit_log.c.action, action),
        (audit_log.c.actor, actor),
        (audit_log.c.target, target),
    ):
        if value is not None:
            statement = statement.where(column == value)
    alias = database or registry.settings(AuditSettings).DATABASE
    async with connections.engine(alias).connect() as connection:
        rows = (await connection.execute(statement)).mappings().all()
    return [
        RecordedEntry(
            occurred_at=row["occurred_at"],
            actor=row["actor"],
            action=row["action"],
            outcome=row["outcome"],
            target=row["target"],
            request_id=row["request_id"],
            details=row["details"],
        )
        for row in rows
    ]
