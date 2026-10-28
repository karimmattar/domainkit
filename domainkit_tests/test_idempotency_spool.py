"""Keyed bodies are kept in a spool: in memory, past ``SPOOL_BYTES`` in a file.

PERF-11: a keyed request's body was read whole, joined, and held for the
whole request; a response being kept was held in parts, then joined. An
8 MiB body cost 16 MiB, and 32 MiB each way 96 MiB. The body is now
fingerprinted as it arrives and handed on part by part, and both are kept
in memory only up to ``SPOOL_BYTES``. The fingerprint, the replay, the
lease and the tombstones are 0.11's.
"""

import asyncio
import hashlib
import tempfile
import tracemalloc
from collections.abc import Iterator
from typing import IO, Any

import pytest
from starlette.types import Message, Receive, Scope, Send

from domainkit.conf import override_settings
from domainkit.contrib.idempotency import middleware as idempotency
from domainkit.db import connections
from domainkit_tests.test_ratelimit_and_idempotency import (  # noqa: F401 - fixture.
    CALLER,
    idempotent,
    stored_keys,
)

MIB = 1 << 20
PART = 64 * 1024
SPOOLED = {
    "MAX_RESPONSE_BYTES": 64 * MIB,
    "SPOOL_BYTES": MIB,
}
"""Keep responses up to 64 MiB; hold 1 MiB of each body in memory."""


@pytest.fixture
def files(monkeypatch: pytest.MonkeyPatch) -> list[IO[bytes]]:
    """The temporary files spools make, as they make them."""
    made: list[IO[bytes]] = []
    make = tempfile.TemporaryFile

    def spill() -> IO[bytes]:
        file = make()
        made.append(file)
        return file

    monkeypatch.setattr(tempfile, "TemporaryFile", spill)
    return made


@pytest.fixture
def spooling(
    request: pytest.FixtureRequest, files: list[IO[bytes]]
) -> Iterator[list[IO[bytes]]]:
    """Keyed requests of up to 64 MiB, spooled past 1 MiB; the files made."""
    request.getfixturevalue("idempotent")  # The app's settings and tables.
    with override_settings(MAX_REQUEST_BODY_SIZE=64 * MIB, IDEMPOTENCY=SPOOLED):
        yield files


def streaming(request: int, response: int) -> Any:  # noqa: ANN401 - an ASGI app.
    """An app that reads ``request`` bytes and drops them, then streams ``response``."""

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen = 0
        while True:
            message = await receive()
            seen += len(message.get("body", b""))
            if not message.get("more_body"):
                break
        assert seen == request
        start = {"type": "http.response.start", "status": 201, "headers": []}
        await send(start)
        for n in range(response // PART):
            part = bytes([n % 251]) * PART
            await send({"type": "http.response.body", "body": part, "more_body": True})
        await send({"type": "http.response.body", "body": b""})

    return app


def keyed(
    app: Any,  # noqa: ANN401 - an ASGI app.
    size: int,
    *,
    length: bool = True,
) -> tuple[int, str, int]:
    """POST ``size`` bytes in 64 KiB parts with a key: status, digest, peak bytes.

    The peak is tracemalloc's while the request ran: what the middleware,
    and the store, held at once.
    """
    parts = max(1, size // PART)

    async def scenario() -> tuple[int, str, int]:
        sent = 0
        digest = hashlib.sha256()
        statuses: list[int] = []

        async def receive() -> Message:
            nonlocal sent
            sent += 1
            if sent > parts:
                return {"type": "http.disconnect"}
            body = b"x" * (PART if size else 0)
            return {"type": "http.request", "body": body, "more_body": sent < parts}

        async def send(message: Message) -> None:
            if message["type"] == "http.response.start":
                statuses.append(message["status"])
            else:
                digest.update(message.get("body", b""))

        headers = [(b"idempotency-key", b"spooled"), CALLER]
        if length:
            headers.append((b"content-length", str(size).encode()))
        scope = {"type": "http", "method": "POST", "path": "/raw", "headers": headers}
        tracemalloc.start()
        try:
            await idempotency.IdempotencyMiddleware(app)(scope, receive, send)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
            await connections.close_all()
        return statuses[0], digest.hexdigest(), peak

    return asyncio.run(scenario())


def test_a_large_keyed_body_is_held_once_and_spooled(
    spooling: list[IO[bytes]],
) -> None:
    """8 MiB in: about the spool's 1 MiB held, where 0.11 held 16 MiB."""
    status, _, peak = keyed(streaming(8 * MIB, 0), 8 * MIB)

    assert status == 201
    assert peak < 2 * MIB
    assert len(spooling) == 1
    assert spooling[0].closed


def test_a_large_kept_response_is_held_once_and_replayed_byte_for_byte(
    spooling: list[IO[bytes]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """8 MiB in, 8 out: the value to store alone held, where 0.11 held 24 MiB.

    Measured as the response goes to the store: the driver's own copies of
    the value (asyncpg encodes it into a buffer of its own) are not the
    middleware's.
    """
    held: list[tuple[int, int]] = []
    settle = idempotency._settle

    async def measured(*args: Any) -> None:  # noqa: ANN401 - passed on as given.
        held.append(tracemalloc.get_traced_memory())
        await settle(*args)

    monkeypatch.setattr(idempotency, "_settle", measured)
    status, digest, _ = keyed(streaming(8 * MIB, 8 * MIB), 8 * MIB)
    replayed, again, _ = keyed(streaming(8 * MIB, 8 * MIB), 8 * MIB)

    assert (status, replayed) == (201, 201)
    assert again == digest
    [(current, peak)] = held
    assert MIB * 8 < current < MIB * 9  # The response to store, read back once.
    assert peak < 9 * MIB
    [row] = stored_keys()
    assert hashlib.sha256(row["body"]).hexdigest() == digest  # type: ignore[arg-type]
    assert [file.closed for file in spooling] == [True, True, True]


@pytest.mark.parametrize("length", [True, False])
@pytest.mark.parametrize("size", [0, PART, 4 * MIB])
def test_the_fingerprint_is_011s_however_the_body_comes(
    spooling: list[IO[bytes]], size: int, length: bool
) -> None:
    """Streamed with ``Content-Length``, or hashed once read, in memory or not.

    A key claimed by 0.11 (a replica not yet upgraded) still matches.
    """
    keyed(streaming(size, 0), size, length=length)

    [row] = stored_keys()
    expected = idempotency._hash("POST", "/raw", b"", b"x" * size)
    assert row["fingerprint"] == expected


def test_a_content_length_the_body_does_not_match_is_not_trusted(
    files: list[IO[bytes]],
) -> None:
    """The body as read is fingerprinted, whatever length was declared."""

    async def read(declared: bytes) -> str | None:
        incoming = [
            {"type": "http.request", "body": b"ab", "more_body": True},
            {"type": "http.request", "body": b"c"},
        ]

        async def receive() -> Message:
            return incoming.pop(0)

        scope = {
            "method": "POST",
            "path": "/raw",
            "headers": [(b"content-length", declared)],
        }
        spool = idempotency._Spool(1)
        try:
            return await idempotency._read_body(receive, scope, spool)
        finally:
            spool.close()

    expected = idempotency._hash("POST", "/raw", b"", b"abc")
    for declared in (b"2", b"9", b"3", b"-3", b"three"):
        assert asyncio.run(read(declared)) == expected


@pytest.mark.anyio
async def test_a_spool_keeps_what_it_is_given_in_memory_then_in_a_file(
    files: list[IO[bytes]],
) -> None:
    """Up to its limit in memory; past it, everything moves to one file."""
    spool = idempotency._Spool(4)
    for part in (b"ab", b"", b"cd"):
        await spool.write(part)

    assert [part async for part in spool.parts()] == [b"ab", b"cd"]
    assert await spool.read() == b"abcd"
    assert files == []
    await spool.write(b"e" * idempotency.SPOOL_PART)
    assert len(files) == 1
    # From the file, in pieces, as often as asked.
    for _ in range(2):
        pieces = [len(part) async for part in spool.parts()]
        assert pieces == [idempotency.SPOOL_PART, 4]
    assert await spool.read() == b"abcd" + b"e" * idempotency.SPOOL_PART
    assert spool.size == 4 + idempotency.SPOOL_PART
    spool.close()
    spool.close()  # Again: nothing left to close.
    assert files[0].closed


@pytest.mark.anyio
async def test_a_spooled_body_is_handed_on_in_parts_then_the_clients_messages(
    files: list[IO[bytes]],
) -> None:
    """Held in memory: one message, as 0.11 sent it. From a file: in pieces."""

    async def client() -> Message:
        return {"type": "http.disconnect"}

    async def handed(spool: idempotency._Spool) -> list[tuple[int, bool]]:
        replay = idempotency._replay(spool, client)
        messages = []
        while (message := await replay())["type"] == "http.request":
            messages.append((len(message["body"]), message["more_body"]))
        return messages

    small, large, empty = (idempotency._Spool(MIB) for _ in range(3))
    for part in (b"ab", b"c"):
        await small.write(part)
    await large.write(b"x" * (MIB + 1))

    assert await handed(small) == [(3, False)]
    pieces = await handed(large)
    assert pieces[-1] == (1, False)
    assert {more for _, more in pieces[:-1]} == {True}
    assert sum(size for size, _ in pieces) == MIB + 1
    assert await handed(empty) == [(0, False)]
    for spool in (small, large, empty):
        spool.close()
    assert [file.closed for file in files] == [True]


def test_a_client_gone_before_its_last_part_leaves_no_file(
    spooling: list[IO[bytes]],
) -> None:
    """The spool is closed, its file with it, and nothing is claimed."""
    ran: list[bool] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        ran.append(True)

    async def scenario() -> None:
        incoming = [
            {"type": "http.request", "body": b"x" * (2 * MIB), "more_body": True},
            {"type": "http.disconnect"},
        ]

        async def receive() -> Message:
            return incoming.pop(0)

        async def send(message: Message) -> None:
            raise AssertionError(message)

        headers = [(b"idempotency-key", b"gone"), CALLER]
        scope = {"type": "http", "method": "POST", "path": "/raw", "headers": headers}
        await idempotency.IdempotencyMiddleware(app)(scope, receive, send)
        await connections.close_all()

    asyncio.run(scenario())

    assert (ran, stored_keys()) == ([], [])
    assert [file.closed for file in spooling] == [True]


def test_an_application_that_raises_leaves_no_file(
    spooling: list[IO[bytes]],
) -> None:
    """Both spools are closed, and the key released, as for any request that raises."""

    async def broken(scope: Scope, receive: Receive, send: Send) -> None:
        await receive()
        start = {"type": "http.response.start", "status": 200, "headers": []}
        await send(start)
        await send(
            {"type": "http.response.body", "body": b"y" * (2 * MIB), "more_body": True}
        )
        raise RuntimeError("broke")

    with pytest.raises(RuntimeError, match="broke"):
        keyed(broken, 2 * MIB)

    assert stored_keys() == []
    assert len(spooling) == 2
    assert all(file.closed for file in spooling)
