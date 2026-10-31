"""Subjects' keys in ``domainkit_keys_subject_keys``, on the writer's connection.

A subject's first key is made inside the writer's transaction. An erasure
that runs meanwhile, in another transaction, cannot see that key (nor wait
for it, on PostgreSQL), so it leaves a tombstone instead
(``domainkit_keys_erasures``), under the subject's lock. A transaction
that made keys checks, as it commits and under the same locks, whether a
subject it made a key for was erased after the key was made; if so it
destroys the key, and the blind indexes it wrote for that subject, before
committing (SEC-15a-2). A lock held from the key's making to the commit
would make an erasure wait for every writer, and deadlock a request that
erases a person it is writing for.
"""

import hashlib
import threading
from collections.abc import Collection
from datetime import timedelta
from typing import Any, Final, cast
from weakref import WeakKeyDictionary

from sqlalchemy import (
    ColumnElement,
    Connection,
    Engine,
    delete,
    event,
    exists,
    func,
    select,
    text,
    update,
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import Transaction

from domainkit import clock
from domainkit.db.encrypted import EncryptedType, encrypted_columns, indexed_tables
from domainkit.exceptions import DomainkitError
from domainkit.keys.ring import StoredSubjectKey

from .tables import erasures, subject_keys

ATTEMPTS: Final = 3
"""How many times ``add`` tries before giving up on a subject being erased."""
ERASURE_MEMORY: Final = timedelta(days=1)
"""How long a tombstone is kept: longer than any transaction that writes keys."""
_LOCKS: Final = text(
    "SELECT pg_advisory_xact_lock(key) FROM unnest(CAST(:keys AS BIGINT[])) "
    "AS locks(key)"
)
"""Take each subject's lock, in the order given, until the transaction ends."""
_made: WeakKeyDictionary[Transaction, dict[str, str]] = WeakKeyDictionary()
"""The subjects' keys each open transaction made, by subject: their ids."""
_made_lock: Final = threading.Lock()


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
                created_at=_now(connection),
            )
            .on_conflict_do_nothing(index_elements=["subject"])
            .returning(subject_keys.c.id)
        )
        for _ in range(ATTEMPTS):
            if connection.execute(statement).first() is not None:
                _remember(connection, key)
                return key
            # Another writer's key won; an erasure may delete it before this read.
            if (stored := self.of_subject(connection, key.subject)) is not None:
                return stored
        raise KeyStoreConflict(
            f"The key of a subject could not be stored in {ATTEMPTS} attempts: "
            "each time, another writer's key for them was being erased."
        )


def _now(connection: Connection) -> Any:  # noqa: ANN401 - a time, or SQL for one.
    """The time now, as the database tells it on PostgreSQL (``clock_timestamp()``).

    Keys and tombstones are compared by one clock, the database's; SQLite
    serializes writers, so the process's clock serves there.
    """
    if connection.dialect.name == "postgresql":
        return func.clock_timestamp()
    return clock.now()


def subject_lock(connection: Connection, subjects: Collection[str]) -> None:
    """Take the locks of ``subjects`` until the transaction ends, on PostgreSQL.

    Each is a stable 64-bit hash of the subject, taken in one order, so
    two transactions never wait for each other. SQLite runs one writer at
    a time and needs none.
    """
    if connection.dialect.name == "postgresql":
        keys = sorted({_lock_key(subject) for subject in subjects})
        connection.execute(_LOCKS, {"keys": keys})


def _lock_key(subject: str) -> int:
    """The advisory lock of ``subject``: a signed 64-bit hash, the same everywhere."""
    data = f"domainkit.keys.subject\0{subject}".encode("utf-8", "surrogatepass")
    digest = hashlib.blake2b(data, digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def _remember(connection: Connection, key: StoredSubjectKey) -> None:
    """Remember that the connection's transaction made ``key``."""
    transaction = cast("Transaction", connection.get_transaction())  # Writing.
    with _made_lock:
        _made.setdefault(transaction, {})[key.subject] = key.id


def erase(connection: Connection, subject: str) -> int:
    """Destroy ``subject``'s keys in the connection's database; how many.

    Under the subject's lock: leave a tombstone first, for a transaction
    making their first key meanwhile, and forget tombstones a day old.
    """
    subject_lock(connection, [subject])
    insert = (
        postgresql.insert if connection.dialect.name == "postgresql" else sqlite.insert
    )
    now = _now(connection)
    tombstone = insert(erasures).values(subject=subject, erased_at=now)
    connection.execute(
        tombstone.on_conflict_do_update(
            index_elements=["subject"], set_={"erased_at": now}
        )
    )
    connection.execute(
        delete(erasures).where(erasures.c.erased_at < clock.now() - ERASURE_MEMORY)
    )
    result = connection.execute(
        delete(subject_keys).where(subject_keys.c.subject == subject)
    )
    return int(result.rowcount)


@event.listens_for(Engine, "commit")
def _committing(connection: Connection) -> None:
    """Destroy the keys this transaction made for subjects erased since.

    Under their locks, so an erasure either finished before (and left its
    tombstone) or starts after this commit (and sees the keys).
    """
    transaction = cast("Transaction", connection.get_transaction())  # Committing.
    with _made_lock:
        made = _made.pop(transaction, {})
    if not made:
        return
    subject_lock(connection, made)
    for subject, key_id in made.items():
        erased = exists().where(
            erasures.c.subject == subject,
            erasures.c.erased_at >= subject_keys.c.created_at,
        )
        gone = connection.execute(
            delete(subject_keys).where(subject_keys.c.id == key_id, erased)
        )
        if gone.rowcount:
            clear_indexes(connection, indexed_tables(connection), subject)


def clear_indexes(
    connection: Connection, tables: Collection[Any], subject: str
) -> None:
    """Clear the blind indexes of ``subject``'s sealed values in ``tables``."""
    from .operations import subject_value

    for table in tables:
        for column in encrypted_columns(table):
            type_ = cast("EncryptedType", column.type)
            if type_.subject is None or type_.blind_index is None:
                continue
            owner = table.c[type_.subject]
            value = subject_value(owner, subject)
            if value is not None:
                connection.execute(
                    update(table)
                    .where(owner == value)
                    .values({type_.blind_index: None})
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
