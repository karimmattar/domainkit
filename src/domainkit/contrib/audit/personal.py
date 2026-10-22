"""Pseudonyms for the personal data in the audit trail, and forgetting people.

A personal value (a username, a client's address) is stored as
``pseudonym:<hex>``: an HMAC-SHA256 of the field's name and the value, keyed
with a random key of the person it is about, its subject. The same value of
the same subject always gives the same pseudonym, so the trail still
answers "did this happen to this person?" (``pseudonym_of``) without
holding the value.

``forget`` deletes subjects' keys. Their pseudonyms stay in the trail, which
is append-only, but can no longer be linked to anyone: the keys were random
and are gone. Callers without an actor are, for each day, one subject
(``anonymous:2026-10-18``), so their addresses can be forgotten once they
are old enough (``forget_anonymous``).

Keys are read where they are used, in the transaction that writes the
pseudonyms, and kept only until that transaction ends. ``forget`` drops
the keys open transactions of this process keep, and a key read before
``forget`` began is never kept after it: a lookup that started earlier
may still use the key it read for the rows it is writing, as a writer
that read the key just before ``forget`` would, but the next lookup reads
again. No key is kept from one transaction to the next, so a key
forgotten anywhere is never used by a transaction that starts afterwards.

A subject's first key is made under a lock of that subject, which
``forget`` takes too (a transaction advisory lock on PostgreSQL; SQLite
runs one writer at a time): ``forget`` waits for a writer making the key,
then deletes it, so no key outlives a ``forget`` that ran while it was
being made. Subjects with a key take no lock.
"""

import hashlib
import hmac
import json
import secrets
import threading
from collections.abc import Callable, Collection, Iterable
from datetime import date, timedelta
from typing import Final
from weakref import WeakKeyDictionary

from pydantic_core import to_jsonable_python
from sqlalchemy import ColumnElement, delete, select, text
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession
from sqlalchemy.orm import SessionTransaction

from domainkit import clock
from domainkit.apps import registry
from domainkit.db import connections
from domainkit.db.routing import router

from .settings import AuditSettings
from .tables import audit_keys

PREFIX: Final = "pseudonym:"
ANONYMOUS: Final = "anonymous:"
KEY_BYTES: Final = 32
DIGITS: Final = 32
"""Hex digits of the HMAC kept in a pseudonym (128 bits)."""

type Executor = AsyncSession | AsyncConnection

_held: WeakKeyDictionary[SessionTransaction, dict[str, bytes]] = WeakKeyDictionary()
"""The keys each open session transaction has read or made, by subject.

An entry goes with its transaction. Loops on several threads share it: it
is read and changed only with ``_held_lock`` held.
"""
_held_lock: Final = threading.Lock()
_forgets = 0
"""How many times keys were dropped; a lookup keeps what it read only if
none were dropped since it began. Read and changed with ``_held_lock``."""
_LOCKS: Final = text(
    "SELECT pg_advisory_xact_lock(key) FROM unnest(CAST(:keys AS BIGINT[])) "
    "AS locks(key)"
)
"""Take each subject's lock, in the order given, until the transaction ends."""


def anonymous_subject(day: date) -> str:
    """The subject of every caller without an actor on ``day`` (UTC)."""
    return f"{ANONYMOUS}{day.isoformat()}"


def pseudonym(key: bytes, field: str, value: object) -> str:
    """``value``, the ``field`` of a subject, as a pseudonym under their ``key``.

    The field's name is part of it, so equal values in different fields do
    not give away that they are equal. Lone surrogates in strings are
    encoded as they are, so every value has a pseudonym.
    """
    data = json.dumps(
        to_jsonable_python(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    message = f"{field}\0{data}".encode("utf-8", "surrogatepass")
    digest = hmac.new(key, message, hashlib.sha256).hexdigest()
    return f"{PREFIX}{digest[:DIGITS]}"


async def subject_keys(
    executor: Executor, subjects: Collection[str]
) -> dict[str, bytes]:
    """The keys of ``subjects``, made for those without one, in ``executor``.

    One query reads them all. Missing keys are inserted in subject order,
    ignoring conflicts, and the insert returns those it made, so writers
    racing for one subject neither deadlock nor fail: the first key
    committed is the one both use, and the writer that lost reads it. On
    PostgreSQL the new subjects' locks are taken first (``_lock``). In a
    session, the keys are kept until its transaction (or savepoint) ends,
    so the same transaction reads them once, unless ``forget`` ran
    meanwhile.
    """
    wanted = set(subjects)
    began = _forgotten()
    keys = _kept(executor, wanted)
    missing = wanted - keys.keys()
    if not missing:
        return keys
    keys |= await _read_keys(executor, missing)
    new = sorted(missing - keys.keys())
    if new:
        await _lock(executor, new)
        made = await _make_keys(executor, new)
        keys |= made | await _read_keys(executor, set(new) - made.keys())
    _keep(executor, keys, began)
    return keys


async def pseudonym_of(
    subject: str, field: str, value: object, *, database: str | None = None
) -> str | None:
    """The pseudonym ``value`` of ``field`` has in the trail for ``subject``.

    None when the subject was forgotten, or never audited. ``database`` is
    the alias to look in (default: ``AUDIT.DATABASE``).
    """
    alias = database or registry.settings(AuditSettings).DATABASE
    async with connections.engine(alias).connect() as connection:
        key = await connection.scalar(
            select(audit_keys.c.key).where(audit_keys.c.subject == subject)
        )
    return None if key is None else pseudonym(key, field, value)


async def forget(*subjects: str) -> int:
    """Delete the keys of ``subjects`` in every audited database.

    Their pseudonyms can no longer be linked to them. Returns how many keys
    were deleted. A writer making one of their first keys is waited for,
    and its key deleted.
    """
    if not subjects:
        return 0
    forgotten = set(subjects).__contains__
    _drop(forgotten)
    try:
        return await _delete(audit_keys.c.subject.in_(subjects), *subjects)
    finally:
        _drop(forgotten)  # Also what a transaction read meanwhile.


async def forget_anonymous(before: date) -> int:
    """Delete the keys of anonymous callers' days before ``before``."""
    last = anonymous_subject(before)

    def forgotten(subject: str) -> bool:
        return subject.startswith(ANONYMOUS) and subject < last

    _drop(forgotten)
    try:
        return await _delete(
            audit_keys.c.subject.startswith(ANONYMOUS) & (audit_keys.c.subject < last),
            last,
        )
    finally:
        _drop(forgotten)


async def prepare_anonymous_keys(database: str) -> None:
    """Make the keys of today's and tomorrow's anonymous callers, if missing.

    Each audited request without an actor needs its day's key (UTC). Made by
    the first such request of the day, inside its transaction, it would
    make every concurrent one wait for that transaction (on PostgreSQL), so
    the seal task makes them ahead, in a short transaction of its own.
    """
    today = clock.today()
    days = (today, today + timedelta(days=1))
    async with connections.engine(database).begin() as connection:
        await subject_keys(connection, [anonymous_subject(day) for day in days])


def audited_databases() -> tuple[str, ...]:
    """Every alias with an audit trail: ``AUDIT.DATABASE`` and each host's."""
    aliases = [registry.settings(AuditSettings).DATABASE]
    for target in router.hosting_targets():
        aliases.extend(router.aliases_of(target))
    return tuple(dict.fromkeys(aliases))


async def _read_keys(executor: Executor, subjects: Collection[str]) -> dict[str, bytes]:
    """The stored keys of ``subjects``."""
    if not subjects:
        return {}
    found = await executor.execute(
        select(audit_keys.c.subject, audit_keys.c.key).where(
            audit_keys.c.subject.in_(sorted(subjects))
        )
    )
    return {row.subject: row.key for row in found}


async def _make_keys(executor: Executor, subjects: list[str]) -> dict[str, bytes]:
    """Insert new keys for ``subjects``; return those inserted.

    A subject another writer made a key for meanwhile is skipped.
    """
    insert = postgresql.insert if _dialect(executor) == "postgresql" else sqlite.insert
    now = clock.now()
    rows = [
        {"subject": subject, "key": secrets.token_bytes(KEY_BYTES), "created_at": now}
        for subject in subjects
    ]
    made = await executor.execute(
        insert(audit_keys)
        .on_conflict_do_nothing(index_elements=["subject"])
        .returning(audit_keys.c.subject, audit_keys.c.key),
        rows,
    )
    return {row.subject: row.key for row in made}


def _transaction(executor: Executor) -> SessionTransaction | None:
    """The innermost transaction of a session; None for a connection, or none yet.

    Keys are kept for a savepoint apart from its transaction, so a key
    made inside a savepoint that rolls back goes with it.
    """
    if not isinstance(executor, AsyncSession):
        return None
    session = executor.sync_session
    return session.get_nested_transaction() or session.get_transaction()


def _kept(executor: Executor, subjects: Iterable[str]) -> dict[str, bytes]:
    """The keys of ``subjects`` the executor's transaction has kept."""
    transaction = _transaction(executor)
    if transaction is None:
        return {}
    with _held_lock:
        held = _held.get(transaction, {})
        return {subject: held[subject] for subject in subjects if subject in held}


def _keep(executor: Executor, keys: dict[str, bytes], began: int) -> None:
    """Keep ``keys`` until the executor's transaction ends.

    Not if keys were dropped since the lookup ``began``: one of them may
    have been read before its subject was forgotten.
    """
    transaction = _transaction(executor)
    if transaction is not None:
        with _held_lock:
            if _forgets == began:
                _held.setdefault(transaction, {}).update(keys)


def _forgotten() -> int:
    """How many times keys were dropped so far."""
    with _held_lock:
        return _forgets


def _drop(forgotten: Callable[[str], bool]) -> None:
    """Drop the kept keys of every subject ``forgotten`` says, in every transaction."""
    global _forgets
    with _held_lock:
        _forgets += 1
        for held in _held.values():
            for subject in [subject for subject in held if forgotten(subject)]:
                del held[subject]


async def _delete(condition: ColumnElement[bool], *subjects: str) -> int:
    """Delete the keys matching ``condition`` everywhere; count them.

    On PostgreSQL the locks of ``subjects`` are taken first, in each
    database's transaction, so a writer making one of their keys commits
    before the delete, which then sees it.
    """
    deleted = 0
    for alias in audited_databases():
        async with connections.engine(alias).begin() as connection:
            await _lock(connection, subjects)
            result = await connection.execute(delete(audit_keys).where(condition))
            deleted += result.rowcount
    return deleted


async def _lock(executor: Executor, subjects: Collection[str]) -> None:
    """Take the locks of ``subjects`` until the transaction ends, on PostgreSQL.

    Each lock is a stable 64-bit hash of the subject; every anonymous day
    shares one, which ``forget_anonymous`` takes. They are taken in one
    order, so two transactions never wait for each other. SQLite needs
    none: it runs one writer at a time.
    """
    if subjects and _dialect(executor) == "postgresql":
        keys = sorted({_lock_key(subject) for subject in subjects})
        await executor.execute(_LOCKS, {"keys": keys})


def _lock_key(subject: str) -> int:
    """The advisory lock of ``subject``: a signed 64-bit hash, the same everywhere."""
    name = ANONYMOUS if subject.startswith(ANONYMOUS) else subject
    data = f"domainkit.audit.subject\0{name}".encode("utf-8", "surrogatepass")
    digest = hashlib.blake2b(data, digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def _dialect(executor: Executor) -> str:
    """The name of the database ``executor`` talks to."""
    if isinstance(executor, AsyncConnection):
        return executor.dialect.name
    return executor.get_bind().dialect.name
