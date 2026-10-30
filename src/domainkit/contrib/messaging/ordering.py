"""Ordering keys: a key's messages run one at a time, in the order of their commits.

A message may carry an ordering key: given when it is queued
(``uow.defer(task, ordering_key=...)``, ``enqueue``), or declared by the
task or durable handler that runs it (``@ordered``, in ``registry``). Every
worker and replica sharing an outbox then runs the messages of one key one
at a time, in the order their commits happened; messages of other keys, and
messages without one, run beside them as before.

The outbox keeps only the first unfinished message of a key ``pending``;
the ones committed after it are ``held``. Claims and relays take only
pending messages, through the index they always used, so holding a key
back costs the claim nothing. When a message of a key finishes (done,
dead, or cancelled), the transaction that records it makes the key's next
message ``pending``. A message that fails and is retried stays pending, and
so holds its key back until it succeeds or is dead-lettered; a worker that
dies mid-message holds it until its lease ends, and the message runs again
first.

Each transaction that changes a key's messages first writes the key's row
in ``domainkit_ordering_keys``, and holds it until it commits. Writers of
one key thus take turns: each message's ``sequence`` comes from that row,
in commit order, and whether it is first of its key is decided with every
earlier commit of the key in sight. Messages of different keys never wait
for each other's rows.
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import (
    ColumnElement,
    Insert,
    ScalarSelect,
    Select,
    case,
    delete,
    literal,
    not_,
    or_,
    select,
    update,
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession
from sqlalchemy.sql.dml import Delete

from domainkit.exceptions import ImproperlyConfigured

from . import dbclock
from .tables import HELD, KEYED, PENDING, WAITING, messages, ordering_keys

type Executor = AsyncSession | AsyncConnection


def _turn(dialect: str, key: str, count: int) -> Insert:
    """Write ``key``'s row, adding ``count`` to its sequence; return the sequence.

    The row stays locked until the transaction ends: the key's turn.
    """
    now = dbclock.now(dialect)
    if dialect == "postgresql":
        statement: Any = postgresql.insert(ordering_keys)
    elif dialect == "sqlite":
        statement = sqlite.insert(ordering_keys)
    else:
        raise ImproperlyConfigured(
            f"Ordering keys need PostgreSQL or SQLite, not {dialect}."
        )
    turn: Insert = (
        statement.values(key=key, sequence=count, touched_at=now)
        .on_conflict_do_update(
            index_elements=[ordering_keys.c.key],
            set_={"sequence": ordering_keys.c.sequence + count, "touched_at": now},
        )
        .returning(ordering_keys.c.sequence)
    )
    return turn


async def take_turns(
    executor: Executor, dialect: str, counts: Mapping[str, int]
) -> dict[str, int]:
    """Take each key's turn, in key order; return each key's first new sequence.

    ``counts`` says how many messages of each key the transaction writes;
    0 takes the turn without writing one. Keys are taken in one order, so
    transactions that write several never wait for each other in a circle.
    """
    first: dict[str, int] = {}
    for key in sorted(counts):
        last = (await executor.execute(_turn(dialect, key, counts[key]))).scalar_one()
        first[key] = int(last) - counts[key] + 1
    return first


def _statuses(statuses: Sequence[str]) -> ColumnElement[bool]:
    """``status IN (...)``, written into the SQL so ``ORDERING_INDEX`` applies."""
    return messages.c.status.in_(
        [literal(status, literal_execute=True) for status in statuses]
    )


def _first(
    key: str, statuses: Sequence[str], column: ColumnElement[Any] | None = None
) -> ScalarSelect[Any]:
    """``key``'s first message in ``statuses`` (its sequence), through the index.

    ``column`` picks what to return instead of the sequence. The statuses
    are written into the SQL, and imply the index's condition, so the
    database reads ``ORDERING_INDEX`` even for a prepared statement.
    """
    return (
        select(messages.c.sequence if column is None else column)
        .where(messages.c.ordering_key == key, _statuses(statuses))
        .order_by(messages.c.sequence)
        .limit(1)
        .scalar_subquery()
    )


def busy(key: str) -> ColumnElement[bool]:
    """Whether ``key`` has a message waiting (pending or held) to run."""
    return _first(key, WAITING).is_not(None)


def first_status(key: str) -> ColumnElement[str]:
    """``pending`` for a message first of ``key``'s waiting ones, else ``held``."""
    return case((busy(key), literal(HELD)), else_=literal(PENDING))


@dataclass(frozen=True, slots=True)
class Promoted:
    """A message made ``pending`` because the one before it finished."""

    id: uuid.UUID
    queue: str


async def promote(
    connection: AsyncConnection, dialect: str, key: str
) -> Promoted | None:
    """Make ``key``'s next held message pending, unless one is pending already.

    Called after the transaction recorded a message of ``key`` as finished
    (or cancelled), it takes the key's turn first, so the commits that
    write the key's messages come before it or after it, never beside it.
    """
    await take_turns(connection, dialect, {key: 0})
    statement = (
        update(messages)
        .where(
            messages.c.id == _first(key, (HELD,), messages.c.id),
            _first(key, (PENDING,)).is_(None),
        )
        .values(status=PENDING)
        .returning(messages.c.id, messages.c.queue)
    )
    row = (await connection.execute(statement)).first()
    return None if row is None else Promoted(row.id, row.queue)


async def requeue(
    connection: AsyncConnection,
    dialect: str,
    key: str,
    dead: ColumnElement[bool],
    values: Mapping[str, object],
) -> int:
    """Give ``key``'s ``dead`` messages back, each in its place in the key's line.

    Each takes ``values`` too, and keeps its sequence. It goes before the
    messages of its key committed after it that still wait, the pending
    one included while no worker has started it, nor a relay handed it to
    the broker (``passed_over``); it goes after one that has, which runs
    first. With nothing of the key waiting, the first given back is
    pending, and the rest are held behind it.
    """
    await take_turns(connection, dialect, {key: 0})
    order = (
        select(messages.c.id, messages.c.sequence)
        .where(dead, messages.c.ordering_key == key)
        .order_by(messages.c.sequence)
    )
    count = 0
    for message_id, sequence in (await connection.execute(order)).all():
        passed = await passed_over(connection, dialect, key, sequence)
        result = await connection.execute(
            update(messages)
            .where(messages.c.id == message_id, dead)
            .values(status=PENDING if passed else first_status(key), **values)
        )
        count += int(result.rowcount)
    return count


async def passed_over(
    connection: AsyncConnection, dialect: str, key: str, sequence: int
) -> bool:
    """Hold ``key``'s pending message again if it comes after ``sequence``.

    Only one that is not under way: no worker runs it (``started_at``), and
    no relay holds it for the broker (a live lock). Either would run it
    whatever its status says, so it keeps its turn. The caller holds the
    key's turn; the condition is checked again on the row, so a claim that
    took it meanwhile wins. Return whether a message was held back.
    """
    now = dbclock.now(dialect)
    result = await connection.execute(
        update(messages)
        .where(
            messages.c.ordering_key == key,
            _statuses((PENDING,)),
            messages.c.sequence > sequence,
            messages.c.started_at.is_(None),
            or_(messages.c.locked_until.is_(None), messages.c.locked_until < now),
        )
        .values(status=HELD, locked_until=None, lock_id=None)
    )
    return bool(result.rowcount)


def stalled(size: int) -> Select[tuple[str]]:
    """Up to ``size`` keys with held messages and none pending, through the index.

    No transaction leaves a key so: the one that finishes its pending
    message makes the next pending. A process that does not know the held
    status does, such as a 0.13 worker finishing a message 0.14 wrote
    during a rolling deploy (SEC-14c-3); ``promote`` repairs each.
    """
    first = messages.alias("first")
    pending = (
        select(first.c.id)
        .where(
            first.c.ordering_key == messages.c.ordering_key,
            first.c.status.in_([literal(PENDING, literal_execute=True)]),
        )
        .exists()
    )
    return (
        select(messages.c.ordering_key)
        .where(messages.c.ordering_key.is_not(None), _statuses((HELD,)), ~pending)
        .distinct()
        .order_by(messages.c.ordering_key)
        .limit(literal(size, literal_execute=True))
    )


async def stalled_keys(connection: AsyncConnection, size: int) -> list[str]:
    """The keys ``stalled`` finds, on ``connection``."""
    return [str(key) for key in (await connection.execute(stalled(size))).scalars()]


def idle_keys(dialect: str, size: int, retention: float) -> Delete:
    """The ``DELETE`` of up to ``size`` key rows no message needs any more.

    A key's row goes once it was not written for ``retention`` seconds and
    none of its messages waits or is dead: a dead message may come back,
    and must still sort before the key's later ones. Rows another
    transaction holds are skipped on PostgreSQL.
    """
    needed = (
        select(messages.c.id)
        .where(messages.c.ordering_key == ordering_keys.c.key, _statuses(KEYED))
        .exists()
    )
    idle = (
        select(ordering_keys.c.key)
        .where(
            ordering_keys.c.touched_at < dbclock.at(dialect, -retention),
            not_(needed),
        )
        .limit(literal(size, literal_execute=True))
        .with_for_update(skip_locked=True)
    )
    return delete(ordering_keys).where(ordering_keys.c.key.in_(idle.scalar_subquery()))


def counts_of(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """How many of ``rows`` each ordering key has."""
    counts: dict[str, int] = {}
    for row in rows:
        if (key := row["ordering_key"]) is not None:
            counts[key] = counts.get(key, 0) + 1
    return counts
