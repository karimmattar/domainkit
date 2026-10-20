"""``MemoryStorage``: files in a dictionary, for tests and development."""

from collections.abc import AsyncIterator

from domainkit.storage.base import FileMissing, Storage, StoredFile


class MemoryFile(StoredFile):
    """A stored file's bytes, read from memory."""

    def __init__(self, name: str, data: bytes) -> None:
        """Read ``data`` from the start."""
        super().__init__(name, len(data))
        self._data = data
        self._position = 0

    async def read(self, size: int = -1) -> bytes:
        """The next ``size`` bytes (the rest if negative)."""
        end = len(self._data) if size < 0 else self._position + size
        chunk = self._data[self._position : end]
        self._position += len(chunk)
        return chunk

    async def close(self) -> None:
        """Nothing to release."""


class MemoryStorage(Storage):
    """Files in this process's memory; gone when it ends. Not for deployments."""

    def __init__(self) -> None:
        """Start empty."""
        self.files: dict[str, bytes] = {}

    async def _write(self, name: str, chunks: AsyncIterator[bytes]) -> None:
        """Keep the file once every chunk arrived."""
        self.files[name] = b"".join([chunk async for chunk in chunks])

    async def _open(self, name: str) -> StoredFile:
        """Read the file from memory."""
        try:
            return MemoryFile(name, self.files[name])
        except KeyError:
            raise FileMissing from None

    async def _delete(self, name: str) -> bool:
        """Forget the file."""
        return self.files.pop(name, None) is not None

    async def _exists(self, name: str) -> bool:
        """Whether the file is kept."""
        return name in self.files

    async def _size(self, name: str) -> int:
        """The file's length."""
        try:
            return len(self.files[name])
        except KeyError:
            raise FileMissing from None

    def reset(self) -> None:
        """Forget every file (tests)."""
        self.files.clear()
