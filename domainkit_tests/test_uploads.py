"""Uploads: types from content, sizes while streaming, names never trusted."""

import io
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile

from domainkit.conf import override_settings
from domainkit.http import get_asgi_application
from domainkit.http.access import Require, open_routes
from domainkit.http.uploads import (
    GIF,
    IMAGES,
    JPEG,
    PDF,
    PNG,
    TEXT,
    WEBP,
    ZIP,
    StoredUpload,
    UploadRefused,
    _head,
    content_disposition,
    sanitize_filename,
    save_upload,
    serve,
    sniff,
)
from domainkit.storage import FileMissing, MemoryStorage, storages
from domainkit.storage.base import CHUNK_SIZE

pytestmark = pytest.mark.anyio

PNG_BYTES: Final = b"\x89PNG\r\n\x1a\n" + b"\0" * 100
PDF_BYTES: Final = b"%PDF-1.7\n" + b"x" * 100
MIDDLEWARE: Final = [
    "domainkit.http.locale.LocaleMiddleware",
    "domainkit.http.middleware.SecurityHeadersMiddleware",
    "domainkit.http.middleware.RequestContextMiddleware",
]


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """The sample routes, storing uploads under ``tmp_path``."""
    storages.reset()
    with override_settings(
        ROOT_ROUTER="domainkit_tests.sample_platform_routes.router",
        MIDDLEWARE=MIDDLEWARE,
        STORAGES={"uploads": {"OPTIONS": {"location": str(tmp_path)}}},
    ):
        yield TestClient(get_asgi_application())
    storages.reset()


def upload(
    client: TestClient,
    content: bytes,
    filename: str = "photo.png",
    content_type: str = "image/png",
) -> tuple[int, dict[str, object]]:
    """Post ``content`` as a file; the status and the body."""
    response = client.post(
        "/uploads", files={"file": (filename, content, content_type)}
    )
    body: dict[str, object] = response.json()
    return response.status_code, body


def stored_files(root: Path) -> list[Path]:
    """The files the storage holds."""
    return [path for path in root.rglob("*") if path.is_file()]


# Receiving ---------------------------------------------------------------------------


def test_uploads_are_stored_under_new_names(client: TestClient, tmp_path: Path) -> None:
    """The type comes from the content; the client's name is only cleaned."""
    status, body = upload(client, PNG_BYTES, filename="../../me\u202egnp.exe")

    assert status == 200
    assert body["content_type"] == "image/png"
    assert body["size"] == len(PNG_BYTES)
    assert body["filename"] == "megnp.exe"
    name = str(body["name"])
    assert name.startswith("avatars/")
    assert name.endswith(".png")
    assert (tmp_path / name).read_bytes() == PNG_BYTES


def test_the_clients_content_type_is_never_believed(
    client: TestClient, tmp_path: Path
) -> None:
    """A PNG called HTML is a PNG; HTML called a PNG is refused."""
    disguised = upload(
        client, PNG_BYTES, filename="page.html", content_type="text/html"
    )
    refused = upload(client, b"<script>alert(1)</script>", content_type="image/png")
    svg = upload(
        client, b"<svg xmlns='http://www.w3.org/2000/svg'/>", "a.svg", "image/svg+xml"
    )

    assert disguised[1]["content_type"] == "image/png"
    assert str(disguised[1]["name"]).endswith(".png")
    for status, body in (refused, svg):
        assert (status, body["code"]) == (415, "unsupported_file_type")
        assert body["detail"] == "This type of file is not accepted."
    assert len(stored_files(tmp_path)) == 1


def test_sizes_are_checked(client: TestClient, tmp_path: Path) -> None:
    """Up to the limit is kept; more, or nothing at all, is refused."""
    exact = upload(client, PDF_BYTES + b"x" * (1024 - len(PDF_BYTES)), "a.pdf")
    large = upload(client, PDF_BYTES + b"x" * 1024, "b.pdf")
    empty = upload(client, b"", "c.pdf")

    assert exact[0] == 200
    assert (large[0], large[1]["code"]) == (413, "file_too_large")
    assert large[1]["detail"] == "The file is too large."
    assert (empty[0], empty[1]["code"]) == (400, "empty_file")
    assert empty[1]["detail"] == "The file is empty."
    assert len(stored_files(tmp_path)) == 1


def test_refusals_are_translated(client: TestClient) -> None:
    """In the caller's language, as every error is."""
    response = client.post(
        "/uploads",
        files={"file": ("x.png", b"plain text", "image/png")},
        headers={"Accept-Language": "ar"},
    )
    assert response.json()["detail"] == "هذا النوع من الملفات غير مقبول."


class Trickle(io.BytesIO):
    """A file that hands out at most seven bytes per read."""

    def read(self, size: int | None = -1) -> bytes:
        """Fewer bytes than asked."""
        return super().read(7 if size is None or size < 0 else min(size, 7))


async def test_sizes_are_enforced_while_streaming() -> None:
    """Without a declared size, the stream stops at the limit; nothing is kept."""
    storage = MemoryStorage()
    unknown = UploadFile(Trickle(PNG_BYTES * 3), filename="big.png")

    with pytest.raises(UploadRefused) as refused:
        await save_upload(
            unknown, types=IMAGES, max_size=len(PNG_BYTES), storage=storage
        )
    small = UploadFile(Trickle(PNG_BYTES), filename=None, size=None)
    stored = await save_upload(small, types=[PNG], max_size=1000, storage=storage)

    assert refused.value.status_code == 413
    assert list(storage.files) == [stored.name]
    assert stored == StoredUpload(stored.name, "image/png", len(PNG_BYTES), "file")
    assert storage.files[stored.name] == PNG_BYTES


async def test_declared_sizes_are_refused_before_reading() -> None:
    """A declared size over the limit is refused without reading a byte."""
    storage = MemoryStorage()
    declared = UploadFile(io.BytesIO(PNG_BYTES), size=10_000)

    with pytest.raises(UploadRefused, match="too large"):
        await save_upload(declared, types=IMAGES, max_size=1000, storage=storage)
    assert declared.file.tell() == 0
    with pytest.raises(ValueError, match=r"^save_upload needs the types it accepts\.$"):
        await save_upload(declared, types=[], max_size=1000, storage=storage)


class Recording(io.BytesIO):
    """A file that remembers how much each read asked for."""

    def __init__(self, data: bytes) -> None:
        """Hold ``data``."""
        super().__init__(data)
        self.asked: list[int | None] = []

    def read(self, size: int | None = -1) -> bytes:
        """Note the size asked for."""
        self.asked.append(size)
        return super().read(size)


async def test_only_the_head_is_read_to_know_the_type() -> None:
    """``SNIFF_BYTES``, though the file gives a few bytes at a time."""
    upload = UploadFile(Trickle(b"x" * 2000))

    assert await _head(upload) == b"x" * 512
    assert upload.file.tell() == 512


async def test_large_uploads_stream_in_chunks(fresh_storages: None) -> None:
    """Every byte, a chunk at a time, into the default storage's top level."""
    content = PNG_BYTES + bytes(range(256)) * 1000  # Several chunks.
    file = Recording(content)
    with override_settings(
        STORAGES={"default": {"BACKEND": "domainkit.storage.MemoryStorage"}}
    ):
        stored = await save_upload(
            UploadFile(file, size=None), types=IMAGES, max_size=10**6
        )
        storage = storages["default"]

    assert isinstance(storage, MemoryStorage)
    assert "/" not in stored.name
    assert stored.size == len(content)
    assert storage.files[stored.name] == content
    assert all(size is not None and 0 < size <= CHUNK_SIZE for size in file.asked)


@pytest.fixture
def fresh_storages() -> Iterator[None]:
    """Forget the storages made."""
    storages.reset()
    yield
    storages.reset()


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (PNG_BYTES, PNG),
        (b"\xff\xd8\xff\xe0rest", JPEG),
        (b"GIF89a....", GIF),
        (b"GIF87a....", GIF),
        (b"RIFF\x10\0\0\0WEBPVP8 ", WEBP),
        (PDF_BYTES, PDF),
        (b"PK\x03\x04rest", ZIP),
        (b"PK\x05\x06", ZIP),
        (b"RIFF\x10\0\0\0WAVE", None),
        (b"GIF90a", None),
        (b"\x89PNG", None),
        (b"\xff\xd8", None),
        (b"%PDF", None),
        (b"PK\x01\x02", None),
        (b"", None),
    ],
)
def test_types_are_known_by_their_first_bytes(head: bytes, expected: object) -> None:
    """Each signature, and near misses."""
    assert sniff(head, [PNG, JPEG, GIF, WEBP, PDF, ZIP]) is expected


@pytest.mark.parametrize(
    ("head", "text"),
    [
        (b"name,amount\r\nalice,10\n\tx\f", True),
        ("\u0646\u0635 \u0639\u0631\u0628\u064a".encode(), True),
        ("\u0646\u0635".encode()[:-1], True),  # Cut by the end of the head.
        (b"ok\xe0\xa0", True),
        (b"ok\xf0\x90\x80", True),
        (b"", False),
        (b"text\0with nul", False),
        (b"\x1b[31mred", False),
        (b"\xff\xfe\xfdbad utf-8", False),
        ("\u0646\u0635".encode()[:-1] + b"more", False),
        (b"ok\xc3\x28", False),
    ],
)
def test_text_is_utf8_without_controls(head: bytes, text: bool) -> None:
    """Tabs and line breaks are text; NUL and escapes are not; cut characters are."""
    assert (sniff(head, [TEXT]) is TEXT) is text


# File names --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "cleaned"),
    [
        (None, "file"),
        ("", "file"),
        ("report.pdf", "report.pdf"),
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\me\\photo.png", "photo.png"),
        ("photo\u202egnp.exe", "photognp.exe"),
        ("a\u200bb\x00c\x7f.txt", "abc.txt"),
        ('bad<>:"|?*name.txt', "badname.txt"),
        ("  lots \t of\n space  .txt", "lots of space .txt"),
        ("...hidden", "hidden"),
        ("trailing. . .", "trailing"),
        ("CON.txt", "_CON.txt"),
        ("lpt9", "_lpt9"),
        ("console.txt", "console.txt"),
        ("Xray.pngX", "Xray.pngX"),
        ("e\u0301te\u0301.txt", "\u00e9t\u00e9.txt"),
        ("تقرير.pdf", "تقرير.pdf"),
        ("\u202e\u200b", "file"),
        ("..", "file"),
    ],
)
def test_file_names_are_cleaned(given: str | None, cleaned: str) -> None:
    """Paths, controls, direction overrides, reserved names, and dots go."""
    assert sanitize_filename(given) == cleaned


def test_long_names_keep_their_extension() -> None:
    """Cut to 255 bytes, never inside a character; long extensions are not kept."""
    arabic = sanitize_filename("ت" * 200 + ".pdf")
    plain = sanitize_filename("a" * 300)
    odd = sanitize_filename("a" * 300 + "." + "b" * 17)

    assert arabic.endswith(".pdf")
    assert len(arabic.encode()) <= 255
    assert arabic[:-4] == "ت" * 125
    assert plain == "a" * 255
    assert len(odd.encode()) == 255
    assert odd == ("a" * 300 + "." + "b" * 17)[:255]
    assert sanitize_filename("a" * 251 + ".pdf") == "a" * 251 + ".pdf"
    sixteen = "." + "b" * 16
    assert sanitize_filename("a" * 300 + sixteen) == "a" * (255 - 17) + sixteen
    assert sanitize_filename("a.b" + "c" * 300 + ".png").endswith("c.png")
    # The cut stem loses the dots and spaces it ends with, nothing else.
    cut = sanitize_filename("a" * 248 + "X. " + "b" * 20 + ".txt")
    assert cut == "a" * 248 + "X.txt"


def test_content_disposition_names_files_for_every_client() -> None:
    """An ASCII name for old clients, the UTF-8 one for the others."""
    assert content_disposition("\u062a\u0642\u0631\u064a\u0631 2026.pdf") == (
        'attachment; filename="2026.pdf"; '
        "filename*=UTF-8''%D8%AA%D9%82%D8%B1%D9%8A%D8%B1%202026.pdf"
    )
    assert content_disposition('a"b?c.png', inline=True) == (
        "inline; filename=\"abc.png\"; filename*=UTF-8''abc.png"
    )
    assert content_disposition("\u00fc.txt").startswith('attachment; filename="u.txt"')
    assert content_disposition("\u65e5.txt").startswith('attachment; filename=".txt"')
    assert content_disposition("\u65e5").startswith('attachment; filename="file"')
    assert content_disposition("a;b=c.txt").startswith(
        'attachment; filename="a_b_c.txt"'
    )


# Serving -----------------------------------------------------------------------------


def test_files_are_served_as_downloads(client: TestClient) -> None:
    """Streamed with their length, never sniffed, never run."""
    name = str(upload(client, PDF_BYTES, "invoice.pdf")[1]["name"])

    response = client.get(
        f"/files/{name}",
        params={"content_type": "application/pdf", "filename": "Invoice 7.pdf"},
    )

    assert response.status_code == 200
    assert response.content == PDF_BYTES
    assert response.headers["Content-Type"] == "application/pdf"
    assert response.headers["Content-Length"] == str(len(PDF_BYTES))
    assert response.headers["Content-Disposition"].startswith(
        'attachment; filename="Invoice 7.pdf"'
    )
    assert response.headers["Content-Security-Policy"] == "default-src 'none'; sandbox"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    default = client.get(f"/files/{name}").headers["Content-Disposition"]
    assert default.startswith(f'attachment; filename="{name.rpartition("/")[2]}"')


def test_images_and_pdfs_may_be_shown_inline(client: TestClient) -> None:
    """Only types browsers do not run; others must download."""
    name = str(upload(client, PNG_BYTES)[1]["name"])

    shown = client.get(
        f"/files/{name}", params={"content_type": "image/png", "inline": True}
    )

    assert shown.headers["Content-Disposition"].startswith("inline;")


def test_missing_files_are_404(client: TestClient) -> None:
    """A name no file has, or a malformed one, is not found."""
    for name in ("avatars/" + "a" * 32 + ".png", "avatars/.hidden", "a%5Cb", "x.PNG"):
        response = client.get(f"/files/{name}")
        assert (response.status_code, response.json()["code"]) == (
            404,
            "file_not_found",
        )
        assert response.json()["detail"] == "No file has that name."


async def test_serving_closes_the_file() -> None:
    """The stream closes the file when it ends, and the background task again."""
    storage = MemoryStorage()
    name = await storage.save(b"data")
    response = await serve(storage, name, content_type="text/plain")

    body = b"".join([chunk async for chunk in response.body_iterator])  # type: ignore[misc]
    assert body == b"data"
    assert response.headers["Content-Disposition"].startswith(
        f'attachment; filename="{name}"'
    )
    assert response.headers["Content-Security-Policy"] == "default-src 'none'; sandbox"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.background is not None
    await response.background()
    with pytest.raises(FileMissing):
        await serve(storage, "a" * 32, content_type="text/plain")
    with pytest.raises(ValueError, match="cannot be served inline"):
        await serve(storage, name, content_type="text/html", inline=True)


def test_file_routes_must_say_who_may_call_them() -> None:
    """Deny by default (ADR 0021): a route that serves files needs a rule."""
    app = FastAPI()

    @app.get("/open/{name}")
    async def unguarded(name: str) -> None:
        """Serve without a rule."""

    @app.get("/guarded/{name}", dependencies=[Depends(Require())])
    async def guarded(name: str) -> None:
        """Serve to users who logged in."""

    assert open_routes(app) == ["GET /open/{name}"]
