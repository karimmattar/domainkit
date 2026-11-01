"""Approvals, the ledger, the auth admin, and step-ups, mounted as a project would."""

from typing import ClassVar

from fastapi import APIRouter
from starlette.requests import HTTPConnection

from domainkit.contrib.accounting.api import router as accounting
from domainkit.contrib.approvals.api import router as approvals
from domainkit.contrib.auth.admin import router as admin
from domainkit.contrib.auth.mfa import router as mfa
from domainkit.contrib.auth.stepup import router as step_up
from domainkit.http.access import BasePermission, Principal

router = APIRouter()
router.include_router(approvals, prefix="/approvals")
router.include_router(accounting, prefix="/accounting")
router.include_router(admin, prefix="/admin/auth")
router.include_router(mfa, prefix="/auth")
router.include_router(step_up, prefix="/auth")


class Proof(BasePermission):
    """A stand-in for a step-up rule: the request shows fresh proof (a header)."""

    message: ClassVar[str] = "Prove who you are again."

    async def has_permission(self, request: HTTPConnection, user: Principal) -> bool:
        """``X-Proof: fresh``."""
        return request.headers.get("x-proof") == "fresh"


class Never(BasePermission):
    """A policy's own approve rule that allows no one."""

    async def has_permission(self, request: HTTPConnection, user: Principal) -> bool:
        """Never."""
        return False


class OnlyPending(BasePermission):
    """A rule that looks at the request itself: only pending ones."""

    async def has_object_permission(
        self, request: HTTPConnection, user: Principal, obj: object
    ) -> bool:
        """The request is pending."""
        return getattr(obj, "status", None) == "pending"
