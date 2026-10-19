"""Routes exercising throttles, cached pages, uploads, and translations."""

from dataclasses import replace
from typing import Final

from fastapi import APIRouter, Depends, Response, WebSocket
from pydantic import BaseModel, Field
from starlette.datastructures import Headers
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Receive, Scope, Send

from domainkit.context import bind_request, current_request
from domainkit.ddd import NotFound
from domainkit.http.ratelimit import Throttle
from domainkit.i18n import get_language

ACTOR_HEADER: Final = "X-Test-Actor"


class ActorFromHeader:
    """Make ``X-Test-Actor`` the actor, as authentication would."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app``."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Bind the actor the header names, if any."""
        context = current_request()
        actor = Headers(scope=scope).get(ACTOR_HEADER)
        if actor is None or context is None:
            await self.app(scope, receive, send)
            return
        with bind_request(replace(context, actor=actor)):
            await self.app(scope, receive, send)


def api_key(connection: HTTPConnection) -> str | None:
    """The caller's API key, if it sent one."""
    return connection.headers.get("X-API-Key")


async def tenant(connection: HTTPConnection) -> str | None:
    """The caller's tenant, looked up asynchronously."""
    return connection.headers.get("X-Tenant")


router = APIRouter()


@router.get("/throttled", dependencies=[Depends(Throttle("2/minute"))])
async def throttled() -> dict[str, str]:
    """Allow two requests a minute per caller."""
    return {"status": "ok"}


@router.get("/unthrottled")
async def unthrottled() -> dict[str, str]:
    """Count nothing."""
    return {"status": "ok"}


@router.get("/by-address", dependencies=[Depends(Throttle("1/minute", key="address"))])
async def by_address() -> dict[str, str]:
    """Allow one request a minute per address, users or not."""
    return {"status": "ok"}


@router.get("/by-key", dependencies=[Depends(Throttle("1/minute", key=api_key))])
async def by_key() -> dict[str, str]:
    """Allow one request a minute per API key (or address without one)."""
    return {"status": "ok"}


@router.get("/by-tenant", dependencies=[Depends(Throttle("1/minute", key=tenant))])
async def by_tenant() -> dict[str, str]:
    """Allow one request a minute per tenant."""
    return {"status": "ok"}


shared = Throttle("1/minute", scope="exports")


@router.get("/exports/a", dependencies=[Depends(shared)])
async def export_a() -> dict[str, str]:
    """Share one count with ``/exports/b``."""
    return {"export": "a"}


@router.get("/exports/b", dependencies=[Depends(shared)])
async def export_b() -> dict[str, str]:
    """Share one count with ``/exports/a``."""
    return {"export": "b"}


@router.websocket("/throttled-ws", dependencies=[Depends(Throttle("1/minute"))])
async def throttled_socket(websocket: WebSocket) -> None:
    """Accept one connection a minute per caller."""
    await websocket.accept()
    await websocket.send_text("hi")
    await websocket.close()


class Order(BaseModel):
    """A body with limits, for translated validation messages."""

    reference: str = Field(max_length=5)
    quantity: int = Field(gt=0)


@router.post("/orders")
async def place(order: Order) -> dict[str, int]:
    """Validate an order."""
    return {"quantity": order.quantity}


@router.get("/language")
async def language() -> dict[str, str]:
    """The language the request is served in."""
    return {"language": get_language()}


@router.get("/labelled")
async def labelled(response: Response) -> dict[str, str]:
    """Say the language and the variance of the response itself."""
    response.headers["Content-Language"] = "en-gb"
    response.headers["Vary"] = "Origin, accept-language"
    return {"status": "ok"}


@router.get("/missing-user")
async def missing_user() -> None:
    """Raise a domain error whose message the catalogs translate."""
    raise NotFound("No such user.")


@router.websocket("/language-ws")
async def language_socket(websocket: WebSocket) -> None:
    """Send the language the connection is served in."""
    await websocket.accept()
    await websocket.send_text(get_language())
    await websocket.close()
