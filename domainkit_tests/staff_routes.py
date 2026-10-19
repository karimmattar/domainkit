"""Staff tools mounted together: auth and the audit history."""

from fastapi import APIRouter

from domainkit.contrib.audit.api import router as audit_router
from domainkit.contrib.auth.admin import router as auth_admin_router
from domainkit.contrib.auth.api import router as auth_router

router = APIRouter()
router.include_router(auth_router, prefix="/auth")
router.include_router(auth_admin_router, prefix="/admin/auth")
router.include_router(audit_router, prefix="/admin/audit")

