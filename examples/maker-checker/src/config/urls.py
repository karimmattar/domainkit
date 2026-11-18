"""The project's routes: mount each domain's router here.

Every route must say who may call it: ``Depends(Require(...))``, or
``dependencies=[Public]`` for anyone. ``manage.py check`` lists those that
do not, and staging and production refuse to start with any.
"""

from domainkit.contrib.approvals.api import router as approvals_router
from domainkit.contrib.auth.actions import router as authorizations_router
from domainkit.contrib.auth.admin import router as admin_router
from domainkit.contrib.auth.api import router as auth_router
from domainkit.contrib.auth.mfa import router as mfa_router
from domainkit.http.health import router as health_router
from fastapi import APIRouter

from domains.payouts.routes import router as payouts_router

router = APIRouter()
router.include_router(health_router)
router.include_router(auth_router, prefix="/auth")
# An authenticator app, and the acts it confirms with a code.
router.include_router(mfa_router, prefix="/auth")
router.include_router(authorizations_router, prefix="/auth")
router.include_router(admin_router, prefix="/admin/auth")
# Large payouts are submitted, approved, rejected, and cancelled here.
router.include_router(approvals_router, prefix="/api/approvals")
router.include_router(payouts_router, prefix="/api/v1")
