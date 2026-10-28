"""The keys' statements, built once, send what the statements built per request sent.

The builders below are 0.9.1's, as each request ran them, with what 0.11
added for tombstones (FIN-84): the claim and the sweep leave an expired key
that may have committed alone until it is a tombstone, the read of a held
key says whether it expired, and ``bury`` makes tombstones. Without
tombstones (``TOMBSTONE_SECONDS = 0``) the claim and the sweep are 0.9.1's
unchanged. Every statement built once must compile to the same SQL text
with the same parameters, on each dialect and each clock, and send the
same to the database.
"""

import asyncio
import threading
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy import (
    ColumnElement,
    Delete,
    Update,
    and_,
    delete,
    event,
    make_url,
    null,
    or_,
    select,
    update,
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import Connection, Dialect
from sqlalchemy.sql import ClauseElement, Executable
from sqlalchemy.sql.compiler import SQLCompiler

from domainkit import clock
from domainkit.clock import freeze_time
from domainkit.conf import DatabaseConfig
from domainkit.contrib.idempotency import statements
from domainkit.contrib.idempotency.middleware import PURGE_BATCH
from domainkit.contrib.idempotency.operations import (
    Operation,
    committed_values,
    mark_committed,
    pin_lease,
)
from domainkit.contrib.idempotency.statements import (
    EXPIRED,
    LAPSED,
    MARK_COMMITTED,
    PIN_LEASE,
    RELEASE,
    STORE,
    clock_statements,
    holder,
)
from domainkit.contrib.idempotency.tables import idempotency_keys
from domainkit.db import dbclock
from domainkit.db.connections import create_engine

keys = idempotency_keys.c
OPERATION = Operation("h" * 64, "t" * 32)
HELD_BY = "o" * 32
FINGERPRINT = "f" * 64
SUBJECTS = "\x1falice\x1f"
TTL, LEASE, TOMBSTONE = 86_400.0, 30.0, 604_800.0
FROZEN = datetime(2026, 10, 25, 9, 30, 15, 123456, tzinfo=UTC)
DIALECTS: dict[str, Dialect] = {
    "asyncpg": postgresql.asyncpg.dialect(),  # type: ignore[no-untyped-call]
    "psycopg": postgresql.psycopg.dialect(),  # type: ignore[no-untyped-call]
    "aiosqlite": sqlite.aiosqlite.dialect(),
}


# 0.9.1's statements, built for each request ----------------------------------------


def later(now: dbclock.Moment, seconds: float) -> dbclock.Moment:
    """0.9.1's ``_later``."""
    return now + timedelta(seconds=seconds)


def forgotten(now: dbclock.Moment, tombstones: bool) -> ColumnElement[bool]:
    """The expired rows a claim replaces, and a sweep deletes.

    0.9.1's: every expired row. With tombstones (0.11), of those whose
    request committed or pinned the key, only tombstones.
    """
    if not tombstones:
        return keys.expires_at < now
    return and_(
        keys.expires_at < now,
        or_(
            and_(keys.committed_at.is_(None), keys.pinned.is_(False)),
            keys.tombstone.is_(True),
        ),
    )


def old_claim(dialect: str, tombstones: bool) -> Executable:
    """0.9.1's claim, as ``_claim`` built it, and 0.11's with tombstones."""
    now = dbclock.now(dialect)
    upsert = postgresql.insert if dialect == "postgresql" else sqlite.insert
    claim = upsert(idempotency_keys).values(
        key_hash=OPERATION.key_hash,
        fingerprint=FINGERPRINT,
        created_at=now,
        expires_at=later(now, TTL),
        token=OPERATION.token,
        locked_until=later(now, LEASE),
        pinned=False,
        tombstone=False,  # 0.11's column, set as ``pinned`` is.
    )
    fresh = claim.excluded
    return claim.on_conflict_do_update(
        index_elements=[keys.key_hash],
        set_={
            "fingerprint": fresh.fingerprint,
            "status_code": null(),
            "headers": null(),
            "body": null(),
            "created_at": fresh.created_at,
            "expires_at": fresh.expires_at,
            "committed_at": null(),
            "token": fresh.token,
            "locked_until": fresh.locked_until,
            "pinned": False,
            "subjects": null(),  # 0.10's column, cleared as the others.
            "tombstone": False,  # 0.11's.
        },
        where=forgotten(now, tombstones),
    ).returning(keys.token)


def old_read_held(dialect: str) -> Executable:
    """0.9.1's read of a held key's row, with whether it expired (0.11)."""
    lapsed = keys.locked_until < dbclock.now(dialect)
    expired = keys.expires_at < dbclock.now(dialect)
    return select(idempotency_keys, lapsed.label(LAPSED), expired.label(EXPIRED)).where(
        keys.key_hash == OPERATION.key_hash
    )


def old_sweep(dialect: str, batch: int | None, tombstones: bool) -> Delete:
    """0.9.1's ``_sweep``, and the hourly purge's delete (``batch`` None)."""
    due = forgotten(dbclock.now(dialect), tombstones)
    if batch is None:
        return delete(idempotency_keys).where(due)
    oldest = (
        select(keys.key_hash).where(due).limit(batch).with_for_update(skip_locked=True)
    )
    return delete(idempotency_keys).where(keys.key_hash.in_(oldest.scalar_subquery()))


def new_bury(dialect: str, batch: int | None) -> Update:
    """0.11's tombstones of expired keys that may have committed, built per call."""
    now = dbclock.now(dialect)
    due = and_(
        keys.expires_at < now,
        keys.tombstone.is_(False),
        or_(keys.committed_at.is_not(None), keys.pinned.is_(True)),
    )
    if batch is not None:
        oldest = (
            select(keys.key_hash)
            .where(due)
            .limit(batch)
            .with_for_update(skip_locked=True)
        )
        due = keys.key_hash.in_(oldest.scalar_subquery())
    return (
        update(idempotency_keys)
        .where(due)
        .values(
            headers=null(),
            body=null(),
            subjects=null(),
            tombstone=True,
            expires_at=later(now, TOMBSTONE),
        )
    )


def old_take_over(dialect: str) -> Update:
    """0.9.1's ``_take_over`` statement."""
    now = dbclock.now(dialect)
    return (
        update(idempotency_keys)
        .where(
            keys.key_hash == OPERATION.key_hash,
            keys.token == HELD_BY,
            keys.status_code.is_(None),
            keys.committed_at.is_(None),
            keys.pinned.is_(False),
        )
        .values(token=OPERATION.token, locked_until=later(now, LEASE))
    )


def old_release() -> Delete:
    """0.9.1's release in ``_finish``."""
    return delete(idempotency_keys).where(
        OPERATION.holds_key(), keys.committed_at.is_(None), keys.pinned.is_(False)
    )


def old_store() -> Update:
    """0.9.1's store in ``_finish``, with whom it is about (0.10's ``subjects``)."""
    return (
        update(idempotency_keys)
        .where(OPERATION.holds_key())
        .values(
            status_code=201,
            headers={"location": "/items/1"},
            body=b"paid",
            subjects=SUBJECTS,
        )
    )


type Pair = tuple[Executable, tuple[Executable, dict[str, Any]]]


def pairs(dialect: str) -> list[Pair]:
    """Each 0.9.1 statement, and the one built once with its values, now."""
    built = clock_statements(dialect)
    now = built.now()
    claim_values = built.claim_values(
        now, OPERATION.key_hash, FINGERPRINT, OPERATION.token, TTL, LEASE
    )
    sweeps: list[Pair] = [
        (
            old_sweep(dialect, batch, tombstones),
            (built.sweep(batch, tombstones), built.sweep_values(now)),
        )
        for batch in (PURGE_BATCH, 2, None)
        for tombstones in (True, False)
    ]
    burials: list[Pair] = [
        (
            new_bury(dialect, batch),
            (built.bury(batch), built.bury_values(now, TOMBSTONE)),
        )
        for batch in (PURGE_BATCH, None)
    ]
    return [
        (old_claim(dialect, tombstones=True), (built.claim, claim_values)),
        (old_claim(dialect, tombstones=False), (built.claim_forgetting, claim_values)),
        (
            old_read_held(dialect),
            (built.read_held, built.read_held_values(now, OPERATION.key_hash)),
        ),
        *sweeps,
        *burials,
        (
            old_take_over(dialect),
            (
                built.take_over,
                built.take_over_values(
                    now, OPERATION.key_hash, HELD_BY, OPERATION.token, LEASE
                ),
            ),
        ),
        (old_release(), (RELEASE, holder(OPERATION.key_hash, OPERATION.token))),
        (
            old_store(),
            (
                STORE,
                {
                    "status_code": 201,
                    "headers": {"location": "/items/1"},
                    "body": b"paid",
                    "subjects": SUBJECTS,
                    **holder(OPERATION.key_hash, OPERATION.token),
                },
            ),
        ),
        (
            pin_lease(OPERATION),
            (PIN_LEASE, holder(OPERATION.key_hash, OPERATION.token)),
        ),
    ] + (
        # The mark reads the host's clock, on every database: frozen, both agree.
        [(mark_committed(OPERATION), (MARK_COMMITTED, committed_values(OPERATION)))]
        if clock.frozen() is not None
        else []
    )


def compiled(
    statement: Executable, values: dict[str, Any], dialect: Dialect
) -> tuple[str, object]:
    """The SQL and typed parameters ``statement`` sends with ``values``.

    Compiled as ``Connection.execute`` compiles it, with the values' names
    as the column keys, and the parameters in the order the driver gets
    them. A parameter's type decides how the dialect sends its value.
    """
    sql = cast(
        SQLCompiler,
        cast(ClauseElement, statement).compile(
            dialect=dialect, column_keys=sorted(values)
        ),
    )
    params = sql.construct_params(values)
    typed = {
        name: (value, repr(sql.binds[name].type)) for name, value in params.items()
    }
    if sql.positional:
        return str(sql), [typed[name] for name in sql.positiontup or ()]
    return str(sql), typed


def clocks(dialect: str) -> list[bool]:
    """Whether to freeze the clock: both ways on PostgreSQL, frozen elsewhere.

    The host's clock moves between two statements built one after the
    other; frozen, both read the same time, as one request's statements do.
    """
    return [False, True] if dialect == "postgresql" else [True]


@pytest.mark.parametrize("driver", list(DIALECTS))
def test_statements_built_once_compile_as_those_built_per_request(
    driver: str,
) -> None:
    """Same SQL text and same parameters, statement by statement."""
    dialect = DIALECTS[driver]

    for frozen in clocks(dialect.name):
        with freeze_time(FROZEN) if frozen else nullcontext():
            for old, (new, values) in pairs(dialect.name):
                assert compiled(new, values, dialect) == compiled(old, {}, dialect)


def test_each_dialect_and_clock_has_statements_of_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Built once for each, found without the lock, told apart by the clock."""
    database = clock_statements("postgresql")
    with monkeypatch.context() as patched:
        patched.setattr(statements, "_lock", None)  # Built: never taken.
        assert clock_statements("postgresql") is database
    assert database.database_clock
    assert database.now() is None
    with freeze_time(FROZEN):
        frozen = clock_statements("postgresql")
        assert (frozen.database_clock, frozen.now()) == (False, FROZEN)
    local = clock_statements("sqlite")
    assert not local.database_clock
    assert len({id(database), id(frozen), id(local)}) == 3
    assert database.sweep(PURGE_BATCH, True) is database.sweep(PURGE_BATCH, True)
    assert database.sweep(2, True) is not database.sweep(PURGE_BATCH, True)
    assert database.sweep(2, False) is not database.sweep(2, True)
    assert database.bury(2) is database.bury(2)
    assert database.bury(2) is not database.bury(None)


def test_what_reaches_the_database_is_the_same(database_url: str) -> None:
    """On the test database, each pair sends the same statement and parameters."""
    sent: list[tuple[str, object]] = []

    def record(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        sent.append((statement, parameters))

    async def run(engine_url: str) -> list[tuple[list[Any], list[Any]]]:
        engine = create_engine(DatabaseConfig.model_validate({"URL": engine_url}))
        event.listen(engine.sync_engine, "before_cursor_execute", record)
        found = []
        try:
            async with engine.begin() as connection:
                await connection.run_sync(idempotency_keys.create)
                for old, (new, values) in pairs(engine.dialect.name):
                    sent.clear()
                    await connection.execute(old)
                    await connection.execute(new, values)
                    found.append(([sent[0]], [sent[1]]))
                await connection.rollback()
        finally:
            await engine.dispose()
        return found

    for frozen in clocks(make_url(database_url).get_backend_name()):
        with freeze_time(FROZEN) if frozen else nullcontext():
            for old, new in asyncio.run(run(database_url)):
                assert new == old


def test_statements_another_thread_built_meanwhile_are_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller that waited for the lock finds them built, and builds none."""
    done = statements._build("sqlite", False)
    built: dict[tuple[str, bool], statements.ClockStatements] = {}
    lock = threading.Lock()

    class Waited:
        """The lock, held meanwhile by a thread that built the statements."""

        def __enter__(self) -> None:
            lock.acquire()
            built[("sqlite", False)] = done

        def __exit__(self, *exc: object) -> None:
            lock.release()

    def never(dialect: str, database_clock: bool) -> statements.ClockStatements:
        raise AssertionError("built twice")

    monkeypatch.setattr(statements, "_built", built)
    monkeypatch.setattr(statements, "_lock", Waited())
    monkeypatch.setattr(statements, "_build", never)

    assert clock_statements("sqlite") is done
