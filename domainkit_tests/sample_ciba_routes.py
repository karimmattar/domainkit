"""The routes of the sign-in approval tests: auth, sessions, devices, and ciba."""

from datetime import timedelta

from fastapi import APIRouter, Depends, status

from domainkit.contrib.auth import StepUp
from domainkit.contrib.auth.api import router as auth_router
from domainkit.contrib.auth.cookies import router as session_router
from domainkit.contrib.auth.mfa import router as mfa_router
from domainkit.contrib.auth.personal_tokens import router as tokens_router
from domainkit.contrib.auth.recovery import router as recovery_router
from domainkit.contrib.auth.sca import router as sca_router
from domainkit.contrib.ciba.api import router as sign_in_router
from domainkit.contrib.ciba.oidc import router as ciba_router
from domainkit.contrib.ciba.oidc import well_known
from domainkit.contrib.devices.api import router as devices_router
from domainkit.http.access import Require
from domainkit.http.health import router as health_router

router = APIRouter()
router.include_router(health_router)
for included in (
    auth_router,
    session_router,
    mfa_router,
    tokens_router,
    recovery_router,
    devices_router,
    sca_router,
):
    router.include_router(included, prefix="/auth")
router.include_router(sign_in_router, prefix="/auth")
router.include_router(ciba_router, prefix="/oauth")
router.include_router(well_known)


@router.post(
    "/payees",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(Require(StepUp(timedelta(minutes=5))))],
)
async def add_payee() -> None:
    """An act that needs a step-up within 5 minutes."""
