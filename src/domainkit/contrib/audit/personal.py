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
"""

import hashlib
import hmac
import json
import secrets
from collections.abc import Collection
from datetime import UTC, date, datetime
from typing import Final

from pydantic_core import to_jsonable_python
from sqlalchemy import ColumnElement, delete, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

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

    Missing keys are inserted in subject order, ignoring conflicts, so
    writers racing for one subject neither deadlock nor fail: the first key
    committed is the one both use.
    """
    keys = await _read_keys(executor, subjects)
    missing = sorted(set(subjects) - keys.keys())
    if not missing:
        return keys
    insert = postgresql.insert if _dialect(executor) == "postgresql" else sqlite.insert
    now = datetime.now(UTC)
    new_keys = [
        {"subject": subject, "key": secrets.token_bytes(KEY_BYTES), "created_at": now}
        for subject in missing
    ]
    await executor.execute(
        insert(audit_keys).on_conflict_do_nothing(index_elements=["subject"]), new_keys
    )
    return keys | await _read_keys(executor, missing)


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
    were deleted.
    """
    if not subjects:
        return 0
    return await _delete(audit_keys.c.subject.in_(subjects))


async def forget_anonymous(before: date) -> int:
    """Delete the keys of anonymous callers' days before ``before``."""
    return await _delete(
        audit_keys.c.subject.startswith(ANONYMOUS)
        & (audit_keys.c.subject < anonymous_subject(before))
    )


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
    return dict(found.tuples().all())


async def _delete(condition: ColumnElement[bool]) -> int:
    """Delete the keys matching ``condition`` everywhere; count them."""
    deleted = 0
    for alias in audited_databases():
        async with connections.engine(alias).begin() as connection:
            result = await connection.execute(delete(audit_keys).where(condition))
            deleted += result.rowcount
    return deleted


def _dialect(executor: Executor) -> str:
    """The name of the database ``executor`` talks to."""
    if isinstance(executor, AsyncConnection):
        return executor.dialect.name
    return executor.get_bind().dialect.name
