"""The keyed request running now, and whether its changes committed.

``IdempotencyMiddleware`` runs each keyed request as an operation. When a SQL
unit of work commits during it, the key is marked committed: in the same
transaction when the unit of work uses ``IDEMPOTENCY.DATABASE``, right after
the commit otherwise. A committed key is never released, so a retry cannot
run the changes twice.

Each operation holds its key under a lease, with a random token. A retry may
take over a key whose lease ran out before anything committed; the key then
carries the retry's token. Marks and results are written only under the
holder's token, so a request that lost its key cannot commit: its unit of
work fails with ``KeyTakenOver``. Before a unit of work on another database
commits, the key is pinned (held until it expires, and marked ``pinned``),
and the operation notes it: from then on the key is never taken over or
released, since those changes may have committed even if their commit
raised (a lost reply, a cancellation while it ran).
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from sqlalchemy import ColumnElement, Update, and_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from domainkit import clock
from domainkit.db import connections
from domainkit.db.query import contains_text
from domainkit.ddd import ConcurrentUpdate
from domainkit.ddd.personal import SEPARATOR, tagged_with

from .statements import ClockStatements, holder
from .tables import idempotency_keys


class KeyTakenOver(ConcurrentUpdate):
    """A retry took the key over after this request's lease ran out."""

    code = "idempotency_key_taken_over"


@dataclass(slots=True)
class Operation:
    """A request running under an ``Idempotency-Key``."""

    key_hash: str
    token: str
    """Proves this request still holds the key."""
    committed: bool = False
    """Whether a SQL unit of work committed during the request."""
    pinned: bool = False
    """Whether the key was pinned for a commit on another database.

    Set before that commit, so it holds whatever the commit reports.
    """
    actor: str | None = None
    """Who sent it; whom its response is about, if anyone."""
    subjects: set[str] = field(default_factory=set)
    """Whom else its response may be about: the subjects of the ``Personal``
    fields of the events it committed. Kept with the response, with the
    actor."""

    def holds_key(self) -> ColumnElement[bool]:
        """Match the key's row while this request holds it."""
        return and_(
            idempotency_keys.c.key_hash == self.key_hash,
            idempotency_keys.c.token == self.token,
        )


_running: Final[ContextVar[Operation | None]] = ContextVar(
    "idempotency_operation", default=None
)


@contextmanager
def run_operation(operation: Operation) -> Iterator[Operation]:
    """Run the code inside as ``operation``."""
    reset = _running.set(operation)
    try:
        yield operation
    finally:
        _running.reset(reset)


def running_operation() -> Operation | None:
    """Return the keyed request running in this context, if any."""
    return _running.get()


def pin_lease(operation: Operation) -> Update:
    """Hold the key until it expires, never released, if the operation holds it."""
    return (
        update(idempotency_keys)
        .where(operation.holds_key())
        .values(locked_until=idempotency_keys.c.expires_at, pinned=True)
    )


def committed_values(operation: Operation) -> dict[str, Any]:
    """The values of ``statements.MARK_COMMITTED`` for ``operation``, as of now.

    ``mark_committed`` builds the same statement with them; the framework
    runs the one built once.
    """
    return {"committed_at": clock.now(), **holder(operation.key_hash, operation.token)}


def mark_committed(operation: Operation) -> Update:
    """Record that the operation committed changes, if it still holds its key."""
    return (
        update(idempotency_keys)
        .where(operation.holds_key())
        .values(committed_at=clock.now())
    )


async def purge(
    connection: AsyncConnection,
    built: ClockStatements,
    now: datetime | None,
    tombstone_seconds: float,
    batch: int | None,
) -> int:
    """Delete expired keys; keep those that may have committed as tombstones.

    Up to ``batch`` rows each (every one: None). A key whose request
    committed or pinned it loses its response, headers, and ``subjects``,
    and is kept ``tombstone_seconds`` more, so its retries still get 409;
    the tombstone is deleted once that time has passed too (FIN-84). Returns
    the most rows one statement touched: a whole batch means more may wait.
    """
    tombstones = tombstone_seconds > 0
    swept = await connection.execute(
        built.sweep(batch, tombstones), built.sweep_values(now)
    )
    if not tombstones:
        return int(swept.rowcount)
    buried = await connection.execute(
        built.bury(batch), built.bury_values(now, tombstone_seconds)
    )
    return max(int(swept.rowcount), int(buried.rowcount))


async def erase_subject(database: str, subject: str) -> int:
    """Drop the kept responses about ``subject``; return how many.

    The keys stay, with their status: a retry gets 409
    ``idempotency_response_unavailable`` rather than running again.
    Tombstones hold no response and name no one (only the hash of caller
    and key every key row has), so there is nothing of theirs to find.
    """
    async with connections.engine(database).begin() as connection:
        named = contains_text(
            idempotency_keys.c.subjects, tagged_with(subject), connection.dialect.name
        )
        dropped = await connection.execute(
            update(idempotency_keys)
            .where(named)
            .values(body=None, headers=None, subjects=None)
        )
    return int(dropped.rowcount)


async def export_subject(database: str, subject: str) -> list[dict[str, Any]]:
    """The kept responses about ``subject``, oldest first.

    A response is what its caller saw, which may hold others' personal
    data: its body is given only when ``subject`` was the caller (the
    actor, first in the tag), and withheld otherwise (``"body": None``,
    ``"withheld": True``; SEC-10b-4).
    """
    actor = tagged_with(subject) + SEPARATOR
    async with connections.engine(database).connect() as connection:
        named = contains_text(
            idempotency_keys.c.subjects, tagged_with(subject), connection.dialect.name
        )
        rows = (
            await connection.execute(
                select(
                    idempotency_keys.c.created_at,
                    idempotency_keys.c.status_code,
                    idempotency_keys.c.body,
                    idempotency_keys.c.subjects,
                )
                .where(named, idempotency_keys.c.body.is_not(None))
                .order_by(idempotency_keys.c.created_at)
            )
        ).all()
    return [
        {
            "created_at": row.created_at.isoformat(),
            "status_code": row.status_code,
            **(
                {"body": bytes(row.body).decode("utf-8", "replace"), "withheld": False}
                if row.subjects.startswith(actor)
                else {"body": None, "withheld": True}
            ),
        }
        for row in rows
    ]
