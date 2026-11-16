"""The admin API and approvals, each change an act, mounted as a project would.

For the tests of ``AUTH.ADMIN_ACTION`` and of approvals' ``SUBMIT_ACTION``,
``REJECT_ACTION`` and ``CANCEL_ACTION`` (user report A6): the admin API,
the requests' routes, and auth's routes that enrol a second factor and
confirm an authorization with its code. ``TRANSFER`` is a guarded use case
of six arguments, more than a screen shows.
"""

from fastapi import APIRouter

from domainkit.contrib.approvals import ApprovalsUnitOfWork, requires_approval
from domainkit.contrib.approvals.api import router as approvals
from domainkit.contrib.auth.actions import router as actions
from domainkit.contrib.auth.admin import router as admin
from domainkit.contrib.auth.mfa import router as mfa
from domainkit.ddd import Command

TRANSFER = "tests.acted_transfer"
"""The guarded action."""

router = APIRouter()
router.include_router(admin, prefix="/admin/auth")
router.include_router(approvals, prefix="/approvals")
router.include_router(mfa, prefix="/auth")
router.include_router(actions, prefix="/auth")


class Transfer(Command):
    """Move money to a payee: six arguments."""

    payee: str
    iban: str
    amount: str
    currency: str
    memo: str = ""
    reference: str = ""


@requires_approval(TRANSFER)
async def transfer(command: Transfer, uow: ApprovalsUnitOfWork) -> None:
    """Nothing: the tests submit, reject, and cancel, and run nothing."""
