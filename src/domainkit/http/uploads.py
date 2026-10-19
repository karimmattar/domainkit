"""Receiving uploaded files, and serving stored ones, safely.

``save_upload`` stores a FastAPI ``UploadFile`` in a storage under a new
random name. It decides the file's type from its first bytes (its "magic
number"), never from the name or the ``Content-Type`` the client sent, and
accepts only the types the route lists. It stops at ``max_size`` while
the file streams into the storage, so nothing larger is kept. The client's
file name comes back cleaned, to show people, never to name the file::

    @router.post("/avatars", dependencies=[Depends(Require())])
    async def upload_avatar(file: UploadFile) -> AvatarOut:
        stored = await save_upload(file, types=IMAGES, max_size=2_000_000)
        ...  # keep stored.name, stored.content_type, and stored.filename

``serve`` answers with a stored file from a route, which must say who may
call it (``Require(...)``, or ``Public`` on purpose), as every route must
(ADR 0021). Routes need ``python-multipart`` to read uploads: install the
``uploads`` extra. See ``docs/uploads.md`` and ADR 0040.
"""

import re
import unicodedata
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass
from typing import Final
from urllib.parse import quote

from fastapi import status
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from starlette.datastructures import UploadFile

from domainkit.http.errors import APIError
from domainkit.storage.base import CHUNK_SIZE, FileTooLarge, Storage
from domainkit.storage.handler import storages

SNIFF_BYTES: Final = 512
"""How much of a file its type is read from."""
MAX_FILENAME_BYTES: Final = 255
"""The longest file name most file systems take, in UTF-8 bytes."""
_RESERVED_NAMES: Final = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{n}" for n in range(1, 10)}
    | {f"lpt{n}" for n in range(1, 10)}
)
"""Names Windows will not open as files, whatever their extension."""
_FORBIDDEN: Final = frozenset('<>:"/\\|?*')
_TEXT_CONTROLS: Final = frozenset(b"\t\n\r\f")


@dataclass(frozen=True, slots=True)
class FileType:
    """A type of file a route accepts, and how to recognize its first bytes."""

    content_type: str
    extension: str
    """Given to stored files, as ``.png``."""
    matches: Callable[[bytes], bool]
    """Whether a file starting with these bytes is of this type."""


def _png(head: bytes) -> bool:
    return head.startswith(b"\x89PNG\r\n\x1a\n")


def _jpeg(head: bytes) -> bool:
    return head.startswith(b"\xff\xd8\xff")


def _gif(head: bytes) -> bool:
    return head[:6] in {b"GIF87a", b"GIF89a"}


def _webp(head: bytes) -> bool:
    return head[:4] == b"RIFF" and head[8:12] == b"WEBP"


def _pdf(head: bytes) -> bool:
    return head.startswith(b"%PDF-")


def _zip(head: bytes) -> bool:
    return head[:4] in {b"PK\x03\x04", b"PK\x05\x06"}


def _text(head: bytes) -> bool:
    """UTF-8 without control characters but tabs and line breaks.

    A character cut off by the end of ``head`` does not count against it.
    """
    if not head or any(byte < 0x20 and byte not in _TEXT_CONTROLS for byte in head):
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as exc:
        return exc.reason == "unexpected end of data" and exc.start >= len(head) - 3
    return True


PNG: Final = FileType("image/png", ".png", _png)
JPEG: Final = FileType("image/jpeg", ".jpg", _jpeg)
GIF: Final = FileType("image/gif", ".gif", _gif)
WEBP: Final = FileType("image/webp", ".webp", _webp)
PDF: Final = FileType("application/pdf", ".pdf", _pdf)
ZIP: Final = FileType("application/zip", ".zip", _zip)
"""Also Office documents (``.docx``, ``.xlsx``), which are zip files."""
TEXT: Final = FileType("text/plain", ".txt", _text)
"""UTF-8 text, CSV included; it cannot be told apart by its bytes."""
IMAGES: Final = (PNG, JPEG, GIF, WEBP)
"""Images browsers show; SVG is not one of them, as it can carry scripts."""
INLINE_TYPES: Final = frozenset(
    {PNG.content_type, JPEG.content_type, GIF.content_type, WEBP.content_type}
    | {PDF.content_type}
)
"""Types ``serve`` may show in the browser rather than download."""


def sniff(head: bytes, types: Iterable[FileType]) -> FileType | None:
    """The first of ``types`` whose signature ``head`` starts with."""
    return next((kind for kind in types if kind.matches(head)), None)


@dataclass(frozen=True, slots=True)
class StoredUpload:
    """An upload, stored."""

    name: str
    """The storage's name for it: keep this to find the file again."""
    content_type: str
    """Its type, from its content."""
    size: int
    filename: str
    """The client's name for it, cleaned: to show people, never a path."""


class UploadRefused(APIError):
    """An upload was empty (400), too large (413), or of a type not accepted (415)."""


async def save_upload(
    upload: UploadFile,
    *,
    types: Iterable[FileType],
    max_size: int,
    storage: Storage | str = "default",
    directory: str = "",
) -> StoredUpload:
    """Store ``upload`` if it is one of ``types`` and at most ``max_size`` bytes.

    Raises:
        UploadRefused: 400 ``empty_file``, 413 ``file_too_large``, or 415
            ``unsupported_file_type``; nothing is stored.
        ValueError: ``types`` is empty, or ``directory`` malformed.
    """
    accepted = tuple(types)
    if not accepted:
        raise ValueError("save_upload needs the types it accepts.")
    target = storages[storage] if isinstance(storage, str) else storage
    if upload.size is not None and upload.size > max_size:
        raise _too_large()
    head = await _head(upload)
    if not head:
        raise UploadRefused(
            status.HTTP_400_BAD_REQUEST, "The file is empty.", code="empty_file"
        )
    kind = sniff(head, accepted)
    if kind is None:
        raise UploadRefused(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "This type of file is not accepted.",
            code="unsupported_file_type",
        )
    size = 0

    async def content() -> AsyncIterator[bytes]:
        nonlocal size
        chunk = head
        while chunk:
            size += len(chunk)
            yield chunk
            chunk = await upload.read(CHUNK_SIZE)

    try:
        name = await target.save(
            content(), directory=directory, extension=kind.extension, max_size=max_size
        )
    except FileTooLarge:
        raise _too_large() from None
    return StoredUpload(
        name, kind.content_type, size, sanitize_filename(upload.filename)
    )


async def _head(upload: UploadFile) -> bytes:
    """The first ``SNIFF_BYTES`` of the upload (fewer if it is shorter)."""
    head = b""
    while len(head) < SNIFF_BYTES and (
        part := await upload.read(SNIFF_BYTES - len(head))
    ):
        head += part
    return head


def _too_large() -> UploadRefused:
    """413: the upload is larger than the route takes."""
    return UploadRefused(
        status.HTTP_413_CONTENT_TOO_LARGE,
        "The file is too large.",
        code="file_too_large",
    )


def sanitize_filename(filename: str | None) -> str:
    """A client's file name, safe to show and to put in ``Content-Disposition``.

    Only the last path segment is kept. Control and formatting characters
    (including right-to-left overrides, which disguise ``exe.png`` as
    ``gnp.exe``), and characters Windows refuses, are dropped; spaces are
    collapsed, and leading and trailing dots and spaces trimmed. Names
    Windows reserves get a ``_`` in front, and long names are cut to 255
    UTF-8 bytes, keeping the extension. What is left of nothing is ``file``.
    """
    if not filename:
        return "file"
    name = re.split(r"[/\\]", unicodedata.normalize("NFC", filename))[-1]
    kept = "".join(
        " " if character.isspace() else character
        for character in name
        if unicodedata.category(character)[0] != "C" or character.isspace()
        if character not in _FORBIDDEN
    )
    name = re.sub(r" +", " ", kept).strip(" .")
    if name.split(".")[0].lower() in _RESERVED_NAMES:
        name = f"_{name}"
    return _shortened(name) or "file"


def _shortened(name: str) -> str:
    """``name`` cut to ``MAX_FILENAME_BYTES``, keeping a short extension."""
    if len(name.encode()) <= MAX_FILENAME_BYTES:
        return name
    stem, dot, extension = name.rpartition(".")
    if not dot or len(extension.encode()) > 16:
        stem, extension = name, ""
    suffix = f".{extension}" if extension else ""
    room = MAX_FILENAME_BYTES - len(suffix.encode())
    return stem.encode()[:room].decode("utf-8", "ignore").rstrip(" .") + suffix


def content_disposition(filename: str, *, inline: bool = False) -> str:
    """A ``Content-Disposition`` value naming ``filename``, for any client (RFC 6266).

    Old clients read the ASCII ``filename``; others ``filename*``, in UTF-8.
    """
    name = sanitize_filename(filename)
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    ascii_name = re.sub(r"[^A-Za-z0-9._ -]", "_", ascii_name).strip() or "file"
    kind = "inline" if inline else "attachment"
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"


async def serve(
    storage: Storage | str,
    name: str,
    *,
    content_type: str,
    filename: str | None = None,
    inline: bool = False,
) -> StreamingResponse:
    """Answer with the stored file ``name``, streamed, as ``content_type``.

    Use it in a route that says who may call it. The file downloads under
    ``filename`` (default: its name) unless ``inline`` shows it, which
    only images and PDFs may. It may not run scripts or load anything
    (``Content-Security-Policy: sandbox``), nor be read as another type.

    Raises:
        FileMissing: No file has the name (404).
        ValueError: ``inline`` for a type browsers could run.
    """
    if inline and content_type not in INLINE_TYPES:
        raise ValueError(f"{content_type} cannot be served inline; download it.")
    target = storages[storage] if isinstance(storage, str) else storage
    file = await target.open(name)
    headers = {
        "Content-Length": str(file.size),
        "Content-Disposition": content_disposition(
            filename or name.rpartition("/")[2], inline=inline
        ),
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "X-Content-Type-Options": "nosniff",
    }
    return StreamingResponse(
        file.chunks(),
        media_type=content_type,
        headers=headers,
        background=BackgroundTask(file.close),
    )
