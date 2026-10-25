"""``Idempotency-Key``: run a POST once, replay its response on retries.

A client sends a unique key (a random UUID) with an unsafe request. The first
request runs and its response is stored; a retry with the same key and the
same request gets the stored response with ``Idempotent-Replayed: true``. The
same key with a different request is a 422, and a retry while the first is
still running is a 409. A request holds its key for ``LEASE_SECONDS``; if it
dies before committing anything, a retry after the lease takes the key over
and runs (see ``operations``). A client that disconnects before sending its
whole body claims no key and runs nothing.

Leases and expiry are decided by the keys' database's clock on PostgreSQL
(``domainkit.db.dbclock``), so hosts whose clocks disagree never take a key
from a request still running, and by ``domainkit.clock`` on SQLite and
under ``freeze_time``.

A request that fails or is refused (a 4xx) before committing anything, or
that raises, releases its key, so it can be retried. A response the key
keeps is stored as soon as its last part is sent, before what the
application runs after it (``BackgroundTasks``, which no deadline bounds),
so a retry gets it however long that work takes; a key to be released is
released once the application returns. The response is stored,
or the key released, in a task of its own within ``SETTLE_TIMEOUT``, so a
request cancelled meanwhile (its client gone, a deadline, a cancel scope)
still lets go of its key instead of holding it until the lease runs out.
Once a SQL unit of work commits during the request, the key is never
released (see ``operations``): if the response cannot be kept (a server
error after the commit, an exception, a body over ``MAX_RESPONSE_BYTES``,
or one marked ``Cache-Control: no-store``, such as new tokens), a retry
gets a 409 ``idempotency_response_unavailable`` instead of running the
changes again. The same holds once the key is pinned for a commit on
another database, even if that commit raised: it may have landed.
Only commits through a unit of work are seen: call payment providers and
other services from deferred tasks, not from the handler.

Keys are scoped to the authenticated actor (set by
``AuthenticationMiddleware``, which must come first). Without an actor,
they are scoped to the client address and the request's credentials (the
``CREDENTIAL_HEADERS``, hashed), so callers behind one address never share
keys where the project authenticates on its own with one of those headers.
List the middleware after ``RequestContextMiddleware`` and
``RequestBodyLimitMiddleware`` (it reads the whole body).
"""

import asyncio
import hashlib
import logging
import secrets
from collections.abc import Callable
from datetime import timedelta
from typing import Any, Final

from fastapi import status
from sqlalchemy import Delete, Table, delete, null, select, update
from sqlalchemy.dialects import postgresql, sqlite
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from domainkit.apps import registry
from domainkit.context import current_request
from domainkit.db import connections, dbclock
from domainkit.http.errors import error_response
from domainkit.tasks import spawn

from .operations import Operation, run_operation
from .settings import IdempotencySettings
from .tables import idempotency_keys

logger = logging.getLogger(__name__)

MESSAGING: Final = "domainkit.contrib.messaging"
PURGE_BATCH: Final = 100
"""Expired keys deleted with each claim when no worker purges them hourly.

Rows another claim is deleting are skipped (``SKIP LOCKED`` on PostgreSQL),
so concurrent claims never queue behind one another's purge.
"""
SETTLE_TIMEOUT: Final = 5.0
"""Seconds to store a response or release a key, whatever befalls the request."""
SETTLE_TASK: Final = "domainkit.idempotency.settle"
HEADER: Final = "idempotency-key"
REPLAYED: Final = "Idempotent-Replayed"
_STORED_HEADERS: Final = frozenset({"content-type", "location", "content-language"})
LAPSED: Final = "lapsed"
"""Read with a held key's row: whether its lease ran out, on the database's clock."""


def _hash(*parts: str | bytes) -> str:
    """Hash ``parts`` unambiguously."""
    digest = hashlib.sha256()
    for part in parts:
        data = part.encode() if isinstance(part, str) else part
        digest.update(len(data).to_bytes(8))  # Big-endian.
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
        read = await _read_body(receive)
        if read is None:
            return  # The client left before sending its whole body: run nothing.
        body, replay_receive = read
        key_hash = _hash(_caller(scope, config.CREDENTIAL_HEADERS), key)
        fingerprint = _hash(
            scope["method"], scope["path"], scope.get("query_string", b""), body
        )
        operation = Operation(key_hash, secrets.token_hex(16))
        existing = await self._claim(config, operation, fingerprint)
        if existing is not None and not (
            _abandoned(existing, fingerprint)
            and await self._take_over(config, operation, existing["token"])
        ):
            await self._answer_repeat(existing, fingerprint, scope, receive, send)
            return
        await self._run_and_store(config, operation, scope, replay_receive, send)

    async def _claim(
        self, config: IdempotencySettings, operation: Operation, fingerprint: str
    ) -> dict[str, Any] | None:
        """Claim the key; return its row instead if another request holds it.

        One statement inserts the in-progress row, or replaces the key's row
        if that expired: an upsert that only updates an expired row. Each
        statement commits as it runs (``connections.autocommit``), so a
        claim is visible at once to every retry, and only one of them
        claims the key. Without the hourly purge, a few expired keys are
        deleted last: through PgBouncer the statements share a transaction,
        and a sweep run first would hold its locks while the upsert waited
        for another claim's, which may wait for this one's.

        The times written and compared are the database's (``dbclock``),
        and so is whether a held key's lease ran out, read with its row.
        """
        async with connections.autocommit(config.DATABASE) as connection:
            dialect = connection.dialect.name
            now = dbclock.now(dialect)
            claim = _upsert(dialect)(idempotency_keys).values(
                key_hash=operation.key_hash,
                fingerprint=fingerprint,
                created_at=now,
                expires_at=_later(now, config.TTL_SECONDS),
                token=operation.token,
                locked_until=_later(now, config.LEASE_SECONDS),
                pinned=False,
            )
            fresh = claim.excluded
            claimed = await connection.scalar(
                claim.on_conflict_do_update(
                    index_elements=[idempotency_keys.c.key_hash],
                    set_={
                        "fingerprint": fresh.fingerprint,
                        "status_code": null(),
                        "headers": null(),  # SQL NULL, not JSON's null.
                        "body": null(),
                        "created_at": fresh.created_at,
                        "expires_at": fresh.expires_at,
                        "committed_at": null(),
                        "token": fresh.token,
                        "locked_until": fresh.locked_until,
                        "pinned": False,
                    },
                    where=idempotency_keys.c.expires_at < now,
                ).returning(idempotency_keys.c.token)
            )
            row = None
            if claimed is None:
                lapsed = idempotency_keys.c.locked_until < now
                row = (
                    await connection.execute(
                        select(idempotency_keys, lapsed.label(LAPSED)).where(
                            idempotency_keys.c.key_hash == operation.key_hash
                        )
                    )
                ).first()
            if not registry.is_installed(MESSAGING):  # No hourly purge: sweep a few.
                await connection.execute(_sweep(now))
        if claimed is not None:
            return None
        return dict(row._mapping) if row is not None else {}

    async def _take_over(
        self, config: IdempotencySettings, operation: Operation, held_by: str
    ) -> bool:
        """Take the key from the request holding it as ``held_by``.

        Only while that request still holds it, unfinished, uncommitted,
        and not pinned: it may have finished, committed, or pinned the key
        since its row was read, or another retry may have taken the key
        first.
        """
        async with connections.autocommit(config.DATABASE) as connection:
            now = dbclock.now(connection.dialect.name)
            taken = await connection.execute(
                update(idempotency_keys)
                .where(
                    idempotency_keys.c.key_hash == operation.key_hash,
                    idempotency_keys.c.token == held_by,
                    idempotency_keys.c.status_code.is_(None),
                    idempotency_keys.c.committed_at.is_(None),
                    idempotency_keys.c.pinned.is_(False),
                )
                .values(
                    token=operation.token,
                    locked_until=_later(now, config.LEASE_SECONDS),
                )
            )
        return bool(taken.rowcount)

    async def _answer_repeat(
        self,
        existing: dict[str, Any],
        fingerprint: str,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        """Replay a finished response, or refuse a mismatch or a request in flight."""
        status_code = existing.get("status_code")
        if existing.get("fingerprint") not in {None, fingerprint}:
            response = error_response(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "This Idempotency-Key was used for a different request.",
                code="idempotency_key_reused",
            )
        elif status_code is None and not _died_after_commit(existing):
            response = error_response(
                status.HTTP_409_CONFLICT,
                "A request with this Idempotency-Key is still in progress.",
                code="idempotency_key_in_use",
            )
            response.headers["Retry-After"] = "1"
        elif existing["body"] is None:
            ended = "" if status_code is None else f" (status {status_code})"
            response = error_response(
                status.HTTP_409_CONFLICT,
                f"The request with this Idempotency-Key already ran{ended}, but "
                "its response was not kept; look up the result instead of "
                "retrying.",
                code="idempotency_response_unavailable",
            )
        else:
            headers = [
                (name.encode("latin-1"), value.encode("latin-1"))
                for name, value in (existing["headers"] or {}).items()
            ]
            await send(
                {
                    "type": "http.response.start",
                    "status": status_code,
                    "headers": [*headers, (REPLAYED.lower().encode(), b"true")],
                }
            )
            await send({"type": "http.response.body", "body": existing["body"] or b""})
            return
        await response(scope, receive, send)

    async def _run_and_store(
        self,
        config: IdempotencySettings,
        operation: Operation,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        """Run the request as the key's operation, then store or release the key.

        A key that keeps the response, or its status, is settled as soon as
        the response's last part is sent, before the application goes on to
        anything after it (Starlette's ``BackgroundTasks``), which no longer
        holds a deadline: a retry then gets the stored response, or a 409,
        however long that work runs, and never runs the request again. A
        key to be released is released once the application returns, as a
        commit made after the response still keeps it. If storing fails,
        the error reaches the application where it sent the response, so
        what would have run after it does not run with the key unsettled.
        """
        started: dict[str, Any] = {}
        chunks: list[bytes] = []
        size = 0
        settled = False

        def kept_body() -> bytes | None:
            """The body to store, if the response is kept; None if not."""
            code = started.get("status", status.HTTP_500_INTERNAL_SERVER_ERROR)
            kept = (
                (code < 400 or (code < 500 and operation.committed))
                and size <= config.MAX_RESPONSE_BYTES
                and not _no_store(started)
            )
            return b"".join(chunks) if kept else None

        async def capture(message: Message) -> None:
            nonlocal size, settled
            if message["type"] == "http.response.start":
                started.update(message)
                await send(message)
                return
            size += len(message.get("body", b""))
            if size <= config.MAX_RESPONSE_BYTES:
                chunks.append(message.get("body", b""))
            await send(message)
            if settled or message.get("more_body", False):
                return
            body = kept_body()
            if body is not None or operation.committed or operation.pinned:
                settled = True  # Before it runs: a cancellation must not repeat it.
                await _settle(config, operation, started, body)

        with run_operation(operation):
            try:
                await self.app(scope, receive, capture)
            except BaseException:
                if not settled:
                    await _settle(config, operation, started, None)
                raise
        if not settled:
            await _settle(config, operation, started, kept_body())


async def _settle(
    config: IdempotencySettings,
    operation: Operation,
    started: dict[str, Any],
    body: bytes | None,
) -> None:
    """``_finish`` in a task of its own, bounded by ``SETTLE_TIMEOUT``, and wait.

    The task is shielded from this request: cancelled here, even by a
    cancel scope that cancels every wait, the request leaves at once and
    the task still stores the response or releases the key, then logs what
    went wrong, since no one is left to raise it to. (``asyncio.wait``
    rather than ``asyncio.shield``, which on Python 3.14 logs that failure
    a second time.)
    """
    task = spawn(
        _finish_within(config, operation, started, body),
        name=SETTLE_TASK,
        log_errors=False,
    )
    try:
        await asyncio.wait([task])
    except asyncio.CancelledError:
        task.add_done_callback(_log_unsettled)
        raise
    task.result()  # Its failure, raised to the request.


async def _finish_within(
    config: IdempotencySettings,
    operation: Operation,
    started: dict[str, Any],
    body: bytes | None,
) -> None:
    """``_finish``, given up after ``SETTLE_TIMEOUT``: the lease then applies."""
    async with asyncio.timeout(SETTLE_TIMEOUT):
        await _finish(config, operation, started, body)


def _log_unsettled(task: asyncio.Task[None]) -> None:
    """Log why a cancelled request's key could not be settled, if it could not."""
    if not task.cancelled() and (exc := task.exception()) is not None:
        logger.error(
            "Could not store the response of a cancelled request or release its "
            "Idempotency-Key; the key is held until its lease runs out",
            exc_info=exc,
        )


async def _finish(
    config: IdempotencySettings,
    operation: Operation,
    started: dict[str, Any],
    body: bytes | None,
) -> None:
    """Store the response; without one, release the key unless it may have committed.

    ``body`` is None when the response is not kept. The release skips a key
    marked committed, which a commit that failed to report back still sets,
    and a key pinned for a commit on another database, which may have
    landed whatever it reported: that key keeps the status, without a body,
    so retries learn the request ran. Nothing is written once a retry has
    taken the key over.
    """
    async with connections.autocommit(config.DATABASE) as connection:
        if body is None and not (operation.committed or operation.pinned):
            released = await connection.execute(
                delete(idempotency_keys).where(
                    operation.holds_key(),
                    idempotency_keys.c.committed_at.is_(None),
                    idempotency_keys.c.pinned.is_(False),
                )
            )
            if released.rowcount:
                return
        headers = {  # ASGI header values are Latin-1 bytes, not always UTF-8.
            name.decode("latin-1").lower(): value.decode("latin-1")
            for name, value in started.get("headers", [])
            if name.decode("latin-1").lower() in _STORED_HEADERS
        }
        await connection.execute(
            update(idempotency_keys)
            .where(operation.holds_key())
            .values(
                status_code=started.get(
                    "status", status.HTTP_500_INTERNAL_SERVER_ERROR
                ),
                headers=headers,
                body=body,
            )
        )


def _sweep(now: dbclock.Moment) -> Delete:
    """Delete up to ``PURGE_BATCH`` expired keys, skipping those being claimed."""
    oldest = (
        select(idempotency_keys.c.key_hash)
        .where(idempotency_keys.c.expires_at < now)
        .limit(PURGE_BATCH)
        .with_for_update(skip_locked=True)
    )
    return delete(idempotency_keys).where(
        idempotency_keys.c.key_hash.in_(oldest.scalar_subquery())
    )


def _later(now: dbclock.Moment, seconds: float) -> dbclock.Moment:
    """``seconds`` after ``now``, a value or the database's clock in SQL."""
    return now + timedelta(seconds=seconds)


def _upsert(dialect: str) -> Callable[[Table], postgresql.Insert | sqlite.Insert]:
    """The dialect's ``INSERT`` with ``ON CONFLICT`` (PostgreSQL's or SQLite's)."""
    return postgresql.insert if dialect == "postgresql" else sqlite.insert


def _abandoned(existing: dict[str, Any], fingerprint: str) -> bool:
    """Whether the same request died before committing: its lease ran out."""
    return (
        existing.get("fingerprint") == fingerprint
        and existing["status_code"] is None
        and existing["committed_at"] is None
        and bool(existing[LAPSED])
    )


def _died_after_commit(existing: dict[str, Any]) -> bool:
    """Whether a committed request's lease ran out before it finished."""
    return existing.get("committed_at") is not None and bool(existing[LAPSED])


def _caller(scope: Scope, credential_headers: tuple[str, ...]) -> str:
    """Scope keys to the actor, or else to the address and the credentials."""
    request = current_request()
    if request is not None and request.actor:
        return f"actor:{request.actor}"
    client = scope.get("client")
    headers = Headers(scope=scope)
    credentials = _hash(
        *(
            part
            for name in credential_headers
            for values in (headers.getlist(name),)
            for part in (name, str(len(values)), *values)
        )
    )
    return f"anonymous:{client[0] if client else 'unknown'}:{credentials}"


def _no_store(started: dict[str, Any]) -> bool:
    """Whether the response says it must not be stored (``Cache-Control``)."""
    for name, value in started.get("headers", []):
        if name.lower() == b"cache-control":
            directives = value.decode("latin-1").lower().split(",")
            if "no-store" in (directive.strip() for directive in directives):
                return True
    return False


async def _read_body(receive: Receive) -> tuple[bytes, Receive] | None:
    """Read the whole body, then return a ``receive`` that replays it.

    None if the client disconnected first: part of a body is never replayed
    as if it were all of it.
    """
    chunks = []
    while True:
        message = await receive()
        if message["type"] != "http.request":
            return None
        chunks.append(message.get("body", b""))
        if not message.get("more_body"):
            break
    body = b"".join(chunks)
    pending: list[Message] = [
        {"type": "http.request", "body": body, "more_body": False}
    ]

    async def replay() -> Message:
        return pending.pop() if pending else await receive()

    return body, replay
