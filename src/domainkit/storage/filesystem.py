"""``FileSystemStorage``: files in a directory, written atomically, never through links.

Every path is walked one directory at a time from the storage's root, each
opened with ``O_NOFOLLOW``, and files are opened the same way, so a
symbolic link planted inside the storage (by another program, or a
restored backup) cannot lead a read, a write, or a deletion outside it.
Files are written to a hidden temporary name, synced, then renamed into
place: a reader sees a whole file or none, even after a crash. Leftover
temporary files start with a dot, so no name can reach them.

The blocking calls run in worker threads. Needs ``O_NOFOLLOW`` and
``dir_fd``: Linux, macOS, and the BSDs.
"""

import asyncio
import contextlib
import errno
import os
import secrets
import stat
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from urllib.parse import quote

from domainkit.exceptions import ImproperlyConfigured
from domainkit.storage.base import FileMissing, Storage, StorageError, StoredFile

_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_REFUSED = frozenset({errno.ELOOP, errno.EMLINK, errno.ENOTDIR})
"""What ``O_NOFOLLOW`` gives for a link (ELOOP; EMLINK on FreeBSD), or a file's
``O_DIRECTORY`` (ENOTDIR)."""


class FileSystemFile(StoredFile):
    """A stored file open for reading."""

    def __init__(self, name: str, size: int, descriptor: int) -> None:
        """Read from ``descriptor``, which this file closes."""
        super().__init__(name, size)
        self._descriptor: int | None = descriptor

    async def read(self, size: int = -1) -> bytes:
        """The next ``size`` bytes (the rest if negative)."""
        if self._descriptor is None:
            raise StorageError("The file is closed.")
        return await asyncio.to_thread(_read, self._descriptor, size)

    async def close(self) -> None:
        """Close the descriptor, once."""
        descriptor, self._descriptor = self._descriptor, None
        if descriptor is not None:
            os.close(descriptor)


class FileSystemStorage(Storage):
    """Files under ``location``, an absolute directory created on first save.

    ``base_url`` is where a web server or CDN serves the directory, if one
    does (``url`` returns None otherwise). Files get ``file_mode`` and
    directories ``directory_mode``, less the process's umask: by default
    only the service's own user can read them.
    """

    def __init__(
        self,
        location: str | Path,
        *,
        base_url: str | None = None,
        file_mode: int = 0o600,
        directory_mode: int = 0o700,
    ) -> None:
        """Keep files under ``location``.

        Raises:
            ImproperlyConfigured: ``location`` is relative.
        """
        self.location = Path(location)
        if not self.location.is_absolute():
            raise ImproperlyConfigured(
                f"FileSystemStorage needs an absolute location, not {str(location)!r}."
            )
        self.base_url = None if base_url is None else base_url.rstrip("/") + "/"
        self.file_mode = file_mode
        self.directory_mode = directory_mode

    def url(self, name: str) -> str | None:
        """``base_url`` and the name, if a web server serves the directory."""
        super().url(name)
        return None if self.base_url is None else self.base_url + quote(name)

    async def _write(self, name: str, chunks: AsyncIterator[bytes]) -> None:
        """Write a temporary file, sync it, and rename it into place."""
        *directories, filename = name.split("/")
        temporary = f".{filename}.{secrets.token_hex(4)}.part"
        folder, descriptor = await asyncio.to_thread(
            self._create, directories, temporary
        )
        try:
            async for chunk in chunks:
                await asyncio.to_thread(_write_all, descriptor, chunk)
            await asyncio.to_thread(os.fsync, descriptor)
        except BaseException:
            _discard(folder, descriptor, temporary)
            raise
        await asyncio.to_thread(_publish, folder, descriptor, temporary, filename)

    def _create(self, directories: Sequence[str], temporary: str) -> tuple[int, int]:
        """Make the directories, then the temporary file; return both descriptors."""
        self.location.mkdir(mode=self.directory_mode, parents=True, exist_ok=True)
        folder = self._walk(directories, create=True)
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
            return folder, os.open(temporary, flags, self.file_mode, dir_fd=folder)
        except BaseException:
            os.close(folder)
            raise

    def _walk(self, directories: Sequence[str], *, create: bool) -> int:
        """A descriptor of ``directories`` under the root, never through a link.

        Raises:
            FileMissing: A directory is missing (``create`` makes them).
            StorageError: A directory is a symbolic link, or not a directory.
        """
        try:
            folder = os.open(self.location, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        except FileNotFoundError:
            raise FileMissing from None
        try:
            for directory in directories:
                if create:
                    with contextlib.suppress(FileExistsError):
                        os.mkdir(directory, self.directory_mode, dir_fd=folder)
                inner = os.open(directory, _DIRECTORY, dir_fd=folder)
                os.close(folder)
                folder = inner
        except FileNotFoundError:
            os.close(folder)
            raise FileMissing from None
        except OSError as exc:
            os.close(folder)
            if exc.errno in _REFUSED:
                raise StorageError(
                    "A directory of the name is a symbolic link or a file; refused."
                ) from None
            raise
        return folder

    async def _open(self, name: str) -> StoredFile:
        """Open a regular file of the name for reading."""
        return await asyncio.to_thread(self._open_file, name)

    def _open_file(self, name: str) -> StoredFile:
        """Open ``name`` without following links; refuse anything but a file."""
        *directories, filename = name.split("/")
        folder = self._reach(directories)
        try:
            # O_NONBLOCK: opening a FIFO planted under the name must not hang.
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
            descriptor = os.open(filename, flags, dir_fd=folder)
        except OSError as exc:
            raise _missing(exc) from None
        finally:
            os.close(folder)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            os.close(descriptor)
            raise FileMissing
        return FileSystemFile(name, info.st_size, descriptor)

    def _reach(self, directories: Sequence[str]) -> int:
        """``_walk`` for reading: a link or a file on the way means no such file."""
        try:
            return self._walk(directories, create=False)
        except StorageError:
            raise FileMissing from None

    async def _delete(self, name: str) -> bool:
        """Unlink the file (a link of that name is unlinked, never followed)."""
        return await asyncio.to_thread(self._unlink, name)

    def _unlink(self, name: str) -> bool:
        """Remove ``name`` if it is a file or a link; report whether it was."""
        *directories, filename = name.split("/")
        try:
            folder = self._reach(directories)
        except FileMissing:
            return False
        try:
            info = os.stat(filename, dir_fd=folder, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                return False
            os.unlink(filename, dir_fd=folder)
        except FileNotFoundError:
            return False
        finally:
            os.close(folder)
        return True

    async def _exists(self, name: str) -> bool:
        """Whether a regular file has the name."""
        try:
            await self._size(name)
        except FileMissing:
            return False
        return True

    async def _size(self, name: str) -> int:
        """The size of the regular file ``name``."""
        return await asyncio.to_thread(self._stat_size, name)

    def _stat_size(self, name: str) -> int:
        """``lstat`` the name: links and directories are no files."""
        *directories, filename = name.split("/")
        folder = self._reach(directories)
        try:
            info = os.stat(filename, dir_fd=folder, follow_symlinks=False)
        except FileNotFoundError:
            raise FileMissing from None
        finally:
            os.close(folder)
        if not stat.S_ISREG(info.st_mode):
            raise FileMissing
        return info.st_size


def _missing(exc: OSError) -> Exception:
    """``FileMissing`` for a missing file or a link; other errors as they are."""
    if isinstance(exc, FileNotFoundError) or exc.errno in _REFUSED:
        return FileMissing()
    return exc


def _read(descriptor: int, size: int) -> bytes:
    """Read ``size`` bytes (all that is left if negative) from ``descriptor``."""
    if size >= 0:
        return os.read(descriptor, size)
    parts = []
    while part := os.read(descriptor, 1024 * 1024):
        parts.append(part)
    return b"".join(parts)


def _write_all(descriptor: int, data: bytes) -> None:
    """Write every byte of ``data`` (``os.write`` may write fewer)."""
    view = memoryview(data)
    while view:
        view = view[os.write(descriptor, view) :]


def _publish(folder: int, descriptor: int, temporary: str, filename: str) -> None:
    """Rename the synced file into place, and sync the directory that names it."""
    try:
        os.close(descriptor)
        try:
            os.rename(temporary, filename, src_dir_fd=folder, dst_dir_fd=folder)
        except BaseException:
            os.unlink(temporary, dir_fd=folder)
            raise
        os.fsync(folder)
    finally:
        os.close(folder)


def _discard(folder: int, descriptor: int, temporary: str) -> None:
    """Close and remove a temporary file that will not be kept."""
    try:
        os.close(descriptor)
        os.unlink(temporary, dir_fd=folder)
    finally:
        os.close(folder)
