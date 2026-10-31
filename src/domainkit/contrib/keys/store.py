"""Subjects' keys in ``domainkit_keys_subject_keys``, on the writer's connection."""

from typing import Final

from sqlalchemy import ColumnElement, Connection, select
from sqlalchemy.dialects import postgresql, sqlite

from domainkit import clock
from domainkit.exceptions import DomainkitError
from domainkit.keys.ring import StoredSubjectKey

from .tables import subject_keys

ATTEMPTS: Final = 3
"""How many times ``add`` tries before giving up on a subject being erased."""


class KeyStoreConflict(DomainkitError):
    """A subject's key could not be stored: their keys were erased meanwhile."""


class TableSubjectKeyStore:
    """Reads and adds subjects' keys in the connection's own transaction.

    A key is read where the value it seals is written or read, so a key
    made for a row is committed with it, and a row rolled back takes its
    key along. Two writers making a subject's first key at once both keep
    the one stored first.
    """

    def find(self, connection: Connection, key_id: str) -> StoredSubjectKey | None:
        """The key ``key_id``; None once destroyed."""
        return _stored(connection, subject_keys.c.id == key_id)

    def of_subject(
        self, connection: Connection, subject: str
    ) -> StoredSubjectKey | None:
        """The key of ``subject``, if it has one."""
        return _stored(connection, subject_keys.c.subject == subject)

    def add(self, connection: Connection, key: StoredSubjectKey) -> StoredSubjectKey:
        """Store ``key``, unless ``key.subject`` has one; the one stored.

        Raises:
            KeyStoreConflict: Another writer's key for the subject was
                deleted each time between its insert and this read.
        """
        insert = (
            postgresql.insert
            if connection.dialect.name == "postgresql"
            else sqlite.insert
        )
        statement = (
            insert(subject_keys)
            .values(
                id=key.id,
                subject=key.subject,
                wrapping_key=key.wrapping_key,
                key=key.wrapped,
                created_at=clock.now(),
            )
            .on_conflict_do_nothing(index_elements=["subject"])
            .returning(subject_keys.c.id)
        )
        for _ in range(ATTEMPTS):
            if connection.execute(statement).first() is not None:
                return key
            # Another writer's key won; an erasure may delete it before this read.
            if (stored := self.of_subject(connection, key.subject)) is not None:
                return stored
        raise KeyStoreConflict(
            f"The key of a subject could not be stored in {ATTEMPTS} attempts: "
            "each time, another writer's key for them was being erased."
        )


def _stored(
    connection: Connection, condition: ColumnElement[bool]
) -> StoredSubjectKey | None:
    """The one key ``condition`` selects."""
    row = connection.execute(
        select(
            subject_keys.c.id,
            subject_keys.c.subject,
            subject_keys.c.wrapping_key,
            subject_keys.c.key,
        ).where(condition)
    ).first()
    if row is None:
        return None
    return StoredSubjectKey(row.id, row.subject, row.wrapping_key, bytes(row.key))
