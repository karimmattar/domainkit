"""The routes of the sign-in approval tests: auth, sessions, devices, and ciba."""

from fastapi import APIRouter

from domainkit.contrib.auth.api import router as auth_router
from domainkit.contrib.auth.cookies import router as session_router
from domainkit.contrib.auth.mfa import router as mfa_router
from domainkit.contrib.ciba.api import router as sign_in_router
from domainkit.contrib.ciba.oidc import router as ciba_router
from domainkit.contrib.ciba.oidc import well_known
from domainkit.contrib.devices.api import router as devices_router
from domainkit.http.health import router as health_router

router = APIRouter()
router.include_router(health_router)
for included in (auth_router, session_router, mfa_router, devices_router):
    router.include_router(included, prefix="/auth")
router.include_router(sign_in_router, prefix="/auth")
router.include_router(ciba_router, prefix="/oauth")
router.include_router(well_known)
