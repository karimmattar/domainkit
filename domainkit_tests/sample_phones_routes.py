"""The routes of the phone tests: auth, and phone verification."""

from fastapi import APIRouter

from domainkit.contrib.auth.api import router as auth_router
from domainkit.contrib.phones.api import router as phones_router
from domainkit.http.health import router as health_router

router = APIRouter()
router.include_router(health_router)
for included in (auth_router, phones_router):
    router.include_router(included, prefix="/auth")
