"""``Idempotency-Key``: run a POST once, replay its response on retries.

A client sends a unique key (a random UUID) with an unsafe request. The first
request runs and its response is stored; a retry with the same key and the
same request gets the stored response with ``Idempotent-Replayed: true``. The
same key with a different request is a 422, and a retry while the first is
still running is a 409. Server errors are not stored, so they can be retried.

Keys are scoped to the authenticated actor, or to the client address for
anonymous callers. List the middleware after ``RequestContextMiddleware`` and
``RequestBodyLimitMiddleware`` (it reads the whole body).
"""

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from fastapi import status
from sqlalchemy import Delete, delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from domainkit.apps import registry
from domainkit.context import current_request
from domainkit.db import connections
from domainkit.http.errors import error_response

from .settings import IdempotencySettings
from .tables import idempotency_keys

HEADER: Final = "idempotency-key"
REPLAYED: Final = "Idempotent-Replayed"
_STORED_HEADERS: Final = frozenset({"content-type", "location", "content-language"})


def _now() -> datetime:
    """Return the current time in UTC."""
    return datetime.now(UTC)


def _hash(*parts: str | bytes) -> str:
    """Hash ``parts`` unambiguously."""
    digest = hashlib.sha256()
    for part in parts:
        data = part.encode() if isinstance(part, str) else part
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


class IdempotencyMiddleware:
    """Store and replay responses of requests carrying ``Idempotency-Key``."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app``."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Run the request once per key; replay or refuse repeats."""
        config = registry.settings(IdempotencySettings)
        key = Headers(scope=scope).get(HEADER) if scope["type"] == "http" else None
        if key is None or scope["method"] not in config.METHODS:
            await self.app(scope, receive, send)
            return
        if not (0 < len(key) <= 255 and key.isascii() and key.isprintable()):
            await error_response(
                status.HTTP_400_BAD_REQUEST,
                "Idempotency-Key must be 1 to 255 printable ASCII characters.",
                code="invalid_idempotency_key",
            )(scope, receive, send)
            return
        body, replay_receive = await _read_body(receive)
        key_hash = _hash(_caller(scope), key)
        fingerprint = _hash(
            scope["method"], scope["path"], scope.get("query_string", b""), body
        )
        existing = await self._claim(config, key_hash, fingerprint)
        if existing is not None:
            await self._answer_repeat(existing, fingerprint, scope, receive, send)
            return
        await self._run_and_store(config, key_hash, scope, replay_receive, send)

    async def _claim(
        self, config: IdempotencySettings, key_hash: str, fingerprint: str
    ) -> dict[str, Any] | None:
        """Insert an in-progress row; return the existing one if the key is taken."""
        now = _now()
        engine = connections.engine(config.DATABASE)
        async with engine.begin() as connection:
            await connection.execute(
                delete(idempotency_keys).where(
                    idempotency_keys.c.key_hash == key_hash,
                    idempotency_keys.c.expires_at < now,
                )
            )
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    insert(idempotency_keys).values(
                        key_hash=key_hash,
                        fingerprint=fingerprint,
                        created_at=now,
                        expires_at=now + timedelta(seconds=config.TTL_SECONDS),
                    )
                )
        except IntegrityError:
            async with engine.connect() as connection:
                row = (
                    await connection.execute(
                        select(idempotency_keys).where(
                            idempotency_keys.c.key_hash == key_hash
                        )
                    )
                ).first()
            return dict(row._mapping) if row is not None else {}
        return None

    async def _answer_repeat(
        self,
        existing: dict[str, Any],
        fingerprint: str,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        """Replay a finished response, or refuse a mismatch or a request in flight."""
        if existing.get("fingerprint") not in {None, fingerprint}:
            response = error_response(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "This Idempotency-Key was used for a different request.",
                code="idempotency_key_reused",
            )
        elif existing.get("status_code") is None:
            response = error_response(
                status.HTTP_409_CONFLICT,
                "A request with this Idempotency-Key is still in progress.",
                code="idempotency_key_in_use",
            )
            response.headers["Retry-After"] = "1"
        else:
            headers = [
                (name.encode(), value.encode())
                for name, value in (existing["headers"] or {}).items()
            ]
            await send(
                {
                    "type": "http.response.start",
                    "status": existing["status_code"],
                    "headers": [*headers, (REPLAYED.lower().encode(), b"true")],
                }
            )
            await send({"type": "http.response.body", "body": existing["body"] or b""})
            return
        await response(scope, receive, send)

    async def _run_and_store(
        self,
        config: IdempotencySettings,
        key_hash: str,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        """Run the request, then store its response (or release the key)."""
        started: dict[str, Any] = {}
        chunks: list[bytes] = []
        size = 0

        async def capture(message: Message) -> None:
            nonlocal size
            if message["type"] == "http.response.start":
                started.update(message)
            else:
                size += len(message.get("body", b""))
                if size <= config.MAX_RESPONSE_BYTES:
                    chunks.append(message.get("body", b""))
            await send(message)

        engine = connections.engine(config.DATABASE)
        try:
            await self.app(scope, receive, capture)
        except BaseException:
            async with engine.begin() as connection:
                await connection.execute(_release(key_hash))
            raise
        code = started.get("status", status.HTTP_500_INTERNAL_SERVER_ERROR)
        async with engine.begin() as connection:
            if code >= 500 or size > config.MAX_RESPONSE_BYTES:
                await connection.execute(_release(key_hash))
                return
            headers = {
                name.decode().lower(): value.decode()
                for name, value in started.get("headers", [])
                if name.decode().lower() in _STORED_HEADERS
            }
            await connection.execute(
                update(idempotency_keys)
                .where(idempotency_keys.c.key_hash == key_hash)
                .values(status_code=code, headers=headers, body=b"".join(chunks))
            )


def _release(key_hash: str) -> Delete:
    """Forget an unfinished key so the client may retry."""
    return delete(idempotency_keys).where(idempotency_keys.c.key_hash == key_hash)


def _caller(scope: Scope) -> str:
    """Scope keys to the actor, or to the client address for anonymous callers."""
    request = current_request()
    if request is not None and request.actor:
        return f"actor:{request.actor}"
    client = scope.get("client")
    return f"anonymous:{client[0] if client else 'unknown'}"


async def _read_body(receive: Receive) -> tuple[bytes, Receive]:
    """Read the whole body, then return a ``receive`` that replays it."""
    chunks = []
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            break
    body = b"".join(chunks)
    sent = False

    async def replay() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return body, replay
