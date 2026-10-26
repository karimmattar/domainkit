"""Routes guarded by ``domainkit.contrib.auth.access``, with the auth API."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, WebSocket

from domainkit.context import current_request
from domainkit.contrib.auth import (
    AnyUser,
    AuthenticatedUser,
    CurrentUser,
    IsAdminUser,
    IsSuperuser,
    current_user,
)
from domainkit.contrib.auth.admin import router as admin_router
from domainkit.contrib.auth.api import router as auth_router
from domainkit.http.access import (
    AllowAny,
    HasObjectPermissions,
    HasPermissions,
    IsAuthenticated,
    IsAuthenticatedOrReadOnly,
    ModelPermissions,
    Public,
    Require,
    check_object_permissions,
)
from domainkit.http.health import router as health_router
from domainkit_tests.sample_domains.shop.models import Product

router = APIRouter()
router.include_router(auth_router, prefix="/auth")
router.include_router(admin_router, prefix="/admin/auth")
router.include_router(health_router)
counted: list[str] = []


@router.get("/whoami")
async def whoami(user: CurrentUser, request: Request) -> dict[str, Any]:
    """Anyone: who the request is from."""
    context = current_request()
    return {
        "user": user.get_username(),
        "actor": context.actor if context else None,
        "scheme": request.auth.scheme if request.auth else None,
    }


@router.get("/private")
async def private(user: AuthenticatedUser) -> dict[str, str]:
    """Users only."""
    return {"user": user.get_username()}


async def products(
    user: Annotated[AnyUser, Depends(Require(ModelPermissions(Product)))],
) -> dict[str, str]:
    """Django's model permissions, by method."""
    return {"user": user.get_username()}


for method in ("GET", "POST", "PUT", "DELETE", "OPTIONS"):
    router.add_api_route(
        "/products", products, methods=[method], operation_id=f"products_{method}"
    )


@router.get(
    "/catalog", dependencies=[Depends(Require(ModelPermissions("shop.product")))]
)
async def catalog() -> dict[str, bool]:
    """Model permissions named by content type."""
    return {"ok": True}


@router.get("/staff", dependencies=[Depends(Require(IsAdminUser))])
async def staff() -> dict[str, bool]:
    """Staff only."""
    return {"ok": True}


@router.get(
    "/publish",
    dependencies=[
        Depends(Require(IsAuthenticated(), HasPermissions("shop.publish_product")))
    ],
)
async def publish() -> dict[str, bool]:
    """Users with a permission."""
    return {"ok": True}


@router.get(
    "/either",
    dependencies=[
        Depends(Require(IsSuperuser() | HasPermissions("shop.view_product")))
    ],
)
async def either() -> dict[str, bool]:
    """Superusers, or users who may view products."""
    return {"ok": True}


@router.get("/anonymous", dependencies=[Depends(Require(~IsAuthenticated()))])
async def anonymous() -> dict[str, bool]:
    """Only callers who did not log in."""
    return {"ok": True}


async def notes() -> dict[str, bool]:
    """Anyone reads; users write."""
    return {"ok": True}


for method in ("GET", "POST"):
    router.add_api_route(
        "/notes",
        notes,
        methods=[method],
        operation_id=f"notes_{method}",
        dependencies=[Depends(Require(IsAuthenticatedOrReadOnly))],
    )


@router.get("/open", dependencies=[Depends(Require(AllowAny))])
async def open_route() -> dict[str, bool]:
    """Anyone."""
    return {"ok": True}


@router.get("/products/{owner}")
async def product(owner: str, request: Request, user: CurrentUser) -> dict[str, str]:
    """Object permissions: owners may change their products."""
    item = Product(id=None, name=owner)  # type: ignore[arg-type]
    await check_object_permissions(
        request, item, HasObjectPermissions("shop.change_product")
    )
    return {"owner": owner}


@router.post("/count", dependencies=[Public])
async def count(request: Request) -> dict[str, int]:
    """Count calls, for idempotency keys."""
    counted.append(current_user(request).get_username())
    return {"count": len(counted)}


@router.websocket("/socket", dependencies=[Public])
async def socket(websocket: WebSocket) -> None:
    """Greet the user over a WebSocket, with the actor its context carries."""
    await websocket.accept()
    request = current_request()
    await websocket.send_json(
        {
            "user": current_user(websocket).get_username(),
            "actor": request.actor if request else "no context",
        }
    )
    await websocket.close()


@router.websocket("/guarded-socket", dependencies=[Depends(Require(IsAdminUser))])
async def guarded_socket(websocket: WebSocket) -> None:
    """Staff only."""
    await websocket.accept()
    await websocket.send_json({"user": current_user(websocket).get_username()})
    await websocket.close()


@router.websocket("/user-socket")
async def user_socket(websocket: WebSocket, user: AuthenticatedUser) -> None:
    """Users who logged in."""
    await websocket.accept()
    await websocket.send_json({"user": user.get_username()})
    await websocket.close()
