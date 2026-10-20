"""Files: a storage port, on the file system or in memory.

Configure storages in ``STORAGES``, then save and read files by the names
the storage gives them::

    from domainkit.storage import storages

    storage = storages["default"]
    name = await storage.save(content, directory="invoices", extension=".pdf")
    async with await storage.open(name) as file:
        data = await file.read()

Names are random and checked, files appear whole, and nothing is followed
through symbolic links. Receiving uploads and serving files over HTTP is in
``domainkit.http.uploads``; see ``docs/uploads.md`` and ADR 0040.
"""

from domainkit.storage.base import (
    FileMissing,
    FileTooLarge,
    Storage,
    StorageError,
    StoredFile,
    check_name,
    new_name,
)
from domainkit.storage.filesystem import FileSystemStorage
from domainkit.storage.handler import StorageHandler, storages
from domainkit.storage.memory import MemoryStorage

__all__ = [
    "FileMissing",
    "FileSystemStorage",
    "FileTooLarge",
    "MemoryStorage",
    "Storage",
    "StorageError",
    "StorageHandler",
    "StoredFile",
    "check_name",
    "new_name",
    "storages",
]
