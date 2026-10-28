"""WebSockets, event streams, and live events, for the live connection tests."""

from collections.abc import AsyncIterator
from typing import Any, Final

from fastapi import APIRouter, Depends, Request, WebSocket
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocketDisconnect

from domainkit.contrib.auth import current_user
from domainkit.contrib.auth.api import router as auth_router
from domainkit.contrib.auth.backends import has_perms
from domainkit.contrib.auth.events import UserCreated, UserPermissionGranted
from domainkit.ddd import DomainEvent
from domainkit.http.access import (
    AllowAny,
    BasePermission,
    HasPermissions,
    Public,
    Require,
)
from domainkit.http.live import TOO_SLOW, Closed, LiveLimits
from domainkit.http.live_events import live_events
from domainkit.http.sse import EventSourceResponse, ServerSentEvent, last_event_id

router = APIRouter()
router.include_router(auth_router, prefix="/auth")

VIEW_PRODUCT: Final = "shop.view_product"
SHORT: Final = LiveLimits(idle_timeout=5, max_lifetime=60, message_rate="3/minute")


class OwnEvents(BasePermission):
    """Users see the events about themselves."""

    async def has_object_permission(
        self,
        request: HTTPConnection,
        user: Any,  # noqa: ANN401 - any user.
        obj: object,
    ) -> bool:
        """The event names the user."""
        return getattr(obj, "user_id", None) == getattr(user, "id", None)


async def echo_until_closed(websocket: WebSocket) -> None:
    """Accept, then send back each text message until the client leaves."""
    await websocket.accept()
    try:
        while True:
            await websocket.send_text(await websocket.receive_text())
    except WebSocketDisconnect:
        return


@router.websocket("/live/echo", dependencies=[Public])
async def echo(websocket: WebSocket) -> None:
    """Send back what the client sends."""
    await echo_until_closed(websocket)


@router.websocket("/live/short", dependencies=[Public, Depends(SHORT)])
async def short(websocket: WebSocket) -> None:
    """Echo under the route's own, shorter limits."""
    await echo_until_closed(websocket)


@router.websocket(
    "/live/products", dependencies=[Depends(Require(HasPermissions(VIEW_PRODUCT)))]
)
async def products(websocket: WebSocket) -> None:
    """Echo, for users who may view products."""
    await echo_until_closed(websocket)


@router.websocket("/live/users", dependencies=[Depends(Require())])
async def user_events(websocket: WebSocket) -> None:
    """Each user's own committed events."""
    await live_events.forward(
        websocket, UserCreated, UserPermissionGranted, rule=OwnEvents()
    )


@router.websocket("/live/everything", dependencies=[Public])
async def everything(websocket: WebSocket) -> None:
    """Every committed event, to anyone (for tests)."""
    await live_events.forward(websocket, DomainEvent, rule=AllowAny())


@router.websocket("/live/whoami", dependencies=[Depends(Require())])
async def whoami(websocket: WebSocket) -> None:
    """Answer each message with whether the user, as now loaded, may view products."""
    await websocket.accept()
    try:
        while True:
            await websocket.receive_text()
            allowed = await has_perms(current_user(websocket), [VIEW_PRODUCT])
            await websocket.send_json({"may_view": allowed})
    except WebSocketDisconnect:
        return


@router.get("/live/users/feed", dependencies=[Depends(Require())])
async def user_feed(request: Request) -> EventSourceResponse:
    """Each user's own committed events, as an event stream."""
    return live_events.stream(request, UserPermissionGranted, rule=OwnEvents())


@router.websocket("/live/mine", dependencies=[Public])
async def my_events(websocket: WebSocket) -> None:
    """A route anyone may open: each event goes to the user it is about."""
    await live_events.forward(websocket, UserPermissionGranted, rule=OwnEvents())


@router.get("/live/mine/feed", dependencies=[Public])
async def my_feed(request: Request) -> EventSourceResponse:
    """The same as an event stream."""
    return live_events.stream(request, UserPermissionGranted, rule=OwnEvents())


@router.get("/live/count", dependencies=[Public])
async def count(
    request: Request, events: int = 3, end: bool = False
) -> EventSourceResponse:
    """``events`` numbered events, then the end; ``end`` ends it as too slow."""

    async def numbers() -> AsyncIterator[ServerSentEvent | str]:
        after = int(last_event_id(request) or 0)
        for number in range(after + 1, after + events + 1):
            yield ServerSentEvent.json({"n": number}, event="number", id=str(number))
        yield "done"
        if end:
            raise Closed(TOO_SLOW)

    return EventSourceResponse(numbers(), retry=2000, headers={"X-Feed": "count"})


@router.get(
    "/live/products/feed",
    dependencies=[Depends(Require(HasPermissions(VIEW_PRODUCT)))],
)
async def product_feed(request: Request) -> EventSourceResponse:
    """A stream that never ends by itself, for users who may view products."""
    return live_events.stream(request, UserPermissionGranted, rule=AllowAny())
