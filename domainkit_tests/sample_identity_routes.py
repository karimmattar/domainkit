"""The auth API with resets, verification, signup, and sessions, for tests."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, WebSocket

from domainkit.contrib.auth import (
    AnyUser,
    CurrentUser,
    HasPermissions,
    HasScope,
    IsSuperuser,
    ModelPermissions,
    Public,
    Require,
)
from domainkit.contrib.auth.access import current_user
from domainkit.contrib.auth.api import router as auth_router
from domainkit.contrib.auth.cookies import router as session_router
from domainkit.contrib.auth.mfa import router as mfa_router
from domainkit.contrib.auth.personal_tokens import router as tokens_router
from domainkit.contrib.auth.recovery import router as recovery_router
from domainkit.contrib.auth.signup import router as signup_router
from domainkit.http.health import router as health_router

VIEW_PRODUCT = "shop.view_product"

router = APIRouter()
router.include_router(health_router)
for included in (
    auth_router,
    recovery_router,
    signup_router,
    session_router,
    mfa_router,
    tokens_router,
):
    router.include_router(included, prefix="/auth")


def _seen(user: AnyUser, request: Request) -> dict[str, Any]:
    """Who sent the request, and how."""
    credentials = request.scope.get("auth")
    return {
        "user": user.get_username(),
        "scheme": credentials.scheme if credentials else None,
    }


@router.get("/whoami")
async def whoami(user: CurrentUser, request: Request) -> dict[str, Any]:
    """Anyone: who the request is from, and how."""
    return _seen(user, request)


@router.post("/notes")
async def write_note(
    request: Request, user: Annotated[AnyUser, Depends(Require())]
) -> dict[str, Any]:
    """Users only: an unsafe method, which cookie requests need CSRF tokens for."""
    return _seen(user, request)


@router.websocket("/live", dependencies=[Public])
async def live(websocket: WebSocket) -> None:
    """Greet whoever opened the socket."""
    await websocket.accept()
    await websocket.send_json({"user": current_user(websocket).get_username()})
    await websocket.close()


@router.get(
    "/scoped/view", dependencies=[Depends(Require(HasPermissions(VIEW_PRODUCT)))]
)
async def scoped_view() -> dict[str, bool]:
    """Users who may view products, within their credentials' scopes."""
    return {"ok": True}


async def scoped_model() -> dict[str, bool]:
    """Model permissions by method, within the scopes."""
    return {"ok": True}


for method in ("GET", "DELETE"):
    router.add_api_route(
        "/scoped/model",
        scoped_model,
        methods=[method],
        operation_id=f"scoped_model_{method}",
        dependencies=[Depends(Require(ModelPermissions("shop.product")))],
    )


@router.get("/scoped/scope", dependencies=[Depends(Require(HasScope(VIEW_PRODUCT)))])
async def scoped_scope() -> dict[str, bool]:
    """Credentials that allow viewing products, whatever the user holds."""
    return {"ok": True}


@router.get("/scoped/root", dependencies=[Depends(Require(IsSuperuser))])
async def scoped_root() -> dict[str, bool]:
    """Superusers, without scopes."""
    return {"ok": True}
