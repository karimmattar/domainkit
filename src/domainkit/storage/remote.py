"""What object stores share: ranged reads, parts, presigned URLs.

A file is read a block at a time, each block one ranged request, so a
large file never sits in memory and every request stays within
``HTTP_CLIENT.MAX_RESPONSE_BYTES``. A file is written in one request
when it fits in a part, else as a multipart upload whose parts are sent
as they fill and which is aborted if the content fails: it appears whole
or not at all, as every storage's files do.

``ObjectStorage`` subclasses (``S3Storage``, ``GCSStorage``,
``AzureBlobStorage``) only speak their service's protocol.
"""

from abc import abstractmethod
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Final

from domainkit.storage.base import FileMissing, Storage, StoredFile

__all__ = ["ObjectStorage", "PresignedUpload"]

PART_SIZE: Final = 8 * 1024 * 1024
"""Bytes in each part of a multipart upload (above every service's minimum)."""
BLOCK_SIZE: Final = 4 * 1024 * 1024
"""Bytes read per ranged request: under ``MAX_RESPONSE_BYTES``'s default."""
MAX_EXPIRY: Final = timedelta(days=7)
"""The longest a presigned URL may work (S3's and GCS's limit)."""


@dataclass(frozen=True, slots=True)
class PresignedUpload:
    """Where a client sends a new file itself, and what it must send with it.

    The client sends ``method`` to ``url`` with ``headers`` (signed: a
    different size or type is refused) and the file as the body. Keep
    ``name``: the file has it once the client is done.
    """

    name: str
    url: str
    method: str = "PUT"
    headers: dict[str, str] = field(default_factory=dict)


class ObjectFile(StoredFile):
    """A remote file, read one block (one ranged request) at a time."""

    def __init__(self, storage: "ObjectStorage", name: str, size: int) -> None:
        """Read ``name`` of ``size`` bytes from the start."""
        super().__init__(name, size)
        self._storage = storage
        self._position = 0
        self._buffer = b""

    async def read(self, size: int = -1) -> bytes:
        """Up to ``size`` more bytes (the rest if negative); ``b""`` at the end."""
        wanted = self.size - self._position - len(self._buffer)
        if size >= 0:
            wanted = min(wanted, size - len(self._buffer))
        while wanted > 0:
            start = self._position + len(self._buffer)
            end = min(start + max(wanted, BLOCK_SIZE), self.size) - 1
            block = await self._storage._range(self.name, start, end)
            if not block:
                break
            self._buffer += block
            wanted -= len(block)
        taken = self._buffer if size < 0 else self._buffer[:size]
        self._buffer = self._buffer[len(taken) :]
        self._position += len(taken)
        return taken

    async def close(self) -> None:
        """Nothing is held open between requests."""
        self._buffer = b""


class ObjectStorage(Storage):
    """A storage in an object store; subclasses speak its protocol.

    ``prefix`` puts every name under a folder of the bucket or container;
    ``public_url``, where clients fetch files directly (a CDN), makes
    ``url(name)`` return ``<public_url>/<prefix><name>``.
    """

    part_size: int = PART_SIZE

    def __init__(self, *, prefix: str = "", public_url: str | None = None) -> None:
        """Keep files under ``prefix`` (``"media/"``); serve them from ``public_url``.

        Raises:
            ValueError: ``prefix`` does not end in ``/``, or has ``..`` in it.
        """
        if prefix and (not prefix.endswith("/") or ".." in prefix.split("/")):
            raise ValueError("A storage prefix ends in '/' and climbs nowhere.")
        self.prefix = prefix
        self.public_url = None if public_url is None else public_url.rstrip("/")

    def key(self, name: str) -> str:
        """The object's key in the store: the prefix, then the name."""
        return f"{self.prefix}{name}"

    def url(self, name: str) -> str | None:
        """``public_url`` and the key, if files are public there; else None."""
        super().url(name)
        if self.public_url is None:
            return None
        return f"{self.public_url}/{self.key(name)}"

    async def presigned_url(
        self, name: str, *, expires: timedelta = timedelta(minutes=15)
    ) -> str:
        """A URL that fetches ``name`` for ``expires``, without credentials.

        Raises:
            FileMissing: The name is malformed (whether it exists is not asked).
            ValueError: ``expires`` is not between one second and seven days.
        """
        from domainkit.storage.base import check_name

        return await self._presign(check_name(name), "GET", _bounded(expires), {})

    async def presigned_upload(
        self,
        *,
        size: int,
        content_type: str,
        directory: str = "",
        extension: str = "",
        expires: timedelta = timedelta(minutes=15),
    ) -> PresignedUpload:
        """A new name, and a URL taking ``size`` bytes of ``content_type``.

        The size and type are signed (Azure's SAS cannot bind them: check
        the file with ``size()`` once the client says it is done), so the
        URL stores nothing else.

        Raises:
            ValueError: ``size`` is not positive, ``expires`` is out of
                range, or ``directory`` or ``extension`` is malformed.
        """
        from domainkit.storage.base import new_name

        if size <= 0:
            raise ValueError("A presigned upload takes a positive size.")
        name = new_name(directory, extension)
        headers = {"Content-Length": str(size), "Content-Type": content_type}
        url = await self._presign(name, "PUT", _bounded(expires), headers)
        return PresignedUpload(name, url, "PUT", self._upload_headers(headers))

    def _upload_headers(self, headers: dict[str, str]) -> dict[str, str]:
        """The headers a client sends with a presigned upload."""
        return dict(headers)

    async def _write(self, name: str, chunks: AsyncIterator[bytes]) -> None:
        """One request if the content fits a part; else a multipart upload."""
        reader = _Parts(chunks, self.part_size)
        first = await reader.next()
        if not reader.more:
            await self._put(name, first)
            return
        upload = await self._start(name)
        parts: list[str] = []
        try:
            data = first
            while data:
                parts.append(await self._part(name, upload, len(parts) + 1, data))
                data = await reader.next()
            await self._finish(name, upload, parts)
        except BaseException:
            await self._abort(name, upload)
            raise

    async def _open(self, name: str) -> StoredFile:
        """The file, read a block at a time."""
        size = await self._head(name)
        if size is None:
            raise FileMissing
        return ObjectFile(self, name, size)

    async def _exists(self, name: str) -> bool:
        """Whether the object is there."""
        return await self._head(name) is not None

    async def _size(self, name: str) -> int:
        """The object's size."""
        size = await self._head(name)
        if size is None:
            raise FileMissing
        return size

    @abstractmethod
    async def _put(self, name: str, data: bytes) -> None:
        """Store ``data`` under ``name`` in one request."""

    @abstractmethod
    async def _start(self, name: str) -> str:
        """Begin a multipart upload of ``name``; its id."""

    @abstractmethod
    async def _part(self, name: str, upload: str, number: int, data: bytes) -> str:
        """Send part ``number`` (from 1); what finishing names it by."""

    @abstractmethod
    async def _finish(self, name: str, upload: str, parts: Sequence[str]) -> None:
        """Join the parts: the object appears now, whole."""

    @abstractmethod
    async def _abort(self, name: str, upload: str) -> None:
        """Drop the parts sent; the object never appears."""

    @abstractmethod
    async def _head(self, name: str) -> int | None:
        """The object's size; None if there is none."""

    @abstractmethod
    async def _range(self, name: str, start: int, end: int) -> bytes:
        """Bytes ``start`` to ``end``, both included."""

    @abstractmethod
    async def _presign(
        self, name: str, method: str, expires: timedelta, headers: dict[str, str]
    ) -> str:
        """A URL for ``method`` on ``name``, with ``headers`` signed."""


class _Parts:
    """``chunks`` regrouped in parts of ``size`` bytes, the last one shorter."""

    def __init__(self, chunks: AsyncIterator[bytes], size: int) -> None:
        """Read ``chunks`` a part at a time."""
        self._chunks = chunks
        self._size = size
        self._rest = b""
        self.more = True
        """Whether anything may follow the part last returned."""

    async def next(self) -> bytes:
        """The next part; ``b""`` once everything was read.

        A part shorter than ``size`` is the last: ``more`` is then False.
        """
        held = bytearray(self._rest)
        if len(held) < self._size:
            async for chunk in self._chunks:
                held += chunk
                if len(held) >= self._size:
                    break
            else:
                self.more = False
        part, self._rest = bytes(held[: self._size]), bytes(held[self._size :])
        return part


def _bounded(expires: timedelta) -> timedelta:
    """``expires``, if between one second and seven days.

    Raises:
        ValueError: It is not.
    """
    if not timedelta(seconds=1) <= expires <= MAX_EXPIRY:
        raise ValueError("A presigned URL works for one second to seven days.")
    return expires
