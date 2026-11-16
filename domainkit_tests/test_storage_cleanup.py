"""Stored files a committed change dropped, removed once no row keeps them.

Nothing removed a file once the change that replaced or deleted it
committed, nor checked that no other row still held its name; a project
wrote durable handlers for it, reading on the primary (user report U5).
``remove_unreferenced`` is that handler, deferred by the use case: it runs
after the commit, asks every column declared to keep the storage's names
on the primary, and deletes only what no row holds.
"""

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from uuid import uuid4

import pytest
from sqlalchemy import Column, MetaData, String, Table, insert, update
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.orm import registry as mappers

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.contrib.messaging import Worker
from domainkit.contrib.messaging.registry import get_task
from domainkit.contrib.messaging.tables import inbox, messages, ordering_keys
from domainkit.db import SqlAlchemyUnitOfWork, connections
from domainkit.exceptions import ImproperlyConfigured
from domainkit.storage import MemoryStorage, cleanup, storages
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


shares = Table("cleanup_shares", Rows.metadata, Column("file", String(200)))
"""A link table with no class, and no domain: read on ``default``."""


@pytest.fixture(autouse=True)
def declared() -> Iterator[None]:
    """Three columns keep names of the default storage; forgotten after."""
    keeps_files(DocumentRow.file, TemplateRow.preview, shares.c.file)
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
    kept, previewed, shared, dropped = [await storage.save(b"%PDF") for _ in range(4)]
    await add(DocumentRow, file=kept)
    await add(TemplateRow, preview=previewed)
    async with SqlAlchemyUnitOfWork() as uow:
        await uow.session.execute(insert(shares).values(file=shared))
        await uow.commit()

    await remove_unreferenced(
        [kept, previewed, shared, dropped, dropped, "never-stored"]
    )

    assert set(storage.files) == {kept, previewed, shared}
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

    @dataclass
    class Unmapped:
        file: str | None = None

    for wrong in (None, "file", Column("file", String(200)), Unmapped.file):
        with pytest.raises(TypeError, match="keeps_files takes a table's column"):
            keeps_files(wrong)

    assert await storage.exists(name)


def test_a_column_is_declared_once() -> None:
    """Declared again, as its attribute or as its table's column: kept once."""
    documents = DocumentRow.__table__.c
    keeps_files(DocumentRow.file)
    keeps_files(documents.file, storage="media")
    keeps_files(DocumentRow.file, storage="media")

    assert [column for column, _ in cleanup._kept["default"]] == [
        documents.file,
        TemplateRow.__table__.c.preview,
        shares.c.file,
    ]
    assert cleanup._kept["media"] == [(documents.file, None)]


@dataclass
class Contract:
    """An entity of the framework's layout, mapped onto its table imperatively."""

    id: str
    file: str | None = None


contracts = Table(
    "cleanup_contracts",
    MetaData(),
    Column("id", String(32), primary_key=True),
    Column("file", String(200)),
    info={"domain": "contracts"},
)
attachments = Table(
    "cleanup_attachments",
    contracts.metadata,
    Column("contract_id", String(32)),
    Column("file", String(200)),
    info={"domain": "contracts"},
)
"""A link table: no class is mapped onto it."""


async def test_columns_are_declared_beside_tables_as_the_framework_lays_them_out(
    make_database: MakeDatabase,
) -> None:
    """``table_for`` tables, mapped in ``start_mappers()``: their columns count.

    The entity's attribute is no column before it is mapped, and a link
    table has no class: each table's column is declared beside it, and
    read on the database its domain is routed to (user report U5).
    """
    keeps_files(contracts.c.file, attachments.c.file, storage="contracts")
    mapper = mappers()
    mapper.map_imperatively(Contract, contracts)
    try:
        keeps_files(Contract.file, storage="contracts")
    finally:
        mapper.dispose()
    assert [column for column, _ in cleanup._kept["contracts"]] == [
        contracts.c.file,
        attachments.c.file,
    ]
    with override_settings(
        DATABASES={
            "default": {"URL": make_database("default")},
            "paper": {"URL": make_database("paper")},
        },
        DATABASE_ROUTING={"contracts": "paper"},
        STORAGES={"contracts": {"BACKEND": "domainkit.storage.MemoryStorage"}},
    ):
        storages.reset()
        async with connections.engine("paper").begin() as connection:
            await connection.run_sync(contracts.metadata.create_all)
            signed, attached, dropped = [
                await storages["contracts"].save(b"%PDF") for _ in range(3)
            ]
            await connection.execute(insert(contracts).values(id="c", file=signed))
            await connection.execute(
                insert(attachments).values(contract_id="c", file=attached)
            )

        await remove_unreferenced([signed, attached, dropped, None], "contracts")

        storage = storages["contracts"]
        assert isinstance(storage, MemoryStorage)
        assert set(storage.files) == {signed, attached}
        await connections.close_all()


async def test_an_empty_name_is_passed_over(replicated: None) -> None:
    """A nullable column's ``None``, deferred as is: nothing asked or deleted."""
    await remove_unreferenced([None, ""])


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


SCRIPT = """
import sys
from domainkit.contrib.messaging import registry
from domainkit.exceptions import ImproperlyConfigured

assert "domainkit.storage" not in sys.modules, "loaded at import"
{body}
"""


def _fresh(body: str) -> None:
    """Run ``body`` in a new interpreter, where no test registered the task."""
    import os
    import subprocess
    import sys

    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("DOMAINKIT_")
    }
    done = subprocess.run(  # noqa: S603 - the test interpreter and a fixed script.
        [sys.executable, "-c", SCRIPT.format(body=body)],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr


def test_the_task_is_registered_when_a_worker_first_asks_for_it() -> None:
    """Nothing of storage loads until the task's key is asked for (PERF-15)."""
    _fresh(
        "spec = registry.get_task('domainkit.storage.remove_unreferenced')\n"
        "from domainkit.storage.cleanup import remove_unreferenced\n"
        "assert spec is not None and spec.function is remove_unreferenced\n"
        "assert registry.get_task('domainkit.storage.remove_unreferenced') is spec\n"
        "assert registry.get_task('nobody.knows') is None\n"
    )


def test_the_task_is_registered_when_first_deferred() -> None:
    """``uow.defer`` finds the task by its function, registering it then."""
    _fresh(
        "from domainkit.storage.cleanup import remove_unreferenced\n"
        "spec = registry.spec_for(remove_unreferenced)\n"
        "assert registry.get_task('domainkit.storage.remove_unreferenced') is spec\n"
        "assert registry.spec_for(remove_unreferenced) is spec\n"
    )


def test_another_function_under_the_tasks_key_is_not_taken_for_it() -> None:
    """A function of the same name that is not the task stays refused."""
    _fresh(
        "async def remove_unreferenced(names: list[str]) -> None: ...\n"
        "remove_unreferenced.__module__ = 'domainkit.storage.cleanup'\n"
        "from domainkit.storage.cleanup import remove_unreferenced as real\n"
        "registry.spec_for(real)\n"
        "try:\n"
        "    registry.spec_for(remove_unreferenced)\n"
        "except ImproperlyConfigured:\n"
        "    pass\n"
        "else:\n"
        "    raise AssertionError('a stranger was taken for the task')\n"
    )
