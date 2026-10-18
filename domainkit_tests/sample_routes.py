"""Routes exercising the middleware and error handling."""

from collections.abc import AsyncIterator
from typing import Final

from fastapi import APIRouter, HTTPException, Request, Response, WebSocket
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Column, Integer, MetaData, Table, insert, update
from sqlalchemy.ext.asyncio import AsyncSession

from domainkit.context import current_request
from domainkit.contrib.idempotency.operations import running_operation
from domainkit.contrib.idempotency.tables import idempotency_keys
from domainkit.db import SqlAlchemyUnitOfWork, connections
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
async def respond(
    status_code: int = 200, size: int = 0, parts: int = 1, cache: str | None = None
) -> Response:
    """Answer with ``status_code`` and ``parts`` body chunks of ``size`` bytes."""

    async def body() -> AsyncIterator[bytes]:
        for _ in range(parts):
            yield b"x" * size

    headers = {"Cache-Control": cache} if cache else None
    return StreamingResponse(
        body(), status_code=status_code, media_type="text/plain", headers=headers
    )


payments_metadata = MetaData()
payments = Table(
    "framework_test_payments",
    payments_metadata,
    Column("id", Integer, primary_key=True),
)


class PaymentsUnitOfWork(SqlAlchemyUnitOfWork):
    """Records payments through its session."""

    def create_repositories(self, session: AsyncSession) -> None:
        """None: the route inserts rows itself."""


class UnreportedCommit(PaymentsUnitOfWork):
    """Commits, then fails as if the reply to COMMIT was lost."""

    async def _commit(self) -> None:
        await super()._commit()
        raise ConnectionError("The reply to COMMIT was lost.")


class RejectedCommit(PaymentsUnitOfWork):
    """Fails to commit after every domain staged its rows."""

    async def _commit(self) -> None:
        await self._rollback()
        raise ConnectionError("The database refused the commit.")


UNITS_OF_WORK: Final = {"unreported": UnreportedCommit, "rejected": RejectedCommit}


async def lose_key() -> None:
    """Hand the running request's key to another token, as a retry would."""
    operation = running_operation()
    assert operation is not None
    async with connections.engine().begin() as connection:
        await connection.execute(
            update(idempotency_keys)
            .where(idempotency_keys.c.key_hash == operation.key_hash)
            .values(token="retry")
        )


@router.post("/pay", status_code=201)
async def pay(outcome: str = "ok", database: str = "default") -> Response:
    """Record a payment on ``database``, then answer as ``outcome`` says."""
    if outcome.startswith("overtaken"):
        await lose_key()
    if outcome == "overtaken-crash":
        raise RuntimeError("crashed before the commit")
    async with UNITS_OF_WORK.get(outcome, PaymentsUnitOfWork)(database=database) as uow:
        await uow.session.execute(insert(payments))
        await uow.commit()
    if outcome == "crash":
        raise RuntimeError("crashed after the commit")
    if outcome == "unavailable":
        raise HTTPException(503, "Try later.")
    if outcome == "failed":
        return Response(b"failed", status_code=500)
    if outcome == "declined":
        return Response(b"declined", status_code=402)
    if outcome == "secret":
        return Response(
            b"token", status_code=201, headers={"Cache-Control": "no-store"}
        )
    return Response(b"x" * 2048 if outcome == "large" else b"paid", status_code=201)


@router.websocket("/ws")
async def socket(websocket: WebSocket) -> None:
    """Accept, greet, and close."""
    await websocket.accept()
    await websocket.send_text("hi")
    await websocket.close()


@router.websocket("/ws/context")
async def socket_context(websocket: WebSocket) -> None:
    """Accept, then report the request context the connection runs in."""
    await websocket.accept()
    request = current_request()
    await websocket.send_json(
        None
        if request is None
        else {
            "request_id": request.request_id,
            "method": request.method,
            "path": request.path,
            "client_ip": request.client_ip,
        }
    )
    await websocket.close()


@router.websocket("/ws/refused")
async def refused_socket(websocket: WebSocket) -> None:
    """Close without accepting: the handshake is refused."""
    await websocket.close()


@router.websocket("/ws/crash")
async def crashing_socket(websocket: WebSocket) -> None:
    """Fail before accepting."""
    raise RuntimeError("socket crashed")
