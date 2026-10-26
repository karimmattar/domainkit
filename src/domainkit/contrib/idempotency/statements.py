"""The keys' statements, built once and run with each request's values.

Building a statement (``insert(...).values(...)``, ``on_conflict_do_update``,
``excluded``) is work in Python that does not depend on the request: a keyed
write spent 41% of its CPU on it (audit 0.9.0, PERF-12). So each statement is
built once, per dialect and per clock (``dbclock``: the database's, or the
host's on SQLite and under ``freeze_time``), with bound parameters where the
request's values go, and each request passes only its values.

The bound parameters have the names SQLAlchemy gave the values it bound
itself, so each statement compiles to the SQL text, and the parameters, the
statements built for each request sent: what reaches the database is the
same, byte for byte. Statements are immutable and shared by every event loop
and thread; the ones not built yet are built under a lock.
"""

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Any, Final

from sqlalchemy import (
    BindParameter,
    ColumnElement,
    Delete,
    Interval,
    Select,
    Update,
    and_,
    bindparam,
    delete,
    null,
    select,
    update,
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.sql.dml import ReturningInsert

from domainkit import clock
from domainkit.db import dbclock

from .tables import idempotency_keys

keys: Final = idempotency_keys.c
SWEEPS: Final = 8
"""Sweeps kept built: one per dialect, clock, and batch size in use."""
LAPSED: Final = "lapsed"
"""Read with a held key's row: whether its lease ran out, on the database's clock."""


def _bound(name: str, column: str) -> BindParameter[Any]:
    """A parameter ``name`` of the type of ``column``, as a value bound to it gets."""
    return bindparam(name, type_=idempotency_keys.c[column].type)


HOLDER: Final[ColumnElement[bool]] = and_(
    keys.key_hash == _bound("key_hash_1", "key_hash"),
    keys.token == _bound("token_1", "token"),
)
"""The key's row while the request holding ``token_1`` holds it."""
RELEASE: Final[Delete] = delete(idempotency_keys).where(
    HOLDER, keys.committed_at.is_(None), keys.pinned.is_(False)
)
"""Release a key nothing committed or pinned, if its request still holds it."""
STORE: Final[Update] = (
    update(idempotency_keys)
    .where(HOLDER)
    .values(
        status_code=_bound("status_code", "status_code"),
        headers=_bound("headers", "headers"),
        body=_bound("body", "body"),
        subjects=_bound("subjects", "subjects"),
    )
)
"""Store the response (``status_code``, ``headers``, ``body``, and whom it is
about, ``subjects``) under its holder."""
PIN_LEASE: Final[Update] = (
    update(idempotency_keys)
    .where(HOLDER)
    .values(locked_until=keys.expires_at, pinned=True)
)
"""Hold the key until it expires, never released (``operations.pin_lease``)."""
MARK_COMMITTED: Final[Update] = (
    update(idempotency_keys)
    .where(HOLDER)
    .values(committed_at=_bound("committed_at", "committed_at"))
)
"""Record that the holder committed (``operations.mark_committed``)."""


def holder(key_hash: str, token: str) -> dict[str, Any]:
    """The values of ``HOLDER``: the key's hash, and the token holding it."""
    return {"key_hash_1": key_hash, "token_1": token}


@dataclass(frozen=True, slots=True)
class ClockStatements:
    """The statements that read the clock, for one dialect and one clock."""

    dialect: str
    database_clock: bool
    """Whether ``statement_timestamp()`` decides, or values from the host."""
    claim: ReturningInsert[Any]
    """Insert the key's row, or replace an expired one; returns the token."""
    read_held: Select[Any]
    """The row of a key held by another request, and whether it ``LAPSED``."""
    take_over: Update
    """Take the key from the holder ``token_1`` if it still holds it, unfinished."""

    def now(self) -> datetime | None:
        """The host's time, which the values of one use share; None on the database's.

        Read once for the statements of one claim, as the database reads
        ``statement_timestamp()`` once for each.
        """
        return None if self.database_clock else clock.now()

    def claim_values(
        self,
        now: datetime | None,
        key_hash: str,
        fingerprint: str,
        token: str,
        ttl: float,
        lease: float,
    ) -> dict[str, Any]:
        """The values of ``claim``: the key, and its expiry and lease from ``now``."""
        values = {"key_hash": key_hash, "fingerprint": fingerprint, "token": token}
        if now is None:
            return values | {
                "statement_timestamp_1": timedelta(seconds=ttl),
                "statement_timestamp_2": timedelta(seconds=lease),
            }
        return values | {
            "created_at": now,
            "expires_at": _later(now, ttl),
            "locked_until": _later(now, lease),
            "expires_at_1": now,
        }

    def read_held_values(self, now: datetime | None, key_hash: str) -> dict[str, Any]:
        """The values of ``read_held``."""
        if now is None:
            return {"key_hash_1": key_hash}
        return {"locked_until_1": now, "key_hash_1": key_hash}

    def sweep(self, batch: int) -> Delete:
        """Delete up to ``batch`` expired keys, skipping those being claimed."""
        return _sweep(self.dialect, self.database_clock, batch)

    def sweep_values(self, now: datetime | None) -> dict[str, Any]:
        """The values of ``sweep``: none on the database's clock."""
        return {} if now is None else {"expires_at_1": now}

    def take_over_values(
        self,
        now: datetime | None,
        key_hash: str,
        held_by: str,
        token: str,
        lease: float,
    ) -> dict[str, Any]:
        """The values of ``take_over``: the new holder's token and lease."""
        values = {"token": token, **holder(key_hash, held_by)}
        if now is None:
            return values | {"statement_timestamp_1": timedelta(seconds=lease)}
        return values | {"locked_until": _later(now, lease)}


_built: dict[tuple[str, bool], ClockStatements] = {}
_lock: Final = threading.Lock()


def clock_statements(dialect: str) -> ClockStatements:
    """The statements for ``dialect`` and the clock that decides on it now."""
    key = (dialect, dbclock.uses_database(dialect))
    built = _built.get(key)
    if built is None:
        with _lock:
            built = _built.get(key)
            if built is None:
                built = _built[key] = _build(*key)
    return built


def _build(dialect: str, database_clock: bool) -> ClockStatements:
    """Build the statements for ``dialect`` on the database's or the host's clock."""
    if database_clock:
        now: dbclock.Moment = dbclock.now(dialect)
        ttl = now + bindparam("statement_timestamp_1", type_=Interval())
        lease = now + bindparam("statement_timestamp_2", type_=Interval())
        new_lease = now + bindparam("statement_timestamp_1", type_=Interval())
        created, expires, locked = now, ttl, lease
        lapsed_before = claim_before = now
    else:
        created = _bound("created_at", "created_at")
        expires = _bound("expires_at", "expires_at")
        locked = new_lease = _bound("locked_until", "locked_until")
        claim_before = _bound("expires_at_1", "expires_at")
        lapsed_before = _bound("locked_until_1", "locked_until")
    insert = postgresql.insert if dialect == "postgresql" else sqlite.insert
    claim = insert(idempotency_keys).values(
        key_hash=_bound("key_hash", "key_hash"),
        fingerprint=_bound("fingerprint", "fingerprint"),
        created_at=created,
        expires_at=expires,
        token=_bound("token", "token"),
        locked_until=locked,
        pinned=False,
    )
    fresh = claim.excluded
    return ClockStatements(
        dialect=dialect,
        database_clock=database_clock,
        claim=claim.on_conflict_do_update(
            index_elements=[keys.key_hash],
            set_={
                "fingerprint": fresh.fingerprint,
                "status_code": null(),
                "headers": null(),  # SQL NULL, not JSON's null.
                "body": null(),
                "created_at": fresh.created_at,
                "expires_at": fresh.expires_at,
                "committed_at": null(),
                "token": fresh.token,
                "locked_until": fresh.locked_until,
                "pinned": False,
                "subjects": null(),
            },
            where=keys.expires_at < claim_before,
        ).returning(keys.token),
        read_held=select(
            idempotency_keys, (keys.locked_until < lapsed_before).label(LAPSED)
        ).where(keys.key_hash == _bound("key_hash_1", "key_hash")),
        take_over=update(idempotency_keys)
        .where(
            HOLDER,
            keys.status_code.is_(None),
            keys.committed_at.is_(None),
            keys.pinned.is_(False),
        )
        .values(token=_bound("token", "token"), locked_until=new_lease),
    )


@lru_cache(maxsize=SWEEPS)
def _sweep(dialect: str, database_clock: bool, batch: int) -> Delete:
    """Build ``ClockStatements.sweep``; the batch is part of the SQL, as a literal.

    ``lru_cache`` keeps its entries under a lock of its own.
    """
    before: dbclock.Moment = (
        dbclock.now(dialect) if database_clock else _bound("expires_at_1", "expires_at")
    )
    oldest = (
        select(keys.key_hash)
        .where(keys.expires_at < before)
        .limit(batch)
        .with_for_update(skip_locked=True)
    )
    return delete(idempotency_keys).where(keys.key_hash.in_(oldest.scalar_subquery()))


def _later(now: datetime, seconds: float) -> datetime:
    """``seconds`` after ``now``."""
    return now + timedelta(seconds=seconds)
