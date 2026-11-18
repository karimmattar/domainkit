"""The HTTP API of the payouts domain (docs/resources.md).

``POST /payouts`` sends a payout at or under the threshold. Its maker holds
``payouts.add_payout`` and confirms the act first: ``POST
/payouts/authorization`` (the route's twin) shows them the payout, they
confirm it with a code at ``/auth/authorizations/{id}/confirm``, and send it
with the ``Action-Authorization`` header. Over the threshold the route
refuses it (``403 approval_required``): it goes to approvals instead.
"""

from domainkit.contrib.auth import StepUp
from domainkit.contrib.auth.actions import requires_authorization
from domainkit.http.access import HasPermissions
from domainkit.resources import ModelActions, ResourceRouter, schema_for
from fastapi import APIRouter

from domains.payouts import controllers
from domains.payouts.domain import Payout
from domains.payouts.models import SqlAlchemyPayoutsUnitOfWork

router = APIRouter(tags=["payouts"])


async def get_unit_of_work() -> SqlAlchemyPayoutsUnitOfWork:
    """A unit of work on the database; tests may override this dependency."""
    return SqlAlchemyPayoutsUnitOfWork()


payouts = ResourceRouter(
    "/payouts",  # GET /payouts, GET /payouts/{payout_id}
    model=Payout,
    unit_of_work=get_unit_of_work,
    repository=lambda uow: uow.payouts,
    rule=ModelActions(Payout),
)
payouts.command_route(  # POST /payouts, and its twin POST /payouts/authorization
    controllers.SendPayout,
    body=schema_for(controllers.SendPayout, write=True),
    service=requires_authorization(controllers.SEND)(controllers.send_payout),
    rule=HasPermissions("payouts.add_payout") & StepUp(for_=controllers.SEND),
)
router.include_router(payouts)
