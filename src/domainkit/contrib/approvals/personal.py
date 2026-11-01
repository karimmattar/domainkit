"""What approvals hold about a person: the requests they made, and their decisions.

``privacy export`` lists the requests a subject submitted (with the
commands they hold) and the decisions they took. ``privacy erase`` keeps
the record that a request was made and decided, which is the point of
maker-checker and a business record the law asks a bank to keep (a
payment's authorisation, SOX-style evidence of who checked whom): the
commands stay as approved, and the decisions as taken, pseudonymised
(FIN-94). Their pending requests are cancelled, and those approved but
not run yet fail (``maker_not_allowed``) rather than run; the reasons
they wrote, free text, are blanked; and their id is replaced, wherever it
stands as maker or approver, by one ``erased:`` id, the same for all of
their rows, so a request still shows that its maker and its approvers
were different people. Seals are made again for the pseudonym, so the
record still verifies. The audit trail keeps the actor of each event,
and its own erasure (``auditforget``) covers its personal data.
"""

import uuid
from collections.abc import Mapping
from typing import Any, Final

from sqlalchemy import or_, select

from domainkit.db import connections

from .exceptions import MakerNotAllowed
from .models import RequestId, RequestStatus, Verdict
from .tables import decisions, requests
from .uow import ApprovalsUnitOfWork, approvals_alias

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
    """Pseudonymise ``subject`` in the requests and decisions; how many of each."""
    pseudonym = f"{ERASED}{uuid.uuid4().hex}"
    counts = dict.fromkeys(("requests", "cancelled", "failed", "decisions"), 0)
    async with ApprovalsUnitOfWork() as uow:
        decided = select(decisions.c.request_id).where(decisions.c.approver == subject)
        found = await uow.session.scalars(
            select(requests.c.id).where(
                or_(requests.c.maker == subject, requests.c.id.in_(decided))
            )
        )
        for request_id in sorted(found):
            request = await uow.locked(RequestId(request_id))
            mine = request.maker == subject
            rejected = any(
                each.approver == subject and each.verdict is Verdict.REJECT
                for each in request.decisions
            )
            if mine and request.status is RequestStatus.PENDING and request.sealed():
                request.cancel(subject, "")
                counts["cancelled"] += 1
            elif mine and request.status is RequestStatus.APPROVED:
                request.failed(MakerNotAllowed.code)
                counts["failed"] += 1
            if mine or rejected:
                request.reason = ""
            counts["requests"] += mine
            counts["decisions"] += sum(
                each.approver == subject for each in request.decisions
            )
            request.pseudonymise(subject, pseudonym)
        await uow.commit()
    return {kind: count for kind, count in counts.items() if count}
