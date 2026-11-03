"""The routes of the identity provider tests: auth, its second factor, identity."""

from fastapi import APIRouter

from domainkit.contrib.auth.api import router as auth_router
from domainkit.contrib.auth.mfa import router as mfa_router
from domainkit.contrib.identity.api import router as identity_router
from domainkit.http.health import router as health_router

router = APIRouter()
router.include_router(health_router)
for included in (auth_router, mfa_router, identity_router):
    router.include_router(included, prefix="/auth")
