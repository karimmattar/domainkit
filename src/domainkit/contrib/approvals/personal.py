"""What approvals hold about a person: the requests they made, and their decisions.

``privacy export`` lists the requests a subject submitted (with the
commands they hold) and the decisions they took. ``privacy erase`` keeps
the record that a request was made and decided, which is the point of
maker-checker, but no longer links it to the person: their pending
requests are cancelled, the commands of every request they made are
blanked (the fingerprint stays, and proves what was approved), the
reasons they wrote are blanked, and their id is replaced, wherever it
stands as maker or approver, by one ``erased:`` id, the same for all of
their rows, so a request still shows that its maker and its approvers
were different people. A request of theirs approved but not run yet no
longer verifies, and fails rather than runs. The audit trail keeps the
actor of each event, and its own erasure (``auditforget``) covers its
personal data.
"""

import uuid
from collections.abc import Mapping
from typing import Any, Final

from sqlalchemy import or_, select, update

from domainkit import clock
from domainkit.db import connections

from .models import RequestStatus, Verdict
from .tables import decisions, requests
from .uow import approvals_alias

ERASED: Final = "erased:"
"""The start of the id that replaces an erased subject's."""


async def export_subject(subject: str) -> Mapping[str, object]:
    """The requests ``subject`` made and the decisions they took, oldest first."""
    async with connections.engine(approvals_alias()).connect() as connection:
        made = await connection.execute(
            select(requests)
            .where(requests.c.maker == subject)
            .order_by(requests.c.submitted_at, requests.c.id)
        )
        found: dict[str, Any] = {}
        mine = [
            {
                "id": str(row.id),
                "action": row.action,
                "arguments": row.arguments,
                "fingerprint": row.fingerprint,
                "status": row.status.value,
                "reason": row.reason,
                "submitted_at": row.submitted_at.isoformat(),
            }
            for row in made
        ]
        if mine:
            found["requests"] = mine
        decided = await connection.execute(
            select(decisions.c.request_id, decisions.c.verdict, decisions.c.decided_at)
            .where(decisions.c.approver == subject)
            .order_by(decisions.c.decided_at, decisions.c.id)
        )
        taken = [
            {
                "request_id": str(row.request_id),
                "verdict": row.verdict.value,
                "decided_at": row.decided_at.isoformat(),
            }
            for row in decided
        ]
        if taken:
            found["decisions"] = taken
    return found


async def erase_subject(subject: str) -> Mapping[str, int]:
    """Unlink ``subject`` from the requests and decisions; how many of each."""
    pseudonym = f"{ERASED}{uuid.uuid4().hex}"
    async with connections.engine(approvals_alias()).begin() as connection:
        rejected = select(decisions.c.request_id).where(
            decisions.c.approver == subject, decisions.c.verdict == Verdict.REJECT
        )
        cancelled = await connection.execute(
            update(requests)
            .where(
                requests.c.maker == subject,
                requests.c.status == RequestStatus.PENDING,
            )
            .values(
                status=RequestStatus.CANCELLED,
                decided_at=clock.now(),
                version=requests.c.version + 1,
            )
        )
        await connection.execute(
            update(requests)
            .where(
                or_(requests.c.maker == subject, requests.c.id.in_(rejected)),
            )
            .values(reason="", version=requests.c.version + 1)
        )
        made = await connection.execute(
            update(requests)
            .where(requests.c.maker == subject)
            .values(arguments={}, maker=pseudonym, version=requests.c.version + 1)
        )
        decided = await connection.execute(
            update(decisions)
            .where(decisions.c.approver == subject)
            .values(approver=pseudonym)
        )
    counts = {
        "requests": made.rowcount,
        "cancelled": cancelled.rowcount,
        "decisions": decided.rowcount,
    }
    return {kind: count for kind, count in counts.items() if count}
