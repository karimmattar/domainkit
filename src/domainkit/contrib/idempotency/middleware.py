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
another database, even if that commit raised: it may have landed. Past
``TTL_SECONDS`` such a key's response is dropped, but the key is kept for
``TOMBSTONE_SECONDS`` more, as a tombstone: its retries get the 409 too,
and never run again (FIN-84).
Only commits through a unit of work are seen: call payment providers and
other services from deferred tasks, not from the handler.

Keys are scoped to the authenticated actor (set by
``AuthenticationMiddleware``, which must come first), and to the scopes its
credentials are limited to, if any: a token limited to scopes never shares
its user's keys. Without an actor, they are scoped to the client address
and the request's credentials (the ``CREDENTIAL_HEADERS``, hashed), so
callers behind one address never share keys where the project
authenticates on its own with one of those headers; once an
authentication middleware found no one in the request, those headers name
nobody.
A key sent with neither is answered 400 (``ANONYMOUS = "refuse"``, the
default); ``"per-address"`` shares it with every caller behind the address.
List the middleware after ``RequestContextMiddleware`` and
``RequestBodyLimitMiddleware`` (it reads the whole body).

A keyed request's body is hashed as it arrives, when ``Content-Length``
gives its length, and kept to hand to the application, part by part: in
memory up to ``SPOOL_BYTES``, past them in a temporary file. So is a
response being kept, until it is stored (PERF-11, ADR 0105).
"""

import asyncio
import hashlib
import logging
import secrets
import tempfile
import threading
from collections import deque
from collections.abc import AsyncIterator, Iterable
from typing import IO, Any, Final

from fastapi import status
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from domainkit import clock
from domainkit.apps import registry
from domainkit.context import current_request
from domainkit.db import connections
from domainkit.ddd.personal import SEPARATOR, subject_tag
from domainkit.http.errors import error_response
from domainkit.tasks import spawn

from . import statements
from .operations import Operation, purge, run_operation
from .settings import REFUSE, IdempotencySettings
from .statements import RELEASE, STORE, clock_statements, holder

__all__ = [
    "IdempotencyMiddleware",
]

logger = logging.getLogger(__name__)

MESSAGING: Final = "domainkit.contrib.messaging"
PURGE_BATCH: Final = 100
"""Expired keys one sweep deletes when no worker purges them hourly.

Rows another claim is deleting are skipped (``SKIP LOCKED`` on PostgreSQL),
so concurrent claims never queue behind one another's purge.
"""
SWEEP_EVERY: Final = 60.0
"""Seconds between sweeps of expired keys by one middleware, once they are few.

Without the hourly purge, a claim sweeps when this long has passed since
the last sweep, or when the last one deleted a whole ``PURGE_BATCH`` (more
may be waiting): a backlog drains as fast as when every claim swept, and
then costs a statement a minute instead of one per keyed request (PERF-10).
"""
SETTLE_TIMEOUT: Final = 5.0
"""Seconds to store a response or release a key, whatever befalls the request."""
SETTLE_TASK: Final = "domainkit.idempotency.settle"
HEADER: Final = "idempotency-key"
REPLAYED: Final = "Idempotent-Replayed"
_STORED_HEADERS: Final = frozenset({"content-type", "location", "content-language"})
UNSEEN_BODIES: Final = frozenset(
    {"http.response.pathsend", "http.response.zerocopysend"}
)
"""ASGI messages that send a body the middleware never sees, so cannot keep."""
LAPSED: Final = statements.LAPSED
"""Read with a held key's row: whether its lease ran out, on the database's clock."""
EXPIRED: Final = statements.EXPIRED
"""Read with a held key's row: whether it expired, on the database's clock."""


SPOOL_PART: Final = 65_536
"""Bytes read back from a spool's file at a time."""


def _digest(*parts: str | bytes) -> "hashlib._Hash":
    """A SHA-256 of ``parts``, each after its length, so none can run into the next."""
    digest = hashlib.sha256()
    for part in parts:
        data = part.encode() if isinstance(part, str) else part
        digest.update(len(data).to_bytes(8))  # Big-endian.
        digest.update(data)
    return digest


def _hash(*parts: str | bytes) -> str:
    """Hash ``parts`` unambiguously."""
    return _digest(*parts).hexdigest()


class _Spool:
    """Bytes kept in memory up to ``limit``; past it, all of them in a file.

    The file is a ``tempfile.TemporaryFile``: unlinked from the start, so
    only this process can read it and it goes when closed. It is written
    and read on a thread: the loop never waits on the disk.
    """

    def __init__(self, limit: int) -> None:
        """Keep up to ``limit`` bytes in memory."""
        self.limit = limit
        self.size = 0
        """Bytes written so far."""
        self._parts: deque[bytes] = deque()
        self._file: IO[bytes] | None = None

    async def write(self, data: bytes) -> None:
        """Keep ``data`` after what was written before."""
        if not data:
            return
        self.size += len(data)
        if self._file is None and self.size <= self.limit:
            self._parts.append(data)
            return
        if self._file is None:
            self._file = await asyncio.to_thread(_spill, self._parts)
            self._parts = deque()
        await asyncio.to_thread(self._file.write, data)

    async def parts(self) -> AsyncIterator[bytes]:
        """What was written, in order: as written, or from the file in pieces."""
        if self._file is None:
            for part in list(self._parts):
                yield part
            return
        async for piece in self._pieces(self._file):
            yield piece

    async def drain(self) -> AsyncIterator[bytes]:
        """What was written, to hand on: letting go of what memory held.

        Held in memory, it comes whole, in one part, as 0.11 handed a body
        on; from a file, in ``SPOOL_PART`` pieces.
        """
        if self._file is None:
            whole = b"".join(self._parts)
            self._parts = deque()
            yield whole
            return
        async for piece in self._pieces(self._file):
            yield piece

    @staticmethod
    async def _pieces(file: IO[bytes]) -> AsyncIterator[bytes]:
        """``file`` from its start, in ``SPOOL_PART`` pieces, read on a thread."""
        await asyncio.to_thread(file.seek, 0)
        while piece := await asyncio.to_thread(file.read, SPOOL_PART):
            yield piece

    async def read(self) -> bytes:
        """Everything written, as one value."""
        if self._file is None:
            return b"".join(self._parts)
        return await asyncio.to_thread(_read_all, self._file)

    def close(self) -> None:
        """Let go of what is held; the file, if any, is deleted."""
        self._parts = deque()
        if self._file is not None:
            self._file.close()
            self._file = None


def _spill(parts: Iterable[bytes]) -> IO[bytes]:
    """A new temporary file holding ``parts``."""
    file = tempfile.TemporaryFile()  # noqa: SIM115 - the spool closes it.
    for part in parts:
        file.write(part)
    return file


def _read_all(file: IO[bytes]) -> bytes:
    """All of ``file``, from its start."""
    file.seek(0)
    return file.read()


class Sweeps:
    """When a claim sweeps expired keys, for one middleware (``SWEEP_EVERY``).

    Shared by every loop and thread that serves the middleware's
    application, so it takes a lock; time is ``domainkit.clock``'s.
    """

    def __init__(self) -> None:
        """Let the first claim sweep."""
        self._lock = threading.Lock()
        self._next = float("-inf")

    def due(self) -> bool:
        """Whether this claim sweeps; if so, the next ones wait ``SWEEP_EVERY``."""
        with self._lock:
            now = clock.monotonic()
            if now < self._next:
                return False
            self._next = now + SWEEP_EVERY
            return True

    def swept(self, deleted: int) -> None:
        """Let the next claim sweep at once if this sweep found a whole batch."""
        if deleted >= PURGE_BATCH:
            with self._lock:
                self._next = float("-inf")


class IdempotencyMiddleware:
    """Store and replay responses of requests carrying ``Idempotency-Key``."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app``."""
        self.app = app
        self.sweeps = Sweeps()

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
        if config.ANONYMOUS == REFUSE and _unnamed(scope, config):
            await error_response(
                status.HTTP_400_BAD_REQUEST,
                "An Idempotency-Key needs a caller this service knows: "
                "authenticate, or send the request without the key.",
                code="idempotency_key_needs_caller",
            )(scope, receive, send)
            return
        body = _Spool(config.SPOOL_BYTES)
        try:
            fingerprint = await _read_body(receive, scope, body)
            if fingerprint is None:
                return  # The client left before sending its whole body: run nothing.
            request = current_request()
            actor = request.actor if request is not None else None
            key_hash = _hash(_caller_of(actor, scope, config.CREDENTIAL_HEADERS), key)
            operation = Operation(key_hash, secrets.token_hex(16), actor=actor or None)
            existing = await self._claim(config, operation, fingerprint)
            if existing is not None and not (
                _abandoned(existing, fingerprint)
                and await self._take_over(config, operation, existing["token"])
            ):
                await self._answer_repeat(existing, fingerprint, scope, receive, send)
                return
            await self._run_and_store(
                config, operation, scope, _replay(body, receive), send
            )
        finally:
            body.close()

    async def _claim(
        self, config: IdempotencySettings, operation: Operation, fingerprint: str
    ) -> dict[str, Any] | None:
        """Claim the key; return its row instead if another request holds it.

        One statement inserts the in-progress row, or replaces the key's row
        if that expired: an upsert that only updates an expired row. Each
        statement commits as it runs (``connections.autocommit``), so a
        claim is visible at once to every retry, and only one of them
        claims the key. Without the hourly purge, a few expired keys are
        deleted last, when a sweep is due (``Sweeps``): through PgBouncer
        the statements share a transaction, and a sweep run first would
        hold its locks while the upsert waited for another claim's, which
        may wait for this one's.

        The times written and compared are the database's (``dbclock``),
        and so is whether a held key's lease ran out, read with its row.
        The statements are built once (``statements``); only the values are
        the request's.
        """
        async with connections.autocommit(config.DATABASE) as connection:
            built = clock_statements(connection.dialect.name)
            now = built.now()
            claimed = await connection.scalar(
                built.claim if config.TOMBSTONE_SECONDS else built.claim_forgetting,
                built.claim_values(
                    now,
                    operation.key_hash,
                    fingerprint,
                    operation.token,
                    config.TTL_SECONDS,
                    config.LEASE_SECONDS,
                ),
            )
            row = None
            if claimed is None:
                row = (
                    await connection.execute(
                        built.read_held,
                        built.read_held_values(now, operation.key_hash),
                    )
                ).first()
            if not registry.is_installed(MESSAGING) and self.sweeps.due():
                # No hourly purge: sweep a few now and then.
                self.sweeps.swept(
                    await purge(
                        connection, built, now, config.TOMBSTONE_SECONDS, PURGE_BATCH
                    )
                )
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
            built = clock_statements(connection.dialect.name)
            taken = await connection.execute(
                built.take_over,
                built.take_over_values(
                    built.now(),
                    operation.key_hash,
                    held_by,
                    operation.token,
                    config.LEASE_SECONDS,
                ),
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
        """Replay a finished response, or refuse a mismatch or a request in flight.

        A key past its TTL whose request may have committed is not replayed,
        nor run again (FIN-84): its response is gone, or about to go.
        """
        status_code = existing.get("status_code")
        if existing.get("fingerprint") not in {None, fingerprint}:
            response = error_response(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "This Idempotency-Key was used for a different request.",
                code="idempotency_key_reused",
            )
        elif _buried(existing):
            response = error_response(
                status.HTTP_409_CONFLICT,
                "The request with this Idempotency-Key already ran, and its "
                "response is no longer kept; look up the result instead of "
                "retrying.",
                code="idempotency_response_unavailable",
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

        A key that keeps the response, or its status, is settled before the
        response's last part is sent: a client never holds a whole response
        that a retry would find in progress, and the application goes on to
        what runs after it (Starlette's ``BackgroundTasks``, which no longer
        holds a deadline) only once the key is settled, so a retry gets the
        stored response, or a 409, however long that work runs, and never
        runs the request again. A key to be released is released once the
        application returns, as a commit made after the response still
        keeps it. If storing fails, the last part is not sent: the client
        has no complete answer the key cannot stand behind, and the error
        reaches the application where it sent that part, so what would have
        run after it does not run with the key unsettled.

        If the application then raises, the response it sent no longer
        says what happened: the key is settled again as for any request
        that raised, so it is released unless something committed or
        pinned it, and a retry runs the work that failed. A body sent by
        path or by file descriptor (``http.response.pathsend``,
        ``http.response.zerocopysend``) is never seen, so it is not kept.
        """
        started: dict[str, Any] = {}
        kept = _Spool(config.SPOOL_BYTES)  # The body, up to MAX_RESPONSE_BYTES.
        size = 0
        settled = False  # A settle began: never begin another while it may run.
        stored = False  # The early settle finished.
        unseen = False  # Part of the body went by path or file descriptor.

        async def kept_body() -> bytes | None:
            """The body to store, if the response is kept; None if not."""
            code = started.get("status", status.HTTP_500_INTERNAL_SERVER_ERROR)
            keep = (
                (code < 400 or (code < 500 and operation.committed))
                and size <= config.MAX_RESPONSE_BYTES
                and not unseen
                and not _no_store(started)
            )
            return await kept.read() if keep else None

        async def capture(message: Message) -> None:
            nonlocal size, settled, stored, unseen
            if message["type"] == "http.response.start":
                started.update(message)
                await send(message)
                return
            unseen = unseen or message["type"] in UNSEEN_BODIES
            part = message.get("body", b"")
            size += len(part)
            if size <= config.MAX_RESPONSE_BYTES:
                await kept.write(part)
            if not (settled or message.get("more_body", False)):
                body = await kept_body()
                if body is not None or operation.committed or operation.pinned:
                    settled = True  # Before it runs: a cancellation must not repeat it.
                    await _settle(config, operation, started, body)
                    stored = True
            await send(message)

        try:
            with run_operation(operation):
                try:
                    await self.app(scope, receive, capture)
                except BaseException:
                    if stored or not settled:
                        await _settle(config, operation, started, None)
                    raise
            if not settled:
                await _settle(config, operation, started, await kept_body())
        finally:
            kept.close()


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
    held = holder(operation.key_hash, operation.token)
    # Whom a kept response may be about (``subjects``): the actor, first,
    # then the subjects of the events it committed (``subject_tag``):
    # ``<US>actor<US><US>ann<US>``, the actor empty when there is none (or
    # it holds the separator). ``tagged_with`` finds either; an export
    # gives the body only to the actor (``export_subject``). The actor
    # alone, the common case, takes no call on the request's path.
    actor = operation.actor
    if body is None or (actor is None and not operation.subjects):
        tag = None
    else:
        caller = actor if actor is not None and SEPARATOR not in actor else ""
        others = subject_tag(operation.subjects) if operation.subjects else None
        tag = f"{SEPARATOR}{caller}{SEPARATOR}{others or SEPARATOR}"
    async with connections.autocommit(config.DATABASE) as connection:
        if body is None and not (operation.committed or operation.pinned):
            released = await connection.execute(RELEASE, held)
            if released.rowcount:
                return
        headers = {  # ASGI header values are Latin-1 bytes, not always UTF-8.
            name.decode("latin-1").lower(): value.decode("latin-1")
            for name, value in started.get("headers", [])
            if name.decode("latin-1").lower() in _STORED_HEADERS
        }
        await connection.execute(
            STORE,
            {
                "status_code": started.get(
                    "status", status.HTTP_500_INTERNAL_SERVER_ERROR
                ),
                "headers": headers,
                "body": body,
                "subjects": tag,
                **held,
            },
        )


def _abandoned(existing: dict[str, Any], fingerprint: str) -> bool:
    """Whether the same request died before committing: its lease ran out."""
    return (
        existing.get("fingerprint") == fingerprint
        and existing["status_code"] is None
        and existing["committed_at"] is None
        and bool(existing[LAPSED])
    )


def _buried(existing: dict[str, Any]) -> bool:
    """Whether the key is a tombstone, or expired after it may have committed.

    The purge makes such a key a tombstone; until it runs, the claim leaves
    it alone, and the response it still holds is past its TTL.
    """
    if existing.get("tombstone"):
        return True
    may_have_committed = existing.get("committed_at") is not None or bool(
        existing.get("pinned")
    )
    return may_have_committed and bool(existing.get(EXPIRED))


def _died_after_commit(existing: dict[str, Any]) -> bool:
    """Whether a committed request's lease ran out before it finished."""
    return existing.get("committed_at") is not None and bool(existing[LAPSED])


def _caller(scope: Scope, credential_headers: tuple[str, ...]) -> str:
    """Scope keys to the actor, or else to the address and the credentials."""
    request = current_request()
    actor = request.actor if request is not None else None
    return _caller_of(actor, scope, credential_headers)


def _caller_of(
    actor: str | None, scope: Scope, credential_headers: tuple[str, ...]
) -> str:
    """``_caller``, for a request whose ``actor`` was read already.

    An actor's credentials limited to scopes (``scope["auth"].scopes``) are
    a caller apart, one per set of scopes: the middleware replays before
    the route's rules run, so a token limited to scopes must never be
    replayed what the actor's other credentials were answered (SEC-11a-2).
    Credentials without a limit (a login, a refreshed token) share the
    actor's keys, as before.
    """
    if actor:
        scopes: frozenset[str] | None = getattr(scope.get("auth"), "scopes", None)
        if scopes is None:
            return f"actor:{actor}"
        return f"actor:{actor}:scopes:{_hash(*sorted(scopes))}"
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
    host = (client[0] or "unknown") if client else "unknown"  # "": a unix socket.
    return f"anonymous:{host}:{credentials}"


def _unnamed(scope: Scope, config: IdempotencySettings) -> bool:
    """Whether nothing in the request names its caller: its key would be shared.

    Named: an actor, one of ``CREDENTIAL_HEADERS``, or, for ``cookie``, a
    cookie of ``SESSION_COOKIES``. Any other cookie (consent, locale) many
    callers send alike, so it scopes a key to them all (SEC-10b-7).

    A credential header names a caller only if no authentication
    middleware judged the request, or one accepted it: where one put a
    principal who is not authenticated in ``scope["user"]``, the header
    authenticated nobody. ``Authorization: Bearer null``, which logged-out
    browser applications send, or a gateway's fixed ``X-API-Key``, is the
    same for many callers (SEC-11a-1). ``SESSION_COOKIES`` are named by the
    project, for a layer of its own, so they hold whatever it found.
    """
    request = current_request()
    if request is not None and request.actor:
        return False
    headers = Headers(scope=scope)
    nobody = "user" in scope and not getattr(scope["user"], "is_authenticated", True)
    for name in config.CREDENTIAL_HEADERS:
        if name.lower() != "cookie":
            if not nobody and name in headers:
                return False
        elif _session_cookie(headers.getlist("cookie"), config.SESSION_COOKIES):
            return False
    return True


def _session_cookie(values: list[str], names: tuple[str, ...]) -> bool:
    """Whether the ``Cookie`` headers carry a cookie named in ``names``."""
    return any(
        part.split("=", 1)[0].strip() in names
        for value in values
        for part in value.split(";")
    )


def _no_store(started: dict[str, Any]) -> bool:
    """Whether the response says it must not be stored (``Cache-Control``)."""
    for name, value in started.get("headers", []):
        if name.lower() == b"cache-control":
            directives = value.decode("latin-1").lower().split(",")
            if "no-store" in (directive.strip() for directive in directives):
                return True
    return False


async def _read_body(receive: Receive, scope: Scope, body: _Spool) -> str | None:
    """Read the whole body into ``body``; return the request's fingerprint.

    The fingerprint hashes the method, the path, the query, and the body,
    each after its length, as 0.11 did, so keys claimed before an upgrade
    still match. With ``Content-Length`` the body's length is known first,
    so the body is hashed as it arrives; without it, or if the body was
    not that long, it is hashed from ``body`` once read. None if the client
    disconnected first: part of a body is never taken for all of it.
    """
    start = _digest(scope["method"], scope["path"], scope.get("query_string", b""))
    declared = _content_length(scope)
    streaming = None
    if declared is not None:
        streaming = start.copy()
        streaming.update(declared.to_bytes(8))
    while True:
        message = await receive()
        if message["type"] != "http.request":
            return None
        part = message.get("body", b"")
        await body.write(part)
        if streaming is not None:
            streaming.update(part)
        if not message.get("more_body"):
            break
    if streaming is not None and body.size == declared:
        return streaming.hexdigest()
    start.update(body.size.to_bytes(8))
    async for part in body.parts():
        start.update(part)
    return start.hexdigest()


def _content_length(scope: Scope) -> int | None:
    """The body's length as ``Content-Length`` gives it; None if it gives none.

    ASGI header names are lower case; the first such header counts.
    """
    for name, value in scope.get("headers", ()):
        if name == b"content-length":
            return int(value) if value.isdigit() else None
    return None


def _replay(body: _Spool, receive: Receive) -> Receive:
    """A ``receive`` that hands the application ``body``, then the client's own.

    A body held in memory goes in one message, as 0.11 sent it; one in a
    file, in parts, the last saying there is no more. An empty body is one
    empty message.
    """
    parts = body.drain()
    ahead: list[bytes] = []
    done = False

    async def replay() -> Message:
        nonlocal done
        if done:
            return await receive()
        part = ahead.pop() if ahead else await anext(parts, b"")
        following = await anext(parts, None)
        if following is None:
            done = True
        else:
            ahead.append(following)
        return {"type": "http.request", "body": part, "more_body": not done}

    return replay
