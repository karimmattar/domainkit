"""Approvals whose approvers authorize each approval, mounted as a project would.

For the tests of ``APPROVALS.APPROVE_ACTION`` (user report A6): the
requests' routes, and auth's routes that enrol a second factor and confirm
an authorization with its code. ``TRANSFER`` is a guarded use case of six
arguments, more than an approver's screen shows.
"""

from datetime import timedelta
from typing import ClassVar

from fastapi import APIRouter
from starlette.requests import HTTPConnection

from domainkit.contrib.approvals import ApprovalsUnitOfWork, requires_approval
from domainkit.contrib.approvals.api import router as approvals
from domainkit.contrib.auth import StepUp
from domainkit.contrib.auth.actions import router as actions
from domainkit.contrib.auth.mfa import router as mfa
from domainkit.ddd import Command
from domainkit.http.access import BasePermission, Principal

TRANSFER = "tests.transfer"
"""The guarded action."""

router = APIRouter()
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
    """Nothing: the tests approve, and run nothing."""


class Present(BasePermission):
    """A rule approving asks besides: the request says the approver is present."""

    message: ClassVar[str] = "Say you are present."

    async def has_permission(self, request: HTTPConnection, user: Principal) -> bool:
        """``X-Present: yes``."""
        return request.headers.get("x-present") == "yes"


WINDOW = StepUp(max_age=timedelta(minutes=5))
"""A step-up window: what ``APPROVE_ACTION`` takes the place of."""
