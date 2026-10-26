"""Routes with and without access rules, for the open-route check."""

from typing import Annotated

from fastapi import APIRouter, Depends, WebSocket

from domainkit.contrib.auth import AnyUser, AuthenticatedUser, CurrentUser
from domainkit.http.access import IsAuthenticated, Public, Require
from domainkit.http.health import router as health_router

router = APIRouter()


@router.get("/open")
async def open_route() -> dict[str, bool]:
    """No rule: the check lists it."""
    return {"ok": True}


@router.get("/public", dependencies=[Public])
async def public_route() -> dict[str, bool]:
    """Public on purpose."""
    return {"ok": True}


@router.get("/required", dependencies=[Depends(Require(IsAuthenticated))])
async def required() -> dict[str, bool]:
    """A rule on the route."""
    return {"ok": True}


@router.get("/current")
async def current(user: CurrentUser) -> dict[str, str]:
    """Reads the user, whoever it is."""
    return {"user": user.get_username()}


@router.get("/authenticated")
async def authenticated(user: AuthenticatedUser) -> dict[str, str]:
    """Users only."""
    return {"user": user.get_username()}


async def owner(user: Annotated[AnyUser, Depends(Require(IsAuthenticated))]) -> str:
    """A dependency that checks a rule itself."""
    return user.get_username()


@router.get("/nested")
async def nested(name: Annotated[str, Depends(owner)]) -> dict[str, str]:
    """The rule is a dependency of a dependency."""
    return {"user": name}


guarded = APIRouter(dependencies=[Depends(Require(IsAuthenticated))])


@guarded.get("/inside")
async def inside() -> dict[str, bool]:
    """The router's rule covers it."""
    return {"ok": True}


router.include_router(guarded, prefix="/guarded")


async def both() -> dict[str, bool]:
    """Two methods, no rule."""
    return {"ok": True}


router.add_api_route("/both", both, methods=["POST", "GET"])


@router.websocket("/socket")
async def socket(websocket: WebSocket) -> None:
    """A WebSocket without a rule."""
    await websocket.accept()
    await websocket.close()


@router.websocket("/public-socket", dependencies=[Public])
async def public_socket(websocket: WebSocket) -> None:
    """A WebSocket anyone may open."""
    await websocket.accept()
    await websocket.close()


live = APIRouter(dependencies=[Public])
"""Public on purpose, router and all."""


@live.websocket("/feed")
async def feed(websocket: WebSocket) -> None:
    """The router's ``Public`` covers it."""
    await websocket.accept()
    await websocket.close()


router.include_router(live, prefix="/live")

public = APIRouter()
"""Routes that need no user at all."""


@public.get("/ping", dependencies=[Public])
async def ping() -> dict[str, bool]:
    """Public on purpose."""
    return {"ok": True}


public.include_router(health_router)
