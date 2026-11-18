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
and its own erasure (``audit forget``) covers its personal data.
"""

import uuid
from collections.abc import Mapping
from typing import Any, Final

from sqlalchemy import or_, select

from domainkit.db import connections
from domainkit.ddd.tenancy import all_tenants, tenant_alone

from .exceptions import MakerNotAllowed
from .models import RequestId, RequestStatus, Verdict
from .tables import decisions, requests
from .uow import ApprovalsUnitOfWork, approvals_alias, servable

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
    """Pseudonymise ``subject`` in the requests and decisions; how many of each.

    In every tenant's requests, as the export lists them all: those of
    each tenant in a transaction of their own, as that tenant alone, so
    what the commit publishes is that tenant's. A request whose stored
    tenant is not a tenant id was changed around the application; it
    still names the person, so it is pseudonymised too, read among every
    tenant's requests and committed as no tenant: its tenant is not to be
    trusted, and its seal is made again only if it held.
    """
    pseudonym = f"{ERASED}{uuid.uuid4().hex}"
    counts = dict.fromkeys(("requests", "cancelled", "failed", "decisions"), 0)
    async with ApprovalsUnitOfWork() as uow:
        decided = select(decisions.c.request_id).where(decisions.c.approver == subject)
        found = await uow.session.execute(
            select(requests.c.id, requests.c.tenant_id).where(
                or_(requests.c.maker == subject, requests.c.id.in_(decided))
            )
        )
        by_tenant: dict[str | None, list[RequestId]] = {}
        for row in found:
            by_tenant.setdefault(row.tenant_id, []).append(RequestId(row.id))
    for tenant_id in sorted(by_tenant, key=lambda each: each or ""):
        ids = sorted(by_tenant[tenant_id])
        if servable(tenant_id):
            with tenant_alone(tenant_id):
                await _erase(subject, pseudonym, ids, counts, tenant_id)
        else:  # Changed around the application: found among every tenant's.
            with all_tenants():
                await _erase(subject, pseudonym, ids, counts, None)
    return {kind: count for kind, count in counts.items() if count}


async def _erase(
    subject: str,
    pseudonym: str,
    ids: list[RequestId],
    counts: dict[str, int],
    committed_as: str | None,
) -> None:
    """Pseudonymise ``subject`` in the requests ``ids``, counting into ``counts``.

    The transaction commits as the tenant ``committed_as`` alone (None: no
    tenant).
    """
    async with ApprovalsUnitOfWork() as uow:
        for request_id in ids:
            request = await uow.locked(request_id)
            mine = request.maker == subject
            rejected = any(
                each.approver == subject and each.verdict is Verdict.REJECT
                for each in request.decisions
            )
            if (mine or rejected) and request.status is not RequestStatus.FAILED:
                request.reason = ""  # Their words; a failure's code is not.
            if mine and request.status is RequestStatus.PENDING and request.sealed():
                request.cancel(subject, "")
                counts["cancelled"] += 1
            elif mine and request.status is RequestStatus.APPROVED:
                request.failed(MakerNotAllowed.code)
                counts["failed"] += 1
            counts["requests"] += mine
            counts["decisions"] += sum(
                each.approver == subject for each in request.decisions
            )
            request.pseudonymise(subject, pseudonym)
        with tenant_alone(committed_as):
            await uow.commit()
