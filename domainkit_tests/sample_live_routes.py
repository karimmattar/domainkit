"""WebSockets, event streams, and live events, for the live connection tests."""

from typing import Final

from fastapi import APIRouter, Depends, WebSocket
from starlette.websockets import WebSocketDisconnect

from domainkit.contrib.auth import Public
from domainkit.http.live import LiveLimits

router = APIRouter()

SHORT: Final = LiveLimits(idle_timeout=5, max_lifetime=60, message_rate="3/minute")


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
