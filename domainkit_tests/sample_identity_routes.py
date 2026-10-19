"""The auth API with resets, verification, and signup, for identity tests."""

from typing import Any

from fastapi import APIRouter, Request

from domainkit.contrib.auth import CurrentUser
from domainkit.contrib.auth.api import router as auth_router
from domainkit.contrib.auth.recovery import router as recovery_router
from domainkit.contrib.auth.signup import router as signup_router
from domainkit.http.health import router as health_router

router = APIRouter()
router.include_router(health_router)
for included in (auth_router, recovery_router, signup_router):
    router.include_router(included, prefix="/auth")


@router.get("/whoami")
async def whoami(user: CurrentUser, request: Request) -> dict[str, Any]:
    """Anyone: who the request is from, and how."""
    credentials = request.scope.get("auth")
    return {
        "user": user.get_username(),
        "scheme": credentials.scheme if credentials else None,
    }
