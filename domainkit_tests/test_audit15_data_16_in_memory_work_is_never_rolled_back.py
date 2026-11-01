"""DATA-16: in memory, work left without ``commit()`` stays done.

``UnitOfWork.__aexit__`` "rolls back anything not committed", and
``UnitOfWork.run`` and ``savepoint`` put in-memory repositories back as
they were (``_mark`` / ``_rewind``). A plain block that ends without
``commit()`` does not: ``rollback()`` calls ``_rollback()``, which the
fake ``startdomain`` generates leaves empty ("Nothing to undo: the
repositories are the state"), and the base class restores nothing. An
aggregate added, or changed, in a block that raised, or that forgot
``commit()``, is still there afterwards in memory, where the SQL unit
of work rolled it back: a use case missing its ``commit()`` passes every
test that reads the repository instead of counting commits.
"""

from typing import NewType
from uuid import UUID, uuid4

import pytest

from domainkit.ddd import AggregateRoot, EventBus, InMemoryRepository, UnitOfWork

pytestmark = pytest.mark.anyio
NoteId = NewType("NoteId", UUID)


class Note(AggregateRoot[NoteId]):
    """An aggregate with one field that changes."""

    text: str


class InMemoryNotes(InMemoryRepository[Note, NoteId]):
    """Notes in a dictionary."""


class FakeNotesUnitOfWork(UnitOfWork):
    """As ``startdomain`` writes ``tests/fakes.py``: commits counted, nothing undone."""

    def __init__(self, notes: InMemoryNotes) -> None:
        """Share ``notes`` across units of work, as one database would be."""
        super().__init__(events=EventBus())
        self.notes = notes
        self.commits = 0

    async def _commit(self) -> None:
        self.commits += 1

    async def _rollback(self) -> None:
        """Nothing to undo: the repositories are the state."""


class Refused(Exception):
    """The use case failed after it changed the aggregate."""


async def test_an_aggregate_added_in_a_failed_block_is_not_stored() -> None:
    """``add`` then an error: the repository holds nothing, as the table would."""
    notes = InMemoryNotes()

    async def create_then_fail() -> None:
        async with FakeNotesUnitOfWork(notes) as uow:
            await uow.notes.add(Note(id=NoteId(uuid4()), text="draft"))
            raise Refused

    with pytest.raises(Refused):
        await create_then_fail()

    assert notes.items == {}


async def test_a_change_left_without_commit_is_undone() -> None:
    """A change in a block that never commits is not seen by the next one."""
    note = Note(id=NoteId(uuid4()), text="first")
    notes = InMemoryNotes([note])

    async with FakeNotesUnitOfWork(notes) as uow:
        (await uow.notes.get(note.id)).text = "second"  # And no commit().

    async with FakeNotesUnitOfWork(notes) as uow:
        assert (await uow.notes.get(note.id)).text == "first"
