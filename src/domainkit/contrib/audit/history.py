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

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import ColumnElement, String, and_, cast, or_, select
from sqlalchemy.engine import RowMapping

from domainkit.db import connections

from .personal import audited_databases
from .tables import audit_log

__all__ = [
    "history",
]

MAX_LIMIT: Final = 200
FIELD: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
"""A field of ``details`` that names an aggregate."""

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
    condition = _about(target, aggregate)
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
    rows: list[tuple[str, RowMapping]] = []
    for alias in databases or audited_databases():
        async with connections.engine(alias).connect() as connection:
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
    target: str | None, aggregate: tuple[str, str] | None
) -> ColumnElement[bool]:
    """The rows about ``target``, or about the aggregate ``(field, id)``."""
    if aggregate is None:
        return audit_log.c.target == target
    field, identifier = aggregate
    if not FIELD.fullmatch(field):
        raise ValueError(f"{field!r} is not a field name.")
    value = cast(audit_log.c.details[field].as_string(), String)
    return value == identifier


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
