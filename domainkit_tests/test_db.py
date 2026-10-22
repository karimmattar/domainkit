"""SQLAlchemy integration: connections, repositories, units of work, migrations."""

import asyncio
import io
import re
from collections.abc import Iterator
from pathlib import Path
from typing import NewType
from uuid import UUID, uuid4

import pytest
from alembic.util.exc import CommandError as AlembicCommandError
from sqlalchemy import (
    Column,
    Integer,
    Interval,
    MetaData,
    String,
    Table,
    Uuid,
    inspect,
)
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import TypeDecorator

from domainkit.conf import override_settings
from domainkit.db import (
    SqlAlchemyRepository,
    SqlAlchemyUnitOfWork,
    connections,
    create_engine,
    map_imperatively,
)
from domainkit.db import migrations as migrations_module
from domainkit.db.connections import ConnectionHandler
from domainkit.db.migrations import (
    downgrade,
    make_migration,
    next_revision_id,
    pending_changes,
    render_item,
    upgrade,
)
from domainkit.ddd import AggregateRoot, ConcurrentUpdate, DomainEvent, EventBus
from domainkit.exceptions import ImproperlyConfigured
from domainkit.management import CommandError, call_command

pytestmark = pytest.mark.anyio

NoteId = NewType("NoteId", UUID)
test_metadata = MetaData()
notes = Table(
    "framework_test_notes",
    test_metadata,
    Column("id", Uuid, primary_key=True),
    Column("text", String(50), nullable=False),
    Column("version", Integer, nullable=False),
)


ghosts = Table(
    "framework_test_ghosts",
    test_metadata,
    Column("id", Integer, primary_key=True),
    info={"domain": "ghost"},
)
"""Belongs to a domain that is never installed: migrations must ignore it."""


class NoteWritten(DomainEvent):
    """A note was stored."""

    note_id: UUID


class Note(AggregateRoot[NoteId]):
    """Aggregate mapped to a table outside the project metadata."""

    text: str


map_imperatively(Note, notes)


class NoteRepository(SqlAlchemyRepository[Note, NoteId]):
    """Repository under test."""

    model = Note


class NotesUnitOfWork(SqlAlchemyUnitOfWork):
    """Unit of work under test."""

    notes: NoteRepository

    def create_repositories(self, session: AsyncSession) -> None:
        """Bind the repository."""
        self.notes = NoteRepository(session)


@pytest.fixture
def database(database_url: str) -> Iterator[str]:
    """An empty database, selected in the settings."""
    with override_settings(DATABASES={"default": {"URL": database_url}}):
        yield database_url


@pytest.fixture
async def notes_table(database: str) -> None:
    """Create the test table in the running loop's engine."""
    async with connections.engine().begin() as connection:
        await connection.run_sync(test_metadata.create_all)


def test_create_engine_applies_pool_options(tmp_path: Path) -> None:
    """Pool settings are passed through only when set."""
    with override_settings(
        DATABASES={
            "default": {
                "URL": f"sqlite+aiosqlite:///{tmp_path / 'pool.sqlite3'}",
                "POOL_SIZE": 3,
                "MAX_OVERFLOW": 1,
                "POOL_TIMEOUT": 5,
                "POOL_RECYCLE": 60,
                "ECHO": True,
            }
        }
    ):
        engine = create_engine(connections.config())

    assert engine.pool.size() == 3  # type: ignore[attr-defined]
    assert engine.echo is True


async def test_connections_are_per_event_loop(database: str) -> None:
    """One engine per loop and alias; ``close_all`` disposes this loop's engines."""
    engine = connections.engine()
    assert connections.engine() is engine
    await connections.check()
    assert await connections.check_all(1.0) == []

    async def other_loop_engine() -> object:
        return connections.engine()

    other = await asyncio.to_thread(asyncio.run, other_loop_engine())
    assert other is not engine

    await connections.close_all()
    assert connections.engine() is not engine
    await connections.close_all()


async def test_each_database_is_checked_on_its_own(tmp_path: Path) -> None:
    """A database that cannot be reached fails its own check, not the others'."""
    missing = tmp_path / "missing" / "other.sqlite3"
    with override_settings(
        DATABASES={
            "default": {"URL": f"sqlite+aiosqlite:///{tmp_path / 'ok.sqlite3'}"},
            "other": {"URL": f"sqlite+aiosqlite:///{missing}"},
        }
    ):
        try:
            assert await connections.check_all(1.0) == ["database:other"]
        finally:
            await connections.close_all()


async def test_a_handler_of_its_own_shares_no_engines(database: str) -> None:
    """Each ``ConnectionHandler`` keeps its own engines, and closes only those."""
    handler = ConnectionHandler()
    engine = handler.engine()
    assert handler.engine() is engine
    assert connections.engine() is not engine

    await handler.close_all()

    assert handler.engine() is not engine
    await handler.close_all()
    await connections.close_all()


async def test_unknown_alias(database: str) -> None:
    """Asking for an alias that is not configured names it."""
    with pytest.raises(ImproperlyConfigured, match="'reports'"):
        connections.engine("reports")


async def test_unit_of_work_commits_and_publishes(notes_table: None) -> None:
    """Committed aggregates are stored and their events published."""
    bus = EventBus()
    published: list[DomainEvent] = []

    async def collect(event: DomainEvent) -> None:
        published.append(event)

    bus.subscribe(DomainEvent, collect)
    note = Note(id=NoteId(uuid4()), text="first")
    note.record(NoteWritten(note_id=note.id))

    async with NotesUnitOfWork(events=bus) as uow:
        await uow.notes.add(note)
        await uow.commit()

    async with NotesUnitOfWork() as uow:
        loaded = await uow.notes.get(note.id)
    assert loaded.text == "first"
    assert [type(event) for event in published] == [NoteWritten]
    await connections.close_all()


async def test_unit_of_work_rolls_back_without_commit(notes_table: None) -> None:
    """Leaving without ``commit`` discards changes; explicit rollback works too."""
    note = Note(id=NoteId(uuid4()), text="draft")

    async with NotesUnitOfWork() as uow:
        await uow.notes.add(note)
    async with NotesUnitOfWork() as uow:
        await uow.notes.add(note)
        await uow.rollback()
    async with NotesUnitOfWork() as uow:
        assert await uow.notes.find(note.id) is None

    async with NotesUnitOfWork() as uow:
        await uow.notes.add(note)
        await uow.commit()
    async with NotesUnitOfWork() as uow:
        await uow.notes.remove(await uow.notes.get(note.id))
        await uow.commit()
    async with NotesUnitOfWork() as uow:
        assert await uow.notes.find(note.id) is None
    await connections.close_all()


def test_map_imperatively_is_idempotent() -> None:
    """Mapping an already mapped class is ignored."""

    class Tag(AggregateRoot[int]):
        label: str

    table = Table(
        "framework_test_tags",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("label", String(10)),
    )

    map_imperatively(Tag, table)
    map_imperatively(Tag, table)

    mapper = inspect(Tag)
    assert mapper is not None
    assert mapper.local_table is table


class Code(TypeDecorator[str]):
    """Decorated generic type."""

    impl = String(8)
    cache_ok = True


class Network(TypeDecorator[str]):
    """Decorated dialect-specific type."""

    impl = INET

    cache_ok = True


def test_render_item_uses_storage_types() -> None:
    """Decorated generic types render as SQLAlchemy types, others are left alone."""
    context = None
    assert render_item("type", Code(), context) == "sa.String(length=8)"  # type: ignore[arg-type]
    assert render_item("type", Network(), context) is False  # type: ignore[arg-type]
    assert render_item("type", String(), context) is False  # type: ignore[arg-type]
    assert render_item("type", Interval(), context) is False  # type: ignore[arg-type]
    assert render_item("column", Code(), context) is False  # type: ignore[arg-type]


@pytest.fixture
def migration_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, database: str
) -> Iterator[Path]:
    """Migrations of the test table into a temporary directory and database."""
    monkeypatch.setattr(migrations_module, "metadata", test_metadata)
    versions = tmp_path / "migrations"
    with override_settings(MIGRATIONS_DIR=versions):
        yield versions


def test_migration_workflow(migration_project: Path) -> None:
    """Detect, write, apply, and revert a revision; skip empty ones."""
    assert pending_changes() is not None

    path = make_migration("create notes")
    assert path is not None
    assert re.fullmatch(r"0001_[0-9a-f]{4}_create_notes\.py", path.name)
    assert '"""Create notes.' in path.read_text(encoding="utf-8")

    upgrade()
    assert pending_changes() is None
    assert make_migration("nothing new") is None

    config = migrations_module.alembic_config()
    assert next_revision_id(config).startswith("0002_")
    downgrade("base")
    with pytest.raises(AlembicCommandError, match="not up to date"):
        pending_changes()


@pytest.mark.parametrize(
    "message",
    [
        'x"""\nimport os\n"""',
        "two\nlines",
        "C:\\new folder",
        'ends with triple quotes"""',
        "tab\there",
    ],
)
def test_migration_messages_stay_inside_the_docstring(
    migration_project: Path, message: str
) -> None:
    """A message that could end the docstring or escape a character is refused."""
    refusal = (
        "A migration message is one line of text, without backslashes or "
        'triple quotes (""").'
    )
    with pytest.raises(ValueError, match=f"^{re.escape(refusal)}$"):
        make_migration(message)
    with pytest.raises(CommandError) as failed:
        call_command("makemigrations", message=message, stdout=io.StringIO())

    assert str(failed.value) == refusal
    assert not migration_project.exists()


def test_migrate_prefers_migration_url(migration_project: Path, database: str) -> None:
    """``MIGRATION_URL`` (the schema owner) is used instead of ``URL``."""
    make_migration("create notes.")
    with override_settings(
        DATABASES={
            "default": {
                "URL": "sqlite+aiosqlite:////nonexistent/x",
                "MIGRATION_URL": database,
            }
        }
    ):
        upgrade()
    assert pending_changes() is None


async def test_concurrent_changes_raise_instead_of_overwriting(
    notes_table: None,
) -> None:
    """Two units of work editing one aggregate: the second commit fails."""
    note = Note(id=NoteId(uuid4()), text="v1")
    async with NotesUnitOfWork() as uow:
        await uow.notes.add(note)
        await uow.commit()
    assert note.version == 1

    async with NotesUnitOfWork() as first, NotesUnitOfWork() as second:
        mine, theirs = await first.notes.get(note.id), await second.notes.get(note.id)
        mine.text, theirs.text = "mine", "theirs"
        await first.commit()
        with pytest.raises(ConcurrentUpdate) as caught:
            await second.commit()

    assert caught.value.code == "concurrent_update"
    assert caught.value.message == (
        "The data changed while this request was running; reload it and try again."
    )
    async with NotesUnitOfWork() as uow:
        stored = await uow.notes.get(note.id)
    assert (stored.text, stored.version) == ("mine", 2)
    await connections.close_all()


async def test_sql_parameters_stay_out_of_errors(tmp_path: Path) -> None:
    """Bound values are hidden from error text unless ``LOG_PARAMETERS``."""
    from sqlalchemy.exc import IntegrityError

    from domainkit.conf import DatabaseConfig

    url = f"sqlite+aiosqlite:///{tmp_path / 'params.sqlite3'}"
    messages = {}
    for log_parameters in (False, True):
        engine = create_engine(DatabaseConfig(URL=url, LOG_PARAMETERS=log_parameters))
        async with engine.begin() as connection:
            await connection.run_sync(test_metadata.create_all)
            row = {"id": uuid4(), "text": "s3cret-token", "version": 1}
            await connection.execute(notes.insert().values(**row))
            with pytest.raises(IntegrityError) as caught:
                await connection.execute(notes.insert().values(**row))
            messages[log_parameters] = str(caught.value)
            await connection.run_sync(test_metadata.drop_all)
        await engine.dispose()

    assert "s3cret-token" not in messages[False]
    assert "s3cret-token" in messages[True]


def test_tables_of_uninstalled_domains_are_ignored(migration_project: Path) -> None:
    """Leftover tables of uninstalled apps are kept; unknown ones are reported."""
    path = make_migration("create notes")
    assert path is not None
    assert "framework_test_ghosts" not in path.read_text(encoding="utf-8")
    upgrade()

    async def create(*tables: Table) -> None:
        async with connections.engine().begin() as connection:
            for table in tables:
                await connection.run_sync(table.create)
        await connections.close_all()

    asyncio.run(create(ghosts))
    assert pending_changes() is None

    stray = Table(
        "framework_test_stray", MetaData(), Column("id", Integer, primary_key=True)
    )
    asyncio.run(create(stray))
    assert "framework_test_stray" in str(pending_changes())
