"""A record's history: the audit rows about one target or one aggregate.

Rows name what they are about in two ways. An explicit record has a
``target`` (``record("report.viewed", target="r-1")``). A committed event
holds its aggregate's id in a field of ``details``, such as ``product_id``,
so an aggregate's history is the rows whose ``details[field]`` is its id::

    entries = await history(target="r-1")
    entries = await history(aggregate=("product_id", str(product.id)))

Entries come newest first, from every audited database, a page at a time:
pass the last entry's ``position`` as ``before`` for the next page.

Each entry lists its ``changes``, where the details allow: the ``before``
and ``after`` mappings an explicit record holds, or else each field whose
value differs from the newest older entry of the history that has it, on
the same page or the one older entry fetched past it. Personal values stay
the pseudonyms the trail holds, also for people who were forgotten: nothing
here reads keys.
"""

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import (
    ColumnElement,
    String,
    and_,
    cast,
    inspect,
    literal_column,
    or_,
    select,
)
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from domainkit.apps import registry
from domainkit.db import connections

from .personal import audited_databases
from .settings import AuditSettings
from .tables import audit_log

__all__ = [
    "history",
]

MAX_LIMIT: Final = 200
FIELD: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
"""A field of ``details`` that names an aggregate."""
HISTORY_INDEX: Final = "ix_domainkit_audit_log_history_"
"""The names of the indexes of ``AUDIT.HISTORY_FIELDS`` start so (``tables``)."""
MAX_NAME: Final = 63
"""The longest name PostgreSQL keeps for an index."""

type Position = tuple[datetime, UUID]
"""Where an entry sits in a history: newest first, by time then id."""


@dataclass(frozen=True, slots=True)
class FieldChange:
    """One field an entry changed: its value before (None if unknown) and after."""

    field: str
    before: object
    after: object


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    """One audit row of a history, and what it changed."""

    id: UUID
    occurred_at: datetime
    actor: str
    action: str
    outcome: str
    target: str | None
    request_id: str | None
    event_id: UUID | None
    details: Mapping[str, Any]
    changes: tuple[FieldChange, ...]
    database: str

    @property
    def position(self) -> Position:
        """Pass it as ``before`` to read the entries older than this one."""
        return self.occurred_at, self.id


@dataclass(frozen=True, slots=True)
class HistoryPage:
    """A page of a history, and whether older entries follow."""

    entries: list[HistoryEntry]
    more: bool


async def history(
    *,
    target: str | None = None,
    aggregate: tuple[str, str] | None = None,
    limit: int = 50,
    before: Position | None = None,
    databases: Sequence[str] | None = None,
) -> list[HistoryEntry]:
    """The newest ``limit`` entries about ``target``, or about ``aggregate``.

    ``aggregate`` is ``(field, id)``: rows whose ``details[field]``, as
    text, is ``id``. Exactly one of the two is given. ``databases``
    defaults to every audited database.

    Raises:
        ValueError: Neither or both are given, the field is not a plain
            name, or ``limit`` is out of 1 to ``MAX_LIMIT``.
    """
    found = await history_page(
        target=target,
        aggregate=aggregate,
        limit=limit,
        before=before,
        databases=databases,
    )
    return found.entries


async def history_page(
    *,
    target: str | None = None,
    aggregate: tuple[str, str] | None = None,
    limit: int = 50,
    before: Position | None = None,
    databases: Sequence[str] | None = None,
) -> HistoryPage:
    """``history``, saying also whether older entries follow."""
    if (target is None) == (aggregate is None):
        raise ValueError("Name a target, or an aggregate's field and id.")
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"A history page holds 1 to {MAX_LIMIT} entries.")
    if aggregate is not None:
        aggregate_value(aggregate[0], "")  # A field name, or ValueError now.
    rows: list[tuple[str, RowMapping]] = []
    for alias in databases or audited_databases():
        engine = connections.engine(alias)
        condition = _about(target, aggregate, engine.dialect.name)
        if before is not None:
            at, entry = before
            condition &= or_(
                audit_log.c.occurred_at < at,
                and_(audit_log.c.occurred_at == at, audit_log.c.id < entry),
            )
        query = (
            select(audit_log)
            .where(condition)
            .order_by(audit_log.c.occurred_at.desc(), audit_log.c.id.desc())
            .limit(limit + 1)
        )
        async with engine.connect() as connection:
            found = (await connection.execute(query)).mappings().all()
        rows.extend((alias, row) for row in found)
    rows.sort(key=lambda item: (item[1]["occurred_at"], item[1]["id"]), reverse=True)
    rows = rows[: limit + 1]
    entries = [
        _entry(alias, row, rows[index + 1 :])
        for index, (alias, row) in enumerate(rows[:limit])
    ]
    return HistoryPage(entries, more=len(rows) > limit)


def _about(
    target: str | None, aggregate: tuple[str, str] | None, dialect: str
) -> ColumnElement[bool]:
    """The rows about ``target``, or about the aggregate ``(field, id)``."""
    if aggregate is None:
        return audit_log.c.target == target
    field, identifier = aggregate
    return aggregate_value(field, dialect) == identifier


def aggregate_value(field: str, dialect: str) -> ColumnElement[str]:
    """``details[field]`` as text, written as its history index is on PostgreSQL.

    There the field is written into the SQL, not bound, so a prepared
    statement's plan can use the index ``migrate`` keeps for it
    (``index_history``); only a ``FIELD`` is.

    Raises:
        ValueError: ``field`` is not a field name.
    """
    if not FIELD.fullmatch(field):
        raise ValueError(f"{field!r} is not a field name.")
    if dialect == "postgresql":
        return audit_log.c.details.op("->>", return_type=String)(
            literal_column(f"'{field}'")
        )
    return cast(audit_log.c.details[field].as_string(), String)


def _indexes(connection: Connection) -> set[str] | None:
    """The names of the trail's indexes; None where it has no table yet."""
    inspector = inspect(connection)
    if not inspector.has_table(audit_log.name):
        return None
    return {
        index["name"]
        for index in inspector.get_indexes(audit_log.name)
        if index["name"] is not None
    }


def history_index_name(field: str) -> str:
    """The name of the index of ``details[field]``; a digest for a long field."""
    name = HISTORY_INDEX + field
    if len(name) <= MAX_NAME:
        return name
    return HISTORY_INDEX + hashlib.sha256(field.encode()).hexdigest()[:16]


async def index_history(connection: AsyncConnection, database: str) -> None:
    """After ``migrate``: one index per ``AUDIT.HISTORY_FIELDS``, the rest dropped.

    Only on PostgreSQL, and where the trail is (``on_migrated``). Each is
    ``((details ->> 'field'), occurred_at, id)``, so a page of an
    aggregate's history reads its rows alone, newest first. Revisions leave
    them alone (``unmanaged_indexes``). Building one locks the trail
    against writes while it runs: on a large trail, make it beforehand
    with ``CREATE INDEX CONCURRENTLY`` under the same name (PERF-29).
    """
    if connection.dialect.name != "postgresql":
        return
    present = await connection.run_sync(_indexes)
    if present is None:
        return
    fields = registry.settings(AuditSettings).HISTORY_FIELDS
    wanted = {history_index_name(field): field for field in fields}
    for name in sorted(present):
        if name.startswith(HISTORY_INDEX) and name not in wanted:
            await connection.exec_driver_sql(f'DROP INDEX "{name}"')
    for name, field in sorted(wanted.items()):
        if name not in present:
            await connection.exec_driver_sql(
                f'CREATE INDEX "{name}" ON {audit_log.name} '
                f"((details ->> '{field}'), occurred_at, id)"
            )


def _entry(
    database: str, row: RowMapping, older: Sequence[tuple[str, RowMapping]]
) -> HistoryEntry:
    """The entry for ``row``, its changes found among the ``older`` rows."""
    details = dict(row["details"] or {})
    return HistoryEntry(
        id=row["id"],
        occurred_at=row["occurred_at"],
        actor=row["actor"],
        action=row["action"],
        outcome=row["outcome"],
        target=row["target"],
        request_id=row["request_id"],
        event_id=row["event_id"],
        details=details,
        changes=changes(details, [dict(other["details"] or {}) for _, other in older]),
        database=database,
    )


def changes(
    details: Mapping[str, Any], older: Sequence[Mapping[str, Any]]
) -> tuple[FieldChange, ...]:
    """What an entry with ``details`` changed, given older details, newest first.

    Explicit ``before`` and ``after`` mappings win. Otherwise each field
    compares with the newest older entry that has it; a field none has is
    new (before None).
    """
    before, after = details.get("before"), details.get("after")
    if isinstance(before, dict) and isinstance(after, dict):
        return tuple(
            FieldChange(name, before.get(name), after.get(name))
            for name in sorted(before.keys() | after.keys())
            if before.get(name) != after.get(name)
        )
    found = []
    for name, value in sorted(details.items()):
        previous = next((other[name] for other in older if name in other), None)
        if previous != value:
            found.append(FieldChange(name, previous, value))
    return tuple(found)
