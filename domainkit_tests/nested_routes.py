"""Routers inside routers, for the flattening tests (``test_routing``).

Each router gives its routes something to keep: a prefix, access rules on
the router or on the ``include_router`` call, tags, responses, a default
response class, a route class, callbacks, ``deprecated``, a lifespan, and
startup hooks. WebSockets, a Starlette route, and a mounted application
sit among them, and some paths overlap, so the order routes match in shows.
"""

from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Header, Request, Response, WebSocket
from fastapi.responses import PlainTextResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel

from domainkit.http.access import IsAuthenticated, Public, Require, mark_public

events: list[str] = []
"""What the routers' lifespan and startup hooks did, in order."""
STAMP = "X-Stamp"


class StampedRoute(APIRoute):
    """A route class whose handler stamps every response it builds."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """FastAPI's handler, then the stamp."""
        handler = super().get_route_handler()

        async def stamped(request: Request) -> Response:
            response = await handler(request)
            response.headers[STAMP] = "stamped"
            return response

        return stamped


@asynccontextmanager
async def lifespan(app: object) -> AsyncIterator[None]:
    """A router's own lifespan."""
    events.append("lifespan started")
    yield
    events.append("lifespan stopped")


def started() -> None:
    """A router's startup hook."""
    events.append("started")


class Item(BaseModel):
    """What the items routes take and answer."""

    name: str


class Event(BaseModel):
    """What the callback documents."""

    kind: str


callbacks = APIRouter()


@callbacks.post("{$callback_url}/events")
def item_created(body: Event) -> None:
    """Documented only: the call the service makes back."""


items = APIRouter(
    prefix="/items",
    tags=["items"],
    responses={418: {"description": "A teapot"}},
    route_class=StampedRoute,
    dependencies=[Public],
)


async def flavour() -> str:
    """A dependency tests override (``app.dependency_overrides``)."""
    return "plain"


@items.get("/new")
async def new_item(taste: Annotated[str, Depends(flavour)]) -> Item:
    """Listed before ``/{item_id}``: it wins for ``/items/new``."""
    return Item(name=f"new {taste}")


@items.get("/{item_id}")
async def get_item(item_id: int, x_tag: Annotated[str, Header()] = "-") -> Item:
    """An item by number; a path that is not one gets 422."""
    return Item(name=f"{item_id}:{x_tag}")


@items.put("/{item_id}", status_code=202, responses={409: {"description": "Busy"}})
async def replace_item(item_id: int, item: Item) -> Item:
    """Replace an item."""
    return Item(name=f"{item_id}:{item.name}")


@items.post("/", status_code=201, callbacks=callbacks.routes)
async def create_item(item: Item) -> Item:
    """Create an item; the service calls back."""
    return item


guarded = APIRouter(dependencies=[Depends(Require(IsAuthenticated))])
"""Its routes declare no rule of their own: the router's covers them."""


@guarded.get("/inside")
async def inside(request: Request) -> dict[str, str]:
    """Users only, by the router's rule."""
    return {"user": request.user.get_username()}


@guarded.websocket("/socket")
async def guarded_socket(websocket: WebSocket) -> None:
    """Users only, by the router's rule."""
    await websocket.accept()
    await websocket.send_text(websocket.user.get_username())
    await websocket.close()


plain = APIRouter(default_response_class=PlainTextResponse, deprecated=True)


@plain.get("/text", response_class=PlainTextResponse)
async def text() -> str:
    """Plain text, deprecated by its router."""
    return "plain"


@plain.get("/hidden", include_in_schema=False)
async def hidden() -> PlainTextResponse:
    """Served, never documented."""
    return PlainTextResponse("hidden")


@plain.websocket("/feed")
async def feed(websocket: WebSocket) -> None:
    """Anyone, by the ``include_router`` call's ``Public``: sends one message."""
    await websocket.accept()
    await websocket.send_text("fed")
    await websocket.close()


async def raw(request: Request) -> PlainTextResponse:
    """A Starlette route, public on purpose."""
    return PlainTextResponse("raw")


mounted = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@mounted.get("/ping", dependencies=[Public])
async def ping() -> dict[str, bool]:
    """A route of a mounted application."""
    return {"pong": True}


v1 = APIRouter(prefix="/v1", lifespan=lifespan, on_startup=[started])
v1.include_router(items)
v1.include_router(guarded, prefix="/guarded")
v1.include_router(plain, prefix="/plain", tags=["plain"], dependencies=[Public])
"""``Public`` on the call: the router's WebSocket has no rule of its own."""


@v1.websocket("/echo", dependencies=[Public])
async def echo(websocket: WebSocket) -> None:
    """Anyone: echoes one message."""
    await websocket.accept()
    await websocket.send_text(await websocket.receive_text())
    await websocket.close()


v1.add_route("/raw", mark_public(raw))
v1.mount("/sub", mounted)

staff = APIRouter()


@staff.get("/report")
async def report() -> dict[str, bool]:
    """Guarded by the ``include_router`` call that adds it."""
    return {"report": True}


router = APIRouter()


@router.get("/items/{name}", dependencies=[Public])
async def shadowed(name: str) -> dict[str, str]:
    """Top-level, before ``v1``: overlaps nothing under ``/v1``."""
    return {"name": name}


router.include_router(v1)
staff_rule = Depends(Require(IsAuthenticated))
router.include_router(staff, prefix="/staff", dependencies=[staff_rule])


@router.get("/open")
async def open_route() -> dict[str, bool]:
    """No rule anywhere: deny-by-default lists it."""
    return {"open": True}


@router.post("/v1/items/{item_id}", dependencies=[Public])
async def after(item_id: int) -> dict[str, int]:
    """After ``v1``: a POST that ``v1``'s PUT route only partly matches."""
    return {"after": item_id}
