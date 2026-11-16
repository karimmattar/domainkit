"""Stored files a committed change dropped, removed once no row keeps them.

Nothing removed a file once the change that replaced or deleted it
committed, nor checked that no other row still held its name; a project
wrote durable handlers for it, reading on the primary (user report U5).
``remove_unreferenced`` is that handler, deferred by the use case: it runs
after the commit, asks every column declared to keep the storage's names
on the primary, and deletes only what no row holds.
"""

from collections.abc import AsyncIterator, Iterator
from uuid import uuid4

import pytest
from sqlalchemy import String, insert, update
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.contrib.messaging import Worker
from domainkit.contrib.messaging.registry import get_task
from domainkit.contrib.messaging.tables import inbox, messages, ordering_keys
from domainkit.db import SqlAlchemyUnitOfWork, connections
from domainkit.exceptions import ImproperlyConfigured
from domainkit.storage import MemoryStorage, storages
from domainkit.storage.cleanup import (
    REMOVE_TASK,
    forget_files,
    keeps_files,
    remove_unreferenced,
)
from domainkit_tests.conftest import ALPHA, MakeDatabase

pytestmark = pytest.mark.anyio

MEMORY = {"default": {"BACKEND": "domainkit.storage.MemoryStorage"}}


class Rows(DeclarativeBase):
    """This file's tables, apart from every other."""


class DocumentRow(Rows):
    """A document, whose file is stored."""

    __tablename__ = "cleanup_documents"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    file: Mapped[str | None] = mapped_column(String(200))


class TemplateRow(Rows):
    """A template, whose preview may be a document's file."""

    __tablename__ = "cleanup_templates"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    preview: Mapped[str] = mapped_column(String(200))


@pytest.fixture(autouse=True)
def declared() -> Iterator[None]:
    """The two columns keep names of the default storage; forgotten after."""
    keeps_files(DocumentRow.file, TemplateRow.preview)
    yield
    forget_files()


@pytest.fixture
def replicated(make_database: MakeDatabase) -> Iterator[None]:
    """``default`` with a replica that has seen no commit; a memory storage."""
    primary, replica = make_database("primary"), make_database("replica")
    with override_settings(
        DATABASES={
            "default": {"URL": primary, "REPLICAS": ["copy"]},
            "copy": {"URL": replica},
        },
        STORAGES=MEMORY,
    ):
        storages.reset()
        yield


async def create(*aliases: str) -> None:
    """This file's tables on ``aliases``."""
    for alias in aliases:
        async with connections.engine(alias).begin() as connection:
            await connection.run_sync(Rows.metadata.create_all)


async def add(row: type[Rows], **values: str) -> None:
    """A committed row, on the primary."""
    async with SqlAlchemyUnitOfWork() as uow:
        await uow.session.execute(insert(row).values(id=uuid4().hex, **values))
        await uow.commit()


async def test_only_files_no_row_keeps_are_removed(replicated: None) -> None:
    """A document's file and a template's preview stay; the dropped one goes.

    The rows are read on the primary: the replica, which has seen none of
    them, would have every file removed.
    """
    await create("default", "copy")
    storage = storages["default"]
    assert isinstance(storage, MemoryStorage)
    kept, previewed, dropped = [await storage.save(b"%PDF") for _ in range(3)]
    await add(DocumentRow, file=kept)
    await add(TemplateRow, preview=previewed)

    await remove_unreferenced([kept, previewed, dropped, dropped, "never-stored"])

    assert set(storage.files) == {kept, previewed}
    await connections.close_all()


async def test_nothing_is_removed_where_no_column_is_declared(
    replicated: None,
) -> None:
    """No column for the storage: refused, rather than delete files rows hold."""
    storage = storages["default"]
    name = await storage.save(b"%PDF")
    forget_files("default")

    with pytest.raises(ImproperlyConfigured, match="No column keeps names of the"):
        await remove_unreferenced([name])
    with pytest.raises(TypeError, match="keeps_files takes mapped attributes"):
        keeps_files(DocumentRow.__table__.c.file)

    assert await storage.exists(name)


def test_a_column_is_declared_once() -> None:
    """Declared again, by the same or another module: kept once."""
    from domainkit.storage import cleanup

    keeps_files(DocumentRow.file)
    keeps_files(DocumentRow.file, storage="media")

    assert cleanup._kept["default"] == [DocumentRow.file, TemplateRow.preview]
    assert cleanup._kept["media"] == [DocumentRow.file]


@pytest.fixture
async def worker_tables(database_url: str) -> AsyncIterator[None]:
    """Messaging installed, its outbox and this file's tables, a memory storage."""
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=["domainkit.contrib.messaging", ALPHA],
        DATABASES={"default": {"URL": database_url}},
        STORAGES=MEMORY,
    ):
        setup()
        storages.reset()
        async with connections.engine().begin() as connection:
            for table in (messages, inbox, ordering_keys):
                await connection.run_sync(table.create)
            await connection.run_sync(Rows.metadata.create_all)
        yield
        await connections.close_all()
    registry.clear()


async def test_a_replaced_file_is_removed_once_the_change_commits(
    worker_tables: None,
) -> None:
    """Deferred by the use case, run by the worker after the commit.

    A change that rolls back defers nothing: its file stays.
    """
    storage = storages["default"]
    assert isinstance(storage, MemoryStorage)
    old, new, kept = [await storage.save(b"%PDF") for _ in range(3)]
    async with SqlAlchemyUnitOfWork() as uow:
        await uow.session.execute(insert(DocumentRow).values(id="doc", file=old))
        await uow.commit()
    async with SqlAlchemyUnitOfWork() as uow:
        await uow.session.execute(update(DocumentRow).values(file=kept))
        uow.defer(remove_unreferenced, names=[old])
        # Rolled back: never committed.
    async with SqlAlchemyUnitOfWork() as uow:
        await uow.session.execute(update(DocumentRow).values(file=new))
        uow.defer(remove_unreferenced, names=[old])
        await uow.commit()

    assert await storage.exists(old)
    assert await Worker().run_once() == 1
    assert set(storage.files) == {new, kept}
    spec = get_task(REMOVE_TASK)
    assert spec is not None
    assert spec.function is remove_unreferenced
