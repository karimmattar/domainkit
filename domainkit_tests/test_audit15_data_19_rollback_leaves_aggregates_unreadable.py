"""DATA-19: after ``uow.rollback()``, an aggregate it had loaded cannot be read.

``UnitOfWork.rollback`` is public; ``SqlAlchemyUnitOfWork._rollback`` calls
``session.rollback()``, which expires every loaded instance (whatever
``expire_on_commit`` says). Reading an attribute afterwards is a lazy load,
I/O that async code cannot do there: ``MissingGreenlet``, a 500. The
savepoint's rollback reloads what it expired (``reload_expired``) and
``__aexit__`` closes the session, which detaches without expiring, so only
an explicit ``rollback()`` leaves the use case holding aggregates it can
no longer read, where the in-memory unit of work leaves them changed
(DATA-16).
"""

from collections.abc import AsyncIterator
from typing import Annotated, NewType
from uuid import UUID, uuid4

import pytest
from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import AsyncSession

from domainkit.conf import override_settings
from domainkit.db import (
    SqlAlchemyRepository,
    SqlAlchemyUnitOfWork,
    connections,
    map_imperatively,
    table_for,
)
from domainkit.ddd import AggregateRoot, MaxLength

pytestmark = pytest.mark.anyio
NoteId = NewType("NoteId", UUID)


class Note(AggregateRoot[NoteId]):
    """An aggregate with one field that changes."""

    text: Annotated[str, MaxLength(20)]


schema = MetaData()
notes = table_for(Note, "framework_test_audit_rollback_notes", metadata=schema)
map_imperatively(Note, notes)


class Notes(SqlAlchemyRepository[Note, NoteId]):
    """Notes in SQL."""


class NotesUnitOfWork(SqlAlchemyUnitOfWork):
    """One transaction over the notes."""

    notes: Notes

    def create_repositories(self, session: AsyncSession) -> None:
        """Bind the repository."""
        self.notes = Notes(session)


@pytest.fixture
async def tables(database_url: str) -> AsyncIterator[None]:
    """The notes table, in a database of its own."""
    with override_settings(DATABASES={"default": {"URL": database_url}}):
        async with connections.engine().begin() as connection:
            await connection.run_sync(schema.create_all)
        yield
        await connections.close_all()


async def test_an_aggregate_reads_as_stored_after_a_rollback(tables: None) -> None:
    """The change is undone, and the aggregate still reads: as it is stored."""
    note = Note(id=NoteId(uuid4()), text="first")
    async with NotesUnitOfWork() as uow:
        await uow.notes.add(note)
        await uow.commit()

    async with NotesUnitOfWork() as uow:
        loaded = await uow.notes.get(note.id)
        loaded.text = "second"
        await uow.rollback()

        assert loaded.text == "first"
