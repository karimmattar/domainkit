"""The storage port: where files live, under names the storage chooses.

``Storage`` checks names and makes new ones; a backend only moves bytes.
Names are paths of plain segments (letters, digits, ``_``, ``-``), the last
one with an optional extension: ``avatars/3f2a...9c.png``. Nothing a client
sends becomes part of one, so no name can climb out of the storage, hide a
file (``.htaccess``), or collide with another.
"""

import re
import secrets
from abc import ABC, abstractmethod
from collections.abc import AsyncIterable, AsyncIterator
from types import TracebackType
from typing import Final, Self

from domainkit.ddd import NotFound

CHUNK_SIZE: Final = 64 * 1024
"""Bytes read or written at a time."""
TOKEN_BYTES: Final = 16
"""Random bytes in every new name (128 bits): names cannot be guessed."""
_SEGMENT: Final = r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}"
DIRECTORY: Final = re.compile(rf"(?:{_SEGMENT}/){{0,7}}{_SEGMENT}")
"""Where a file may go: up to eight plain segments, such as ``avatars/2026``."""
EXTENSION: Final = re.compile(r"\.[a-z0-9]{1,10}")
NAME: Final = re.compile(rf"(?:{_SEGMENT}/){{0,8}}{_SEGMENT}(?:\.[a-z0-9]{{1,10}})?")
"""A name a storage accepts."""


class StorageError(Exception):
    """A storage refused an operation."""


class FileTooLarge(StorageError):
    """The content went over the size limit while it was saved; nothing was kept."""

    def __init__(self, limit: int) -> None:
        """Name the limit in bytes."""
        super().__init__(f"The file is larger than {limit} bytes.")
        self.limit = limit


class FileMissing(NotFound):
    """No file has the name (a malformed name names none): 404 over HTTP."""

    code = "file_not_found"

    def __init__(self) -> None:
        """Say so without repeating the name."""
        super().__init__("No file has that name.")


def check_name(name: str) -> str:
    """Return ``name`` if a storage could hold a file under it.

    Raises:
        FileMissing: It could not, so no file has it.
    """
    if not NAME.fullmatch(name):
        raise FileMissing
    return name


def new_name(directory: str = "", extension: str = "") -> str:
    """A new random name, in ``directory``, ending in ``extension`` (``.png``).

    Raises:
        ValueError: The directory or the extension is malformed.
    """
    if directory and not DIRECTORY.fullmatch(directory):
        raise ValueError(
            "A storage directory is up to 8 segments of letters, digits, _ and -."
        )
    if extension and not EXTENSION.fullmatch(extension):
        raise ValueError(
            "An extension is a dot and 1 to 10 lower-case letters or digits."
        )
    token = secrets.token_hex(TOKEN_BYTES)
    return f"{directory}/{token}{extension}" if directory else f"{token}{extension}"


class StoredFile(ABC):
    """A file open for reading; close it, or use it in ``async with``."""

    def __init__(self, name: str, size: int) -> None:
        """Describe the file ``name`` of ``size`` bytes."""
        self.name = name
        self.size = size

    @abstractmethod
    async def read(self, size: int = -1) -> bytes:
        """Up to ``size`` more bytes (the rest if negative); ``b""`` at the end."""

    async def chunks(self, chunk_size: int = CHUNK_SIZE) -> AsyncIterator[bytes]:
        """The rest of the file, a chunk at a time; the file is closed at the end."""
        try:
            while chunk := await self.read(chunk_size):
                yield chunk
        finally:
            await self.close()

    @abstractmethod
    async def close(self) -> None:
        """Release the file; closing it again does nothing."""

    async def __aenter__(self) -> Self:
        """Use the file in the block."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the file."""
        await self.close()


class Storage(ABC):
    """Files under names the storage chooses: save, open, delete, check, size.

    Subclasses implement the ``_`` methods; names reach them checked.
    """

    async def save(
        self,
        content: bytes | AsyncIterable[bytes],
        *,
        directory: str = "",
        extension: str = "",
        max_size: int | None = None,
    ) -> str:
        """Store ``content`` under a new random name, and return the name.

        The file appears whole or not at all. Past ``max_size`` bytes,
        saving stops and nothing is kept.

        Raises:
            FileTooLarge: The content went over ``max_size``.
            ValueError: ``directory`` or ``extension`` is malformed.
        """
        name = new_name(directory, extension)
        await self._write(name, _limited(_pieces(content), max_size))
        return name

    async def open(self, name: str) -> StoredFile:
        """Open the file ``name`` for reading.

        Raises:
            FileMissing: No file has the name.
        """
        return await self._open(check_name(name))

    async def delete(self, name: str) -> bool:
        """Delete the file ``name``; report whether it existed."""
        try:
            check_name(name)
        except FileMissing:
            return False
        return await self._delete(name)

    async def exists(self, name: str) -> bool:
        """Report whether a file has the name."""
        try:
            check_name(name)
        except FileMissing:
            return False
        return await self._exists(name)

    async def size(self, name: str) -> int:
        """The size of the file ``name``, in bytes.

        Raises:
            FileMissing: No file has the name.
        """
        return await self._size(check_name(name))

    def url(self, name: str) -> str | None:
        """Where clients can fetch the file themselves; None if only routes serve it."""
        check_name(name)
        return None

    @abstractmethod
    async def _write(self, name: str, chunks: AsyncIterator[bytes]) -> None:
        """Store ``chunks`` under the new ``name``, whole or not at all."""

    @abstractmethod
    async def _open(self, name: str) -> StoredFile:
        """Open ``name``, or raise ``FileMissing``."""

    @abstractmethod
    async def _delete(self, name: str) -> bool:
        """Delete ``name``; report whether it existed."""

    @abstractmethod
    async def _exists(self, name: str) -> bool:
        """Report whether ``name`` exists."""

    @abstractmethod
    async def _size(self, name: str) -> int:
        """The size of ``name``, or raise ``FileMissing``."""


async def _pieces(content: bytes | AsyncIterable[bytes]) -> AsyncIterator[bytes]:
    """``content`` as chunks of at most ``CHUNK_SIZE``, empty ones left out."""
    if isinstance(content, bytes):
        for start in range(0, len(content), CHUNK_SIZE):
            yield content[start : start + CHUNK_SIZE]
        return
    async for chunk in content:
        if chunk:
            yield chunk


async def _limited(
    chunks: AsyncIterator[bytes], max_size: int | None
) -> AsyncIterator[bytes]:
    """``chunks``, refused as soon as they go over ``max_size`` bytes."""
    received = 0
    async for chunk in chunks:
        received += len(chunk)
        if max_size is not None and received > max_size:
            raise FileTooLarge(max_size)
        yield chunk
