"""The HTTP API of the transfers domain (docs/resources.md).

Reads are generic; requesting a transfer is a command. Every route needs
its model permission: ``transfers.view_transfer`` to read,
``transfers.add_transfer`` to request one; superusers have them all.
"""

from domainkit.resources import ModelActions, ResourceRouter
from fastapi import APIRouter

from domains.transfers import controllers
from domains.transfers.domain import Transfer
from domains.transfers.models import SqlAlchemyTransfersUnitOfWork

router = APIRouter(tags=["transfers"])


async def get_unit_of_work() -> SqlAlchemyTransfersUnitOfWork:
    """A unit of work on the database; tests may override this dependency."""
    return SqlAlchemyTransfersUnitOfWork()


transfers = ResourceRouter(
    "/transfers",  # GET /transfers, GET /transfers/{transfer_id}
    model=Transfer,
    unit_of_work=get_unit_of_work,
    repository=lambda uow: uow.transfers,
    rule=ModelActions(Transfer),
)
transfers.use_case(controllers.request_transfer)  # POST /transfers
router.include_router(transfers)
