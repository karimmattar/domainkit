"""Staff tools mounted together: auth, flags, and the audit history."""

from fastapi import APIRouter, Depends

from domainkit.contrib.audit.api import router as audit_router
from domainkit.contrib.auth.admin import router as auth_admin_router
from domainkit.contrib.auth.api import router as auth_router
from domainkit.contrib.flags import RequireFlag
from domainkit.contrib.flags.admin import router as flags_router
from domainkit.http.access import Public

router = APIRouter()
router.include_router(auth_router, prefix="/auth")
router.include_router(auth_admin_router, prefix="/admin/auth")
router.include_router(flags_router, prefix="/admin/flags")
router.include_router(audit_router, prefix="/admin/audit")


@router.get("/beta", dependencies=[Depends(RequireFlag("beta")), Public])
async def beta() -> dict[str, str]:
    """A feature behind the ``beta`` flag."""
    return {"feature": "beta"}
