"""Uploads scanned for viruses as they are stored, and thumbnails of images."""

import asyncio
import io
import struct
from collections.abc import AsyncIterator

import pytest
from PIL import Image
from starlette.datastructures import UploadFile

from domainkit.conf import override_settings
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.uploads import IMAGES, TEXT, UploadRefused, save_upload
from domainkit.storage import FileMissing, MemoryStorage
from domainkit.storage import images as images_module
from domainkit.storage.images import NotAnImage, thumbnail
from domainkit.storage.scanning import (
    ClamAVScanner,
    Infected,
    ScannerUnavailable,
    configured_scanner,
)

pytestmark = pytest.mark.anyio

EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


async def fake_clamd(
    reply: bytes | None = None,
) -> tuple[asyncio.Server, int, list[bytes]]:
    """A clamd that reads INSTREAM, and finds EICAR (or answers ``reply``)."""
    received: list[bytes] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        assert await reader.readexactly(10) == b"zINSTREAM\0"
        data = b""
        try:
            while size := struct.unpack("!L", await reader.readexactly(4))[0]:
                data += await reader.readexactly(size)
        except asyncio.IncompleteReadError:  # The client went away mid-stream.
            writer.close()
            return
        received.append(data)
        verdict = reply or (
            b"stream: Eicar-Signature FOUND\0" if EICAR in data else b"stream: OK\0"
        )
        writer.write(verdict)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port, received


def text_upload(data: bytes) -> UploadFile:
    """A text file as a client sends it."""
    return UploadFile(io.BytesIO(data), filename="notes.txt", size=len(data))


async def test_a_clean_upload_is_scanned_and_kept() -> None:
    """Every byte goes to clamd as the storage writes it; OK keeps the file."""
    server, port, received = await fake_clamd()
    storage = MemoryStorage()
    scanner = {"BACKEND": "domainkit.storage.scanning.ClamAVScanner"}
    async with server:
        with override_settings(
            UPLOAD_SCANNER={**scanner, "OPTIONS": {"host": "127.0.0.1", "port": port}}
        ):
            stored = await save_upload(
                text_upload(b"hello"), types=[TEXT], max_size=100, storage=storage
            )

    assert received == [b"hello"]
    assert storage.files == {stored.name: b"hello"}


async def test_an_infected_upload_is_refused_and_never_kept(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """422 ``infected_file``; the finding is logged, not told to the client."""
    server, port, _ = await fake_clamd()
    storage = MemoryStorage()
    async with server:
        with (
            override_settings(
                UPLOAD_SCANNER={"OPTIONS": {"host": "127.0.0.1", "port": port}}
            ),
            pytest.raises(UploadRefused) as refused,
        ):
            await save_upload(
                text_upload(EICAR), types=[TEXT], max_size=1000, storage=storage
            )

    assert (refused.value.status_code, refused.value.code) == (422, "infected_file")
    assert "Eicar" not in str(refused.value.detail)
    assert "Eicar-Signature" in caplog.text
    assert storage.files == {}


async def test_a_scanner_that_cannot_scan_refuses_the_upload() -> None:
    """Unreachable, or an ERROR verdict: 503, nothing kept, never unscanned."""
    server, port, _ = await fake_clamd(
        reply=b"stream: INSTREAM size limit exceeded. ERROR\0"
    )
    closed = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    nobody = closed.sockets[0].getsockname()[1]
    closed.close()
    await closed.wait_closed()

    async def one_chunk() -> AsyncIterator[bytes]:
        yield b"data"

    async with server:
        with pytest.raises(ScannerUnavailable):
            async for _ in ClamAVScanner(host="127.0.0.1", port=port).scan(one_chunk()):
                pass
    with pytest.raises(ScannerUnavailable):
        async for _ in ClamAVScanner(host="127.0.0.1", port=nobody).scan(one_chunk()):
            pass
    assert ScannerUnavailable.status_code == 503


async def test_a_silent_clamd_is_unavailable() -> None:
    """A connection closed before the verdict."""

    async def hang_up(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await reader.readexactly(10)
        writer.close()

    server = await asyncio.start_server(hang_up, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    async def nothing() -> AsyncIterator[bytes]:
        return
        yield b""

    async with server:
        with pytest.raises(ScannerUnavailable):
            async for _ in ClamAVScanner(host="127.0.0.1", port=port, timeout=2).scan(
                nothing()
            ):
                pass


def test_the_configured_scanner_is_made_once_and_checked() -> None:
    """None unless set; the same instance while the settings hold; bad ones refused."""
    assert configured_scanner() is None
    with override_settings(UPLOAD_SCANNER={"OPTIONS": {"host": "clamav"}}):
        first = configured_scanner()
        assert isinstance(first, ClamAVScanner)
        assert configured_scanner() is first
    memory = {"BACKEND": "domainkit.storage.MemoryStorage"}
    with (
        override_settings(UPLOAD_SCANNER=memory),
        pytest.raises(ImproperlyConfigured, match="is not a Scanner"),
    ):
        configured_scanner()
    with (
        override_settings(UPLOAD_SCANNER={"OPTIONS": {"colour": "red"}}),
        pytest.raises(ImproperlyConfigured, match="UPLOAD_SCANNER"),
    ):
        configured_scanner()
    assert Infected("X").signature == "X"


def png(width: int, height: int) -> bytes:
    """A PNG image of ``width`` by ``height``."""
    out = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(out, format="PNG")
    return out.getvalue()


async def test_a_thumbnail_fits_its_size_and_keeps_the_proportions() -> None:
    """800 by 400 into 256 by 256: 256 by 128, stored as WebP beside the image."""
    storage = MemoryStorage()
    name = await storage.save(png(800, 400), extension=".png")

    small = await thumbnail(storage, name, size=(256, 256))
    jpeg = await thumbnail(storage, name, size=(100, 100), format="jpeg")

    assert small.startswith("thumbnails/")
    assert small.endswith(".webp")
    with Image.open(io.BytesIO(storage.files[small])) as image:
        assert (image.format, image.size) == ("WEBP", (256, 128))
    with Image.open(io.BytesIO(storage.files[jpeg])) as image:
        assert (image.format, image.size) == ("JPEG", (100, 50))


async def test_thumbnails_refuse_what_is_not_a_small_enough_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not an image, too many pixels, too many bytes, odd options, no file."""
    storage = MemoryStorage()
    text = await storage.save(b"not an image")
    image = await storage.save(png(40, 30))

    with pytest.raises(NotAnImage, match="not an image"):
        await thumbnail(storage, text)
    monkeypatch.setattr(images_module, "MAX_PIXELS", 1000)
    with pytest.raises(NotAnImage, match="too many pixels"):
        await thumbnail(storage, image)
    monkeypatch.setattr(images_module, "MAX_SOURCE_BYTES", 10)
    with pytest.raises(NotAnImage, match="too large"):
        await thumbnail(storage, image)
    with pytest.raises(ValueError, match="one of"):
        await thumbnail(storage, image, format="gif")
    with pytest.raises(ValueError, match="1 to 4096"):
        await thumbnail(storage, image, size=(0, 10))
    with pytest.raises(FileMissing):
        await thumbnail(storage, "missing.png")
    assert IMAGES


async def test_a_clamd_that_stops_reading_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A write clamd no longer takes fails the scan, not the process."""
    server, port, _ = await fake_clamd()

    async def broken(self: asyncio.StreamWriter) -> None:
        raise ConnectionResetError("clamd went away")

    async def one_chunk() -> AsyncIterator[bytes]:
        yield b"data"

    monkeypatch.setattr(asyncio.StreamWriter, "drain", broken)
    async with server:
        with pytest.raises(ScannerUnavailable):
            async for _ in ClamAVScanner(host="127.0.0.1", port=port).scan(one_chunk()):
                pass
