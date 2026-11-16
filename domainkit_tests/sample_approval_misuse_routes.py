"""The acts' routes, and a route that withdraws a request the wrong way.

``POST /misused/{request_id}/cancel`` asks for an authorization of
``approvals.cancel``, as approvals' own route does, but runs
``record_cancellation``, the function for the project's own code, which
spends nothing. The tests show what that does: the step is taken, then the
request fails (``authorization_unspent``, in tests).
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from domainkit.contrib.approvals import (
    ApprovalsUnitOfWork,
    CancelRequest,
    record_cancellation,
)
from domainkit.contrib.auth import StepUp
from domainkit.http.access import Principal, Require
from domainkit_tests.sample_admin_action_routes import router as acts

CANCELLING = Depends(Require(StepUp(for_="approvals.cancel")))
"""What approvals' cancel route asks, with ``CANCEL_ACTION`` on."""

router = APIRouter()
router.include_router(acts)


@router.post("/misused/{request_id}/cancel")
async def misused(
    request_id: UUID, user: Annotated[Principal, CANCELLING]
) -> dict[str, str]:
    """Withdraw a request with the function for code: what a route must not do."""
    cancelled = await record_cancellation(
        CancelRequest(request_id=request_id, maker=str(user.id)),
        ApprovalsUnitOfWork(),
    )
    return {"status": cancelled.status}
