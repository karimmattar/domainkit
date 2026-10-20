"""Routes exercising throttles, cached pages, uploads, and translations."""

from dataclasses import asdict, replace
from typing import Annotated, Final

from fastapi import (
    APIRouter,
    Depends,
    Query,
    Request,
    Response,
    UploadFile,
    WebSocket,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.datastructures import Headers
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Receive, Scope, Send

from domainkit.cache import cache_page
from domainkit.context import bind_request, current_request
from domainkit.ddd import NotFound
from domainkit.http.ratelimit import Throttle
from domainkit.http.uploads import IMAGES, PDF, save_upload, serve
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
        if context is None:  # Lifespan events have no request.
            await self.app(scope, receive, send)
            return
        actor = Headers(scope=scope).get(ACTOR_HEADER)
        if actor is None:
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


class Product(BaseModel):
    """What the catalog stores, including what only staff see."""

    id: int
    name: str
    cost: int = 7


class ProductOut(BaseModel):
    """What callers see of a product."""

    id: int
    name: str


computed: list[str] = []
"""What each run of a cached endpoint computed, in order."""


def page_routes() -> APIRouter:
    """The cached pages, on a new router.

    Tests build it when they run, not on import, so each test applies
    ``@cache_page`` itself (and mutation testing sees it do so).
    """
    pages = APIRouter()

    @pages.get("/products")
    @cache_page(ttl=60)
    async def products(
        category: str = "all", tag: Annotated[list[str] | None, Query()] = None
    ) -> list[ProductOut]:
        """List products; the list itself is cached."""
        computed.append(f"products:{category}:{tag}")
        return [
            ProductOut.model_validate(
                Product(id=1, name=category), from_attributes=True
            )
        ]

    @pages.get("/products/{product_id}")
    @cache_page(ttl=60, vary=["X-Tenant"], max_age=30)
    async def product(product_id: int) -> ProductOut:
        """One product, per tenant; the full record goes in, the public part out."""
        computed.append(f"product:{product_id}")
        if product_id == 0:
            raise NotFound("No such user.")
        return Product(id=product_id, name="widget")  # type: ignore[return-value]

    @pages.get("/me/basket")
    @cache_page(ttl=60, vary_on_user=True)
    async def basket() -> dict[str, str | None]:
        """The caller's basket, cached per user."""
        context = current_request()
        actor = context.actor if context else None
        computed.append(f"basket:{actor}")
        return {"owner": actor}

    @pages.get("/greeting")
    @cache_page(ttl=60, name="shared.greeting")
    async def greeting() -> dict[str, str]:
        """A greeting in the request's language."""
        computed.append(f"greeting:{get_language()}")
        return {"language": get_language()}

    @pages.get("/here")
    @cache_page(ttl=60)
    async def here(request: Request) -> dict[str, str]:
        """This page's absolute URL, as a tenant chosen by host would see it."""
        computed.append(f"here:{request.url.hostname}")
        return {"url": str(request.url_for("here"))}

    @pages.post("/products")
    @cache_page(ttl=60)
    async def add_product(body: ProductOut) -> ProductOut:
        """Never cached: only GET answers are."""
        computed.append(f"add:{body.id}")
        return body

    return pages


pages = page_routes()
"""The cached pages; ``test_cache_pages`` swaps in a new set for each test."""


@router.post("/uploads")
async def upload(file: UploadFile) -> dict[str, object]:
    """Keep an image or a PDF of up to 1,024 bytes."""
    stored = await save_upload(
        file,
        types=(*IMAGES, PDF),
        max_size=1024,
        storage="uploads",
        directory="avatars",
    )
    return asdict(stored)


@router.get("/files/{name:path}")
async def download(
    name: str,
    content_type: str = "application/octet-stream",
    inline: bool = False,
    filename: str | None = None,
) -> StreamingResponse:
    """Send a stored file."""
    return await serve(
        "uploads", name, content_type=content_type, inline=inline, filename=filename
    )
