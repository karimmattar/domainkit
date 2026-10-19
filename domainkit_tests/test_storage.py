"""Storages: names, atomic writes, size limits, and links that lead nowhere."""

import asyncio
import errno
import os
import re
import stat
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from domainkit.conf import Settings, StorageConfig, override_settings
from domainkit.exceptions import ImproperlyConfigured
from domainkit.storage import (
    FileMissing,
    FileSystemStorage,
    FileTooLarge,
    MemoryStorage,
    Storage,
    StorageError,
    check_name,
    new_name,
    storages,
)
from domainkit.storage.base import CHUNK_SIZE

pytestmark = pytest.mark.anyio

TOKEN = r"[0-9a-f]{32}"


@pytest.fixture(params=["filesystem", "memory"])
def storage(request: pytest.FixtureRequest, tmp_path: Path) -> Storage:
    """Each backend: files in a new directory, or in memory."""
    if request.param == "filesystem":
        return FileSystemStorage(tmp_path / "files")
    return MemoryStorage()


@pytest.fixture
def files(tmp_path: Path) -> FileSystemStorage:
    """A file system storage in a new directory."""
    return FileSystemStorage(tmp_path / "files")


async def chunks(*parts: bytes) -> AsyncIterator[bytes]:
    """``parts``, one at a time."""
    for part in parts:
        yield part


def leftovers(root: Path) -> list[str]:
    """Every file and directory under ``root``, relative."""
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


# Names -------------------------------------------------------------------------------


def test_new_names_are_random_and_placed() -> None:
    """128 random bits, in the directory, with the extension."""
    names = {new_name() for _ in range(100)}

    assert len(names) == 100
    assert all(re.fullmatch(TOKEN, name) for name in names)
    assert re.fullmatch(rf"avatars/2026/{TOKEN}\.png", new_name("avatars/2026", ".png"))
    assert re.fullmatch(rf"{TOKEN}\.tar", new_name(extension=".tar"))
    deepest = "/".join(["d"] * 8)
    assert new_name(deepest).startswith(f"{deepest}/")


@pytest.mark.parametrize(
    "directory", ["../x", "a/", "/a", ".a", "a//b", "a b", "a\\b", "/".join(["d"] * 9)]
)
def test_directories_are_plain_segments(directory: str) -> None:
    """No dots first, no empty or absolute parts, at most eight segments."""
    with pytest.raises(ValueError, match="up to 8 segments"):
        new_name(directory)


@pytest.mark.parametrize("extension", ["png", ".PNG", ".tar.gz", ".", ".abcdefghijk"])
def test_extensions_are_one_lower_case_suffix(extension: str) -> None:
    """A dot and 1 to 10 lower-case letters or digits."""
    with pytest.raises(ValueError, match="An extension is a dot"):
        new_name(extension=extension)


@pytest.mark.parametrize(
    "name",
    [
        "",
        "../etc/passwd",
        "a/../b",
        "/etc/passwd",
        ".hidden",
        "a/.part",
        "a//b",
        "a\\b",
        "a.PNG",
        "a.png.exe",
        "a\0b",
        "x" * 129,
        "/".join(["d"] * 10),
        "dir/",
    ],
)
async def test_malformed_names_name_no_file(storage: Storage, name: str) -> None:
    """They cannot be read, sized, or linked to, and deleting them does nothing."""
    with pytest.raises(FileMissing):
        check_name(name)
    with pytest.raises(FileMissing):
        await storage.open(name)
    with pytest.raises(FileMissing):
        await storage.size(name)
    with pytest.raises(FileMissing):
        storage.url(name)
    assert await storage.exists(name) is False
    assert await storage.delete(name) is False


# Both backends -----------------------------------------------------------------------


async def test_files_are_saved_read_and_deleted(storage: Storage) -> None:
    """Bytes in, the same bytes out, then gone."""
    data = bytes(range(256)) * 600  # More than one chunk.
    name = await storage.save(data, directory="docs", extension=".bin")

    assert re.fullmatch(rf"docs/{TOKEN}\.bin", name)
    assert await storage.exists(name) is True
    assert await storage.size(name) == len(data)
    async with await storage.open(name) as file:
        assert (file.name, file.size) == (name, len(data))
        assert await file.read(10) == data[:10]
        assert await file.read() == data[10:]
        assert await file.read() == b""
    reread = await storage.open(name)
    assert b"".join([chunk async for chunk in reread.chunks(1000)]) == data
    assert storage.url(name) is None
    assert await storage.delete(name) is True
    assert await storage.delete(name) is False
    assert await storage.exists(name) is False
    with pytest.raises(FileMissing):
        await storage.open(name)
    with pytest.raises(FileMissing):
        await storage.size(name)


async def test_streams_are_saved_chunk_by_chunk(storage: Storage) -> None:
    """Async iterables are saved as they come; empty chunks are skipped."""
    name = await storage.save(chunks(b"he", b"", b"llo"))
    empty = await storage.save(b"")

    assert re.fullmatch(TOKEN, name)
    async with await storage.open(name) as file:
        assert await file.read() == b"hello"
    assert await storage.size(empty) == 0


async def test_size_limits_keep_nothing_larger(storage: Storage) -> None:
    """At the limit a file is saved; one byte over, saving stops and nothing is kept."""
    exact = await storage.save(b"x" * 10, max_size=10)
    with pytest.raises(FileTooLarge, match="larger than 10 bytes") as refused:
        await storage.save(chunks(b"x" * 6, b"x" * 5), max_size=10)
    with pytest.raises(FileTooLarge):
        await storage.save(b"x" * (CHUNK_SIZE + 1), max_size=CHUNK_SIZE)

    assert refused.value.limit == 10
    assert await storage.size(exact) == 10
    if isinstance(storage, MemoryStorage):
        assert list(storage.files) == [exact]
        storage.reset()
        assert storage.files == {}
    else:
        assert isinstance(storage, FileSystemStorage)
        assert leftovers(storage.location) == [exact]


# The file system ---------------------------------------------------------------------


def test_locations_are_absolute(tmp_path: Path) -> None:
    """A relative location would depend on the working directory."""
    with pytest.raises(ImproperlyConfigured, match="absolute location"):
        FileSystemStorage("media")
    assert FileSystemStorage(str(tmp_path)).location == tmp_path


async def test_files_and_directories_are_private(files: FileSystemStorage) -> None:
    """Only the service's user may read them, by default."""
    previous = os.umask(0o022)
    try:
        name = await files.save(b"x", directory="a/b", extension=".txt")
    finally:
        os.umask(previous)
    path = files.location / name

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(files.location.stat().st_mode) == 0o700
    public = FileSystemStorage(files.location, file_mode=0o644, directory_mode=0o755)
    other = await public.save(b"x", directory="c")
    assert stat.S_IMODE((files.location / other).stat().st_mode) == 0o644 & ~0o022


async def test_failed_writes_leave_nothing(files: FileSystemStorage) -> None:
    """A stream that fails, or a cancelled save, removes its temporary file."""

    async def failing() -> AsyncIterator[bytes]:
        yield b"partial"
        raise RuntimeError("client went away")

    with pytest.raises(RuntimeError, match="went away"):
        await files.save(failing(), directory="tmp")

    started = asyncio.Event()

    async def slow() -> AsyncIterator[bytes]:
        yield b"partial"
        started.set()
        await asyncio.sleep(60)
        yield b"never"

    task = asyncio.create_task(files.save(slow()))
    await started.wait()
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert leftovers(files.location) == ["tmp"]


async def test_a_failed_rename_leaves_nothing(
    files: FileSystemStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the file cannot be put in place, its temporary copy goes."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(os, "rename", refuse)
    with pytest.raises(OSError, match="cross-device"):
        await files.save(b"data")

    assert leftovers(files.location) == []


async def test_links_inside_the_storage_lead_nowhere(
    files: FileSystemStorage, tmp_path: Path
) -> None:
    """Files and directories that are symbolic links are neither read nor written."""
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"password")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / ("a" * 32)).write_bytes(b"x")
    name = await files.save(b"mine", directory="docs")
    linked = files.location / "docs" / ("b" * 32)
    linked.symlink_to(secret)
    (files.location / "escape").symlink_to(outside)

    with pytest.raises(FileMissing):
        await files.open(f"docs/{'b' * 32}")
    with pytest.raises(FileMissing):
        await files.size(f"docs/{'b' * 32}")
    assert await files.exists(f"docs/{'b' * 32}") is False
    with pytest.raises(FileMissing):
        await files.open(f"escape/{'a' * 32}")
    assert await files.exists(f"escape/{'a' * 32}") is False
    assert await files.delete(f"escape/{'a' * 32}") is False
    with pytest.raises(StorageError, match="symbolic link or a file"):
        await files.save(b"x", directory="escape")
    with pytest.raises(StorageError, match="symbolic link or a file"):
        await files.save(b"x", directory=name)  # A file where a directory would go.
    assert await files.delete(f"docs/{'b' * 32}") is True  # The link, not its target.

    assert secret.read_bytes() == b"password"
    assert leftovers(outside) == ["a" * 32]
    assert await files.size(name) == 4


async def test_only_regular_files_are_files(files: FileSystemStorage) -> None:
    """A directory or a FIFO under a file's name is no file, and never hangs a read."""
    await files.save(b"x", directory="d")
    (files.location / "d" / "fifo").parent.mkdir(exist_ok=True)
    os.mkfifo(files.location / "d" / "fifo")
    (files.location / "d" / "folder").mkdir()

    for name in ("d/fifo", "d/folder", "d"):
        with pytest.raises(FileMissing):
            await files.open(name)
        assert await files.exists(name) is False
    assert await files.delete("d/folder") is False
    assert await files.delete("d/fifo") is True


async def test_a_missing_root_holds_no_files(tmp_path: Path) -> None:
    """Nothing is created until the first save."""
    files = FileSystemStorage(tmp_path / "later")

    with pytest.raises(FileMissing):
        await files.open("a" * 32)
    assert await files.exists("x/y") is False
    assert await files.delete("x/y") is False
    assert not (tmp_path / "later").exists()


async def test_other_errors_are_not_hidden(
    files: FileSystemStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal by the system (permissions, a full disk) is raised as it is."""
    name = await files.save(b"x", directory="d")
    real_open = os.open

    def denied(path: str | Path, flags: int, *args: object, **kwargs: object) -> int:
        if str(path) != str(files.location):
            raise PermissionError(errno.EACCES, "denied")
        return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", denied)
    with pytest.raises(PermissionError):
        await files.open(name)
    with pytest.raises(PermissionError):
        await files.open("a" * 32)
    with pytest.raises(PermissionError):
        await files.save(b"x")
    monkeypatch.undo()
    assert leftovers(files.location) == ["d", name]


async def test_closed_files_cannot_be_read(files: FileSystemStorage) -> None:
    """Closing twice is harmless; reading after closing is refused."""
    file = await files.open(await files.save(b"x"))
    await file.close()
    await file.close()

    with pytest.raises(StorageError, match="closed"):
        await file.read()


def test_urls_point_where_a_web_server_serves_files(tmp_path: Path) -> None:
    """Only with a ``base_url``; names are quoted."""
    served = FileSystemStorage(tmp_path, base_url="https://cdn.example.com/media")
    assert served.url("a/b.png") == "https://cdn.example.com/media/a/b.png"
    assert FileSystemStorage(tmp_path).url("a/b.png") is None


# Settings and the handler ------------------------------------------------------------


@pytest.fixture
def fresh_storages() -> Iterator[None]:
    """Forget the storages made."""
    storages.reset()
    yield
    storages.reset()


def test_storages_are_made_from_settings(tmp_path: Path, fresh_storages: None) -> None:
    """One per alias, made again when its settings change."""
    configured = {
        "default": {"OPTIONS": {"location": str(tmp_path)}},
        "scratch": {"BACKEND": "domainkit.storage.MemoryStorage"},
    }
    with override_settings(STORAGES=configured):
        default = storages["default"]
        assert isinstance(default, FileSystemStorage)
        assert default.location == tmp_path
        assert storages["default"] is default
        assert isinstance(storages["scratch"], MemoryStorage)
        with pytest.raises(ImproperlyConfigured, match="STORAGES has no 'missing'"):
            storages["missing"]
    with override_settings(STORAGES={"default": {"OPTIONS": {"location": "/srv"}}}):
        changed = storages["default"]
        assert isinstance(changed, FileSystemStorage)
        assert changed is not default
    with override_settings(STORAGES={}), pytest.raises(ImproperlyConfigured):
        storages["default"]


@pytest.mark.parametrize(
    ("backend", "message"),
    [
        ("no.such.Storage", r"STORAGES\['x'\].BACKEND: "),
        ("domainkit.cache.DummyStore", "is not a Storage"),
    ],
)
def test_backends_must_be_storages(
    backend: str, message: str, fresh_storages: None
) -> None:
    """A wrong path or class fails clearly."""
    with (
        override_settings(STORAGES={"x": {"BACKEND": backend, "OPTIONS": {}}}),
        pytest.raises(ImproperlyConfigured, match=message),
    ):
        storages["x"]


def test_options_must_fit_the_backend(fresh_storages: None) -> None:
    """Missing or unknown arguments name the setting."""
    for options in ({}, {"location": "/srv", "colour": "blue"}):
        with (
            override_settings(STORAGES={"x": {"OPTIONS": options}}),
            pytest.raises(ImproperlyConfigured, match=r"STORAGES\['x'\].OPTIONS: "),
        ):
            storages["x"]


def test_storage_settings() -> None:
    """None by default; unknown keys refused; memory refused when deployed."""
    assert Settings().STORAGES == {}
    assert StorageConfig().BACKEND == "domainkit.storage.FileSystemStorage"
    with pytest.raises(ValidationError, match="Extra inputs"):
        StorageConfig.model_validate({"LOCATION": "/srv"})
    base = {"ENVIRONMENT": "production", "ALLOWED_HOSTS": ["api.example.com"]}
    memory = {"BACKEND": "domainkit.storage.MemoryStorage"}
    with pytest.raises(
        ValidationError, match=r"memory storages .* STORAGES \['scratch'\]"
    ):
        Settings.model_validate(
            {**base, "STORAGES": {"default": {}, "scratch": memory}}
        )
    assert Settings.model_validate({**base, "STORAGES": {"default": {}}})
