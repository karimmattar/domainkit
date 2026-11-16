"""Receiving uploaded files, and serving stored ones, safely.

``save_upload`` stores a FastAPI ``UploadFile``, bytes, or a stream of
them in a storage under a new random name. It decides the file's type from
its first bytes (its "magic number"), never from the name or the
``Content-Type`` the client sent, and accepts only the types the route
lists. It stops at ``max_size`` while the file streams into the storage,
so nothing larger is kept. The client's file name comes back cleaned, to
show people, never to name the file::

    @router.post("/avatars", dependencies=[Depends(Require())])
    async def upload_avatar(file: UploadFile) -> AvatarOut:
        stored = await save_upload(file, types=IMAGES, max_size=2_000_000)
        ...  # keep stored.name, stored.content_type, and stored.filename

A type with an ``inspect`` hook is checked whole before it is stored: the
upload is held in a temporary file (in memory up to 1 MiB), the hook reads
it in a process of its own, under the system's limits of memory and time
(``UPLOAD_CHECKS``), and a file it refuses is never stored (422).
``CHECKED_PDF``, ``DOCX``, ``XLSX`` and ``PPTX`` ship such hooks
(``domainkit.inspection``, the ``documents`` extra); ``PDF`` and
``ZIP`` read the first bytes only. ``receive_upload`` does the same work
without storing, for a route that stores later (ADR 0167).

``serve`` answers with a stored file from a route, which must say who may
call it (``Require(...)``, or ``Public`` on purpose), as every route must
(ADR 0021). Routes need ``python-multipart`` to read forms: install the
``uploads`` extra. See ``docs/uploads.md`` and ADR 0040.
"""

import asyncio
import hashlib
import logging
import pickle
import re
import tempfile
import unicodedata
from collections.abc import AsyncIterable, AsyncIterator, Callable, Iterable
from dataclasses import dataclass, replace
from typing import IO, Final
from urllib.parse import quote

from fastapi import status
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from starlette.datastructures import UploadFile

from domainkit.conf import settings
from domainkit.http.errors import APIError
from domainkit.inspection import (
    FileRefused,
    NotOfType,
    OfficeCheck,
    PdfCheck,
    run_checks,
)
from domainkit.storage.base import CHUNK_SIZE, FileTooLarge, Storage
from domainkit.storage.base import StoredUpload as StoredUpload
from domainkit.storage.handler import storages

__all__ = [
    "CHECKED_PDF",
    "DOCX",
    "GIF",
    "IMAGES",
    "JPEG",
    "OFFICE",
    "PDF",
    "PNG",
    "PPTX",
    "TEXT",
    "WEBP",
    "XLSX",
    "ZIP",
    "FileType",
    "ReceivedUpload",
    "UploadRefused",
    "receive_upload",
    "sanitize_filename",
    "save_upload",
    "serve",
]

logger = logging.getLogger(__name__)
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
SPOOL_MEMORY: Final = 1024 * 1024
"""How much of an upload held for a check stays in memory; the rest goes to disk."""

type Upload = UploadFile | bytes | AsyncIterable[bytes]
"""What ``save_upload`` takes: a form's file, bytes, or a stream of bytes."""


@dataclass(frozen=True, slots=True)
class FileType:
    """A type of file a route accepts, and how to recognize its first bytes."""

    content_type: str
    extension: str
    """Given to stored files, as ``.png``."""
    matches: Callable[[bytes], bool]
    """Whether a file starting with these bytes is of this type."""
    inspect: Callable[[IO[bytes]], None] | None = None
    """Checks the whole file, read from its start, before it is stored (New
    in 2.5). It runs in a process of its own (``UPLOAD_CHECKS``), so it
    must pickle: a function at a module's top level, or an object of a
    class that is. It raises ``FileRefused`` (422) for a file it will not
    accept, or ``NotOfType`` for one that is not of the type (the next
    type that matches is tried). None: the first bytes decide."""

    def __post_init__(self) -> None:
        """Refuse a check that cannot be handed to a process of its own.

        Raises:
            TypeError: ``inspect`` does not pickle (a lambda, a nested
                function), or is defined in ``__main__``, which the process
                cannot import: it runs no script.
        """
        if self.inspect is None:
            return
        modules = (
            getattr(self.inspect, "__module__", None),
            type(self.inspect).__module__,
        )
        if "__main__" in modules:
            raise TypeError(
                "A FileType's inspect runs in a process of its own, which cannot "
                "import __main__: define it in a module the application imports."
            )
        try:
            pickle.dumps(self.inspect)
        except (pickle.PicklingError, AttributeError, TypeError) as exc:
            raise TypeError(
                "A FileType's inspect runs in a process of its own: give a function "
                "defined at a module's top level, or an object of a class that is."
            ) from exc


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

    A character cut off by the end of ``head`` does not count against it:
    the decoder says "unexpected end of data" for that alone, as it stops
    at the first fault.
    """
    if not head or any(byte < 0x20 and byte not in _TEXT_CONTROLS for byte in head):
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as exc:
        return exc.reason == "unexpected end of data"
    return True


PNG: Final = FileType("image/png", ".png", _png)
JPEG: Final = FileType("image/jpeg", ".jpg", _jpeg)
GIF: Final = FileType("image/gif", ".gif", _gif)
WEBP: Final = FileType("image/webp", ".webp", _webp)
PDF: Final = FileType("application/pdf", ".pdf", _pdf)
ZIP: Final = FileType("application/zip", ".zip", _zip)
"""Any zip file, Office documents included, by its first bytes only."""
TEXT: Final = FileType("text/plain", ".txt", _text)
"""UTF-8 text, CSV included; it cannot be told apart by its bytes."""
IMAGES: Final = (PNG, JPEG, GIF, WEBP)
"""Images browsers show; SVG is not one of them, as it can carry scripts."""
CHECKED_PDF: Final = FileType("application/pdf", ".pdf", _pdf, PdfCheck())
"""A PDF without scripts, actions, embedded files, XFA or a password (New in 2.5)."""
DOCX: Final = FileType(
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".docx",
    _zip,
    OfficeCheck("docx"),
)
"""A Word document without macros, objects or outside links (New in 2.5)."""
XLSX: Final = FileType(
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsx",
    _zip,
    OfficeCheck("xlsx"),
)
"""An Excel workbook without macros, objects or outside links (New in 2.5)."""
PPTX: Final = FileType(
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".pptx",
    _zip,
    OfficeCheck("pptx"),
)
"""A PowerPoint presentation without macros, objects or outside links (New in 2.5)."""
OFFICE: Final = (DOCX, XLSX, PPTX)
"""Word, Excel and PowerPoint documents, each checked (New in 2.5)."""
INLINE_TYPES: Final = frozenset(
    {PNG.content_type, JPEG.content_type, GIF.content_type, WEBP.content_type}
    | {PDF.content_type}
)
"""Types ``serve`` may show in the browser rather than download."""


def sniff(head: bytes, types: Iterable[FileType]) -> FileType | None:
    """The first of ``types`` whose signature ``head`` starts with."""
    return next((kind for kind in types if kind.matches(head)), None)


class UploadRefused(APIError):
    """An upload was empty (400), too large (413), not taken (415), or refused (422).

    A type not taken is 415; a refusal is the virus scan's, or the check
    of the file's type.
    """


class _Reader:
    """An upload, read a few bytes at a time, whatever it came as."""

    def __init__(self, upload: Upload, filename: str | None) -> None:
        """Read ``upload``; ``filename`` replaces a form file's own name."""
        self._file = upload if isinstance(upload, UploadFile) else None
        self._chunks: AsyncIterator[bytes] | None = None
        self._pending = b""
        self._at = 0
        self.size: int | None = None
        """The size the upload declares, if it declares one."""
        self.filename = filename
        """The client's name for the file, as it sent it."""
        if isinstance(upload, UploadFile):
            self.size = upload.size
            if filename is None:
                self.filename = upload.filename
        elif isinstance(upload, bytes | bytearray | memoryview):
            self._pending = bytes(upload)
            self.size = len(self._pending)
        else:
            self._chunks = aiter(upload)

    async def read(self, size: int) -> bytes:
        """Up to ``size`` more bytes; ``b""`` at the end."""
        if self._file is not None:
            return await self._file.read(size)
        while self._at >= len(self._pending):
            chunk = None if self._chunks is None else await anext(self._chunks, None)
            if chunk is None:
                return b""
            self._pending, self._at = bytes(chunk), 0
        part = self._pending[self._at : self._at + size]
        self._at += len(part)
        return part


@dataclass(slots=True)
class ReceivedUpload:
    """An upload received whole and checked, held in a temporary file.

    Nothing is stored yet: ``store`` keeps it, through the virus scan, and
    drops the temporary file; ``close`` drops it without storing.
    ``receive_upload`` makes one (New in 2.5).
    """

    kind: FileType
    """Its type, from its content, and its type's check if it has one."""
    size: int
    sha256: str
    """The SHA-256 of its bytes, in hex."""
    filename: str
    """The client's name for it, cleaned: to show people, never a path."""
    file: IO[bytes]
    """The temporary file that holds it."""

    def unstored(self) -> StoredUpload:
        """What ``store`` returns, but its name, which is empty: nothing is stored."""
        return StoredUpload(
            "", self.kind.content_type, self.size, self.filename, self.sha256
        )

    async def store(
        self, storage: Storage | str = "default", *, directory: str = ""
    ) -> StoredUpload:
        """Store it under a new name, streamed through ``UPLOAD_SCANNER`` if set.

        Raises:
            UploadRefused: 422 ``infected_file``; nothing is stored.
            ScannerUnavailable: The scanner could not scan it (503).
            ValueError: ``directory`` is malformed.
        """
        target = storages[storage] if isinstance(storage, str) else storage
        try:
            stored = await _store(
                target,
                self.kind,
                self._chunks(),
                directory=directory,
                max_size=self.size,
                filename=self.filename,
            )
        finally:
            self.close()
        return replace(stored, sha256=self.sha256)

    def close(self) -> None:
        """Drop the temporary file; again, nothing more."""
        self.file.close()

    async def _chunks(self) -> AsyncIterator[bytes]:
        """The file from its start, a chunk at a time; read in a thread from disk."""
        on_disk = self.size > SPOOL_MEMORY
        self.file.seek(0)
        while chunk := (
            await asyncio.to_thread(self.file.read, CHUNK_SIZE)
            if on_disk
            else self.file.read(CHUNK_SIZE)
        ):
            yield chunk


async def save_upload(
    upload: Upload,
    *,
    types: Iterable[FileType],
    max_size: int,
    storage: Storage | str = "default",
    directory: str = "",
    filename: str | None = None,
) -> StoredUpload:
    """Store ``upload`` if it is one of ``types`` and at most ``max_size`` bytes.

    ``upload`` is a form's ``UploadFile``, bytes, or an async iterable of
    bytes (``request.stream()``); ``filename`` is the client's name for
    it, which replaces a form file's own. A type with an ``inspect`` hook
    is received whole and checked before it is stored (``receive_upload``).
    With ``UPLOAD_SCANNER`` set, the upload streams through the scanner as
    it is stored, and a finding fails the write.

    Raises:
        UploadRefused: 400 ``empty_file``, 413 ``file_too_large``, 415
            ``unsupported_file_type``, or 422 ``infected_file``,
            ``unsafe_file`` or ``unreadable_file``; nothing is stored.
        ScannerUnavailable: The scanner could not scan it (503).
        ValueError: ``types`` is empty, or ``directory`` malformed.
    """
    accepted = _accepted(types)
    target = storages[storage] if isinstance(storage, str) else storage
    reader = _Reader(upload, filename)
    head = await _first(reader, max_size)
    candidates = _candidates(head, accepted)
    if candidates[0].inspect is None:
        return await _store(
            target,
            candidates[0],
            _chunks(head, reader),
            directory=directory,
            max_size=max_size,
            filename=sanitize_filename(reader.filename),
        )
    received = await _received(reader, head, candidates, max_size)
    return await received.store(target, directory=directory)


async def receive_upload(
    upload: Upload,
    *,
    types: Iterable[FileType],
    max_size: int,
    filename: str | None = None,
) -> ReceivedUpload:
    """Receive ``upload`` whole and check it as ``save_upload`` does; store nothing.

    For a route that stores the file later, or not at all (an
    authorization's twin). Close what it returns, or store it.

    Raises:
        UploadRefused: 400 ``empty_file``, 413 ``file_too_large``, 415
            ``unsupported_file_type``, or 422 ``unsafe_file`` or
            ``unreadable_file``.
        ValueError: ``types`` is empty.
    """
    accepted = _accepted(types)
    reader = _Reader(upload, filename)
    head = await _first(reader, max_size)
    return await _received(reader, head, _candidates(head, accepted), max_size)


def _accepted(types: Iterable[FileType]) -> tuple[FileType, ...]:
    """``types``, as a tuple.

    Raises:
        ValueError: There are none.
    """
    accepted = tuple(types)
    if not accepted:
        raise ValueError("save_upload needs the types it accepts.")
    return accepted


async def _first(reader: _Reader, max_size: int) -> bytes:
    """The upload's first bytes, once its declared size fits.

    Raises:
        UploadRefused: It declares more than ``max_size`` bytes (413), or
            is empty (400).
    """
    if reader.size is not None and reader.size > max_size:
        raise _too_large()
    head = await _head(reader)
    if not head:
        raise UploadRefused(
            status.HTTP_400_BAD_REQUEST, "The file is empty.", code="empty_file"
        )
    return head


def _candidates(head: bytes, accepted: tuple[FileType, ...]) -> list[FileType]:
    """The accepted types whose first bytes ``head`` has, in order.

    Raises:
        UploadRefused: None has (415).
    """
    found = [kind for kind in accepted if kind.matches(head)]
    if not found:
        raise _unsupported()
    return found


async def _chunks(head: bytes, reader: _Reader) -> AsyncIterator[bytes]:
    """The upload from its start: ``head``, then the rest, a chunk at a time."""
    chunk = head
    while chunk:
        yield chunk
        chunk = await reader.read(CHUNK_SIZE)


async def _store(
    target: Storage,
    kind: FileType,
    chunks: AsyncIterator[bytes],
    *,
    directory: str,
    max_size: int,
    filename: str,
) -> StoredUpload:
    """Store ``chunks`` as ``kind``, through the scanner if one is set.

    Raises:
        UploadRefused: 413 ``file_too_large``, or 422 ``infected_file``.
        ScannerUnavailable: The scanner could not scan it (503).
    """
    size = 0
    digest = hashlib.sha256()

    async def counted() -> AsyncIterator[bytes]:
        nonlocal size
        async for chunk in chunks:
            size += len(chunk)
            digest.update(chunk)
            yield chunk

    # Loaded here: an application that takes no uploads never imports it.
    from domainkit.storage.scanning import Infected, configured_scanner

    scanner = configured_scanner()
    stream = counted() if scanner is None else scanner.scan(counted())
    try:
        name = await target.save(
            stream, directory=directory, extension=kind.extension, max_size=max_size
        )
    except FileTooLarge:
        raise _too_large() from None
    except Infected as exc:
        logger.warning("An upload was refused by the virus scan: %s", exc.signature)
        raise UploadRefused(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "The virus scan refused the file.",
            code="infected_file",
        ) from None
    return StoredUpload(name, kind.content_type, size, filename, digest.hexdigest())


async def _received(
    reader: _Reader, head: bytes, candidates: list[FileType], max_size: int
) -> ReceivedUpload:
    """The whole upload in a temporary file, and the first candidate it is.

    Raises:
        UploadRefused: 413, 415, or 422 (a check refused it).
    """
    spool = tempfile.SpooledTemporaryFile(max_size=SPOOL_MEMORY)  # noqa: SIM115 - the caller closes it.
    try:
        size = 0
        digest = hashlib.sha256()
        async for chunk in _chunks(head, reader):
            size += len(chunk)
            if size > max_size:
                raise _too_large()
            digest.update(chunk)
            if size > SPOOL_MEMORY:
                await asyncio.to_thread(spool.write, chunk)  # It goes to disk.
            else:
                spool.write(chunk)
        kind = await _checked(spool, candidates)
    except BaseException:
        spool.close()
        raise
    filename = sanitize_filename(reader.filename)
    return ReceivedUpload(kind, size, digest.hexdigest(), filename, spool)


async def _checked(file: IO[bytes], candidates: list[FileType]) -> FileType:
    """The first of ``candidates`` whose check takes ``file``: in a process of its own.

    A type without a check takes it, and a file whose first candidate has
    none takes no process. A check that says the file is not of its type
    (``NotOfType``) passes it to the next. The process runs as
    ``UPLOAD_CHECKS`` says (``domainkit.inspection.run_checks``).

    Raises:
        ServiceBusy: As many checks run already (503).
        UploadRefused: A check refused it, or a limit stopped it (422), or
            no type took it (415).
    """
    if candidates[0].inspect is None:
        return candidates[0]
    taken = candidates[
        : next(
            (
                index + 1
                for index, kind in enumerate(candidates)
                if kind.inspect is None
            ),
            len(candidates),
        )
    ]
    config = settings.UPLOAD_CHECKS
    try:
        index = await run_checks(
            [kind.inspect for kind in taken],
            file,
            processes=config.PROCESSES,
            memory=config.MEMORY,
            seconds=config.SECONDS,
        )
    except NotOfType:
        raise _unsupported() from None
    except FileRefused as exc:
        message = str(exc.message)  # A lazy one's text, in the request's language.
        logger.info("An upload was refused by its check: %s", message)
        raise UploadRefused(
            status.HTTP_422_UNPROCESSABLE_CONTENT, message, code=exc.code
        ) from None
    return taken[index]


async def _head(upload: "_Reader | UploadFile") -> bytes:
    """The first ``SNIFF_BYTES`` of the upload (fewer if it is shorter)."""
    head = b""
    while len(head) < SNIFF_BYTES and (
        part := await upload.read(SNIFF_BYTES - len(head))
    ):
        head += part
    return head


def _unsupported() -> UploadRefused:
    """415: the upload is of no type the route takes."""
    return UploadRefused(
        status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        "This type of file is not accepted.",
        code="unsupported_file_type",
    )


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
        "Content-Disposition": content_disposition(filename or name, inline=inline),
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "X-Content-Type-Options": "nosniff",
    }
    return StreamingResponse(
        file.chunks(),
        media_type=content_type,
        headers=headers,
        background=BackgroundTask(file.close),
    )
