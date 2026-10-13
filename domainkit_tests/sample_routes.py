"""Routes exercising the middleware and error handling."""

from collections.abc import AsyncIterator
from typing import Final

from fastapi import APIRouter, HTTPException, Request, Response, WebSocket
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

from domainkit.ddd import (
    BusinessRuleViolation,
    ConcurrentUpdate,
    Conflict,
    DomainError,
    NotFound,
    PermissionDenied,
)
from domainkit.http import APIError
from domainkit.http.health import router as health_router
from domainkit.http.metrics import router as metrics_router


class Overbooked(Conflict):
    """A domain error with its own code."""

    code = "fully_booked"


ERRORS: Final[dict[str, type[DomainError]]] = {
    "not_found": NotFound,
    "conflict": Conflict,
    "denied": PermissionDenied,
    "rule": BusinessRuleViolation,
    "domain": DomainError,
    "custom": Overbooked,
    "concurrent": ConcurrentUpdate,
}


class Typed(BaseModel):
    """Strict body for validation tests."""

    model_config = ConfigDict(extra="forbid")

    count: int


router = APIRouter()
router.include_router(health_router)
router.include_router(metrics_router)
NOT_A_ROUTER: Final = "not a router"


@router.get("/ok")
async def ok() -> dict[str, str]:
    """Succeed."""
    return {"status": "ok"}


@router.get("/framed")
async def framed() -> Response:
    """Set a header the security middleware must not overwrite."""
    return Response(headers={"X-Frame-Options": "SAMEORIGIN"})


@router.get("/boom")
async def boom() -> None:
    """Fail before the response starts."""
    raise RuntimeError("boom")


@router.get("/stream-boom")
async def stream_boom() -> StreamingResponse:
    """Fail after the response has started."""

    async def chunks() -> AsyncIterator[bytes]:
        yield b"partial"
        raise RuntimeError("mid-stream")

    return StreamingResponse(chunks())


@router.post("/echo")
async def echo(request: Request) -> dict[str, int]:
    """Read the whole body."""
    return {"size": len(await request.body())}


@router.get("/errors/{kind}")
async def error(kind: str) -> None:
    """Raise the requested domain error."""
    raise ERRORS[kind](f"{kind} happened")


@router.post("/typed")
async def typed(body: Typed) -> dict[str, int]:
    """Validate a body."""
    return {"count": body.count}


@router.get("/http/{status_code}")
async def http_error(
    status_code: int, detail: str | None = None, structured: bool = False
) -> None:
    """Raise a plain HTTPException."""
    raise HTTPException(
        status_code,
        detail={"reason": "structured"} if structured else detail,
        headers={"X-Reason": "test"},
    )


@router.get("/api-error")
async def api_error() -> None:
    """Raise an APIError with its own code and headers."""
    raise APIError(429, "Slow down.", code="rate_limited", headers={"Retry-After": "1"})


created: list[dict[str, object]] = []


@router.post("/counter", status_code=201)
async def counter(request: Request, response: Response) -> dict[str, int]:
    """Create something; each real run increments the count."""
    created.append(await request.json())
    response.headers["Location"] = f"/counter/{len(created)}"
    return {"count": len(created)}


@router.post("/unavailable")
async def unavailable() -> None:
    """Fail with a server error the client may retry."""
    raise HTTPException(503, "Try later.")


@router.post("/crash")
async def crash() -> None:
    """Fail with an unhandled exception."""
    raise RuntimeError("crashed")


@router.post("/large")
async def large() -> Response:
    """Return a body larger than the stored-response limit used in tests."""
    return Response(b"x" * 2048)


@router.post("/respond")
async def respond(status_code: int = 200, size: int = 0, parts: int = 1) -> Response:
    """Answer with ``status_code`` and ``parts`` body chunks of ``size`` bytes."""

    async def body() -> AsyncIterator[bytes]:
        for _ in range(parts):
            yield b"x" * size

    return StreamingResponse(body(), status_code=status_code, media_type="text/plain")


@router.websocket("/ws")
async def socket(websocket: WebSocket) -> None:
    """Accept, greet, and close."""
    await websocket.accept()
    await websocket.send_text("hi")
    await websocket.close()
