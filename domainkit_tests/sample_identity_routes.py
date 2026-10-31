"""The auth API with resets, verification, signup, and sessions, for tests."""

from datetime import timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request, WebSocket
from pydantic import BaseModel

from domainkit.contrib.auth import (
    AnyUser,
    AuthenticatedUser,
    CurrentUser,
    IsAdminUser,
    IsSuperuser,
    StepUp,
    current_user,
)
from domainkit.contrib.auth.api import router as auth_router
from domainkit.contrib.auth.cookies import router as session_router
from domainkit.contrib.auth.mfa import router as mfa_router
from domainkit.contrib.auth.personal_tokens import router as tokens_router
from domainkit.contrib.auth.recovery import router as recovery_router
from domainkit.contrib.auth.sca import (
    consume_authorization,
    request_authorization,
)
from domainkit.contrib.auth.sca import router as sca_router
from domainkit.contrib.auth.signup import router as signup_router
from domainkit.contrib.auth.stepup import router as step_up_router
from domainkit.http.access import (
    HasPermissions,
    HasScope,
    ModelPermissions,
    Public,
    Require,
)
from domainkit.http.health import router as health_router
from domainkit.money import Money

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
    step_up_router,
    sca_router,
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


@router.get("/scoped/staff", dependencies=[Depends(Require(IsAdminUser))])
async def scoped_staff() -> dict[str, bool]:
    """Staff, without scopes."""
    return {"ok": True}


@router.get("/scoped/root", dependencies=[Depends(Require(IsSuperuser))])
async def scoped_root() -> dict[str, bool]:
    """Superusers, without scopes."""
    return {"ok": True}


@router.get("/mine")
async def mine(user: AuthenticatedUser) -> dict[str, str]:
    """Users: a route that checks no scope (``AuthenticatedUser`` alone)."""
    return {"user": user.get_username()}


@router.get(
    "/scoped/mine", dependencies=[Depends(Require(HasPermissions(VIEW_PRODUCT)))]
)
async def scoped_mine(user: AuthenticatedUser) -> dict[str, str]:
    """Users who may view products: the rule checks the scopes."""
    return {"user": user.get_username()}


FIVE_MINUTES = timedelta(minutes=5)


@router.post("/payees", dependencies=[Depends(Require(StepUp(max_age=FIVE_MINUTES)))])
async def add_payee(request: Request, user: AuthenticatedUser) -> dict[str, Any]:
    """Users who stepped up within five minutes."""
    return _seen(user, request)


@router.post(
    "/payees/strict",
    dependencies=[
        Depends(Require(StepUp(max_age=FIVE_MINUTES) & HasPermissions(VIEW_PRODUCT)))
    ],
)
async def add_payee_strictly() -> dict[str, bool]:
    """Users who stepped up and may view products: the denial answers first."""
    return {"ok": True}


@router.websocket(
    "/live/stepped", dependencies=[Depends(Require(StepUp(max_age=FIVE_MINUTES)))]
)
async def live_stepped(websocket: WebSocket) -> None:
    """Greet a user who stepped up, once the socket is open."""
    await websocket.accept()
    await websocket.send_json({"user": current_user(websocket).get_username()})
    await websocket.receive_text()
    await websocket.close()


class TransferIn(BaseModel):
    """An amount, to a payee."""

    amount: Money
    payee: str


class TransferOut(BaseModel):
    """A transfer that was made."""

    amount: Money
    payee: str
    challenge: UUID


@router.post("/transfers/authorize")
async def authorize_transfer(
    body: TransferIn, user: AuthenticatedUser
) -> dict[str, Any]:
    """Ask the user to confirm a transfer: its id, for ``/auth/sca/{id}``."""
    challenge = await request_authorization(
        user.id, body.amount, body.payee, purpose="transfer"
    )
    return {"challenge": str(challenge.id)}


@router.post("/transfers")
async def transfer(
    body: TransferIn, request: Request, user: AuthenticatedUser
) -> TransferOut:
    """Make a transfer its user authorized: this amount, to this payee, once."""
    used = await consume_authorization(
        request.headers.get("X-Authorization", ""),
        user_id=user.id,
        amount=body.amount,
        payee=body.payee,
        purpose="transfer",
    )
    return TransferOut(amount=body.amount, payee=body.payee, challenge=used.id)
