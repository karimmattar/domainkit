"""WebSockets, event streams, and live events, for the live connection tests."""

from collections.abc import AsyncIterator
from typing import Final

from fastapi import APIRouter, Depends, Request, WebSocket
from starlette.websockets import WebSocketDisconnect

from domainkit.contrib.auth import Public
from domainkit.http.live import TOO_SLOW, Closed, LiveLimits
from domainkit.http.sse import EventSourceResponse, ServerSentEvent, last_event_id

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
