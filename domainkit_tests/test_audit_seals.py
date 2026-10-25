"""The audit trail's hash chain, its anchors on a ledger, and verification."""

import asyncio
import hashlib
import importlib
import io
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from prometheus_client import REGISTRY
from pydantic import ValidationError
from sqlalchemy import delete, event, func, insert, select, update

from domainkit import clock, setup
from domainkit.apps import registry
from domainkit.clock import freeze_time
from domainkit.conf import override_settings, settings
from domainkit.context import RequestContext, bind_request
from domainkit.contrib.audit import apps as audit_apps
from domainkit.contrib.audit import record
from domainkit.contrib.audit import seals as seals_module
from domainkit.contrib.audit.management.commands.auditseal import (
    Command as SealCommand,
)
from domainkit.contrib.audit.management.commands.auditverify import (
    Command as VerifyCommand,
)
from domainkit.contrib.audit.management.commands.auditverify import _summary
from domainkit.contrib.audit.protection import protect, statements
from domainkit.contrib.audit.seals import (
    COLUMNS,
    GENESIS,
    LOOKBACK,
    Anchor,
    Report,
    anchor,
    anchor_key,
    digest,
    link,
    seal,
    seal_all,
    unsealed_backlog,
    verify,
)
from domainkit.contrib.audit.settings import AuditSettings
from domainkit.contrib.audit.tables import audit_keys, audit_log, audit_seals
from domainkit.db import connections
from domainkit.ledger import LedgerEntry, LedgerKeyNotFound, MemoryLedger, ledgers
from domainkit.management import CommandError, call_command
from domainkit_tests.conftest import POSTGRES_VARIABLE
from domainkit_tests.test_postgres import Migration

pytestmark = pytest.mark.anyio

WINDOW = AuditSettings().SEAL_LATE_WINDOW

AUDIT = "domainkit.contrib.audit"
ALPHA = "domainkit_tests.sample_domains.alpha"
on_postgres = pytest.mark.skipif(
    not os.environ.get(POSTGRES_VARIABLE),
    reason=f"set {POSTGRES_VARIABLE} to a PostgreSQL server",
)


@pytest.fixture
def audited(database_url: str) -> Iterator[str]:
    """The audit app on an empty database, anchoring on a memory ledger."""
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[AUDIT, ALPHA],
        DATABASES={"default": {"URL": database_url}},
        LEDGERS={"audit": {"URL": "memory://"}},
        AUDIT={"LEDGER": "audit"},
    ):
        setup()
        yield database_url
    registry.clear()


@pytest.fixture
async def tables(audited: str) -> AsyncIterator[None]:
    """Create the audit tables."""
    async with connections.engine().begin() as connection:
        for table in (audit_log, audit_keys, audit_seals):
            await connection.run_sync(table.create)
    yield
    await connections.close_all()
    await ledgers.close_all()  # Memory ledgers are kept per process.


@pytest.fixture
def local_zone() -> Iterator[Callable[[str], None]]:
    """Set the process's local time zone; the original comes back afterwards."""
    previous = os.environ.get("TZ")

    def use(name: str) -> None:
        os.environ["TZ"] = name
        time.tzset()

    yield use
    if previous is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = previous
    time.tzset()


async def add(occurred_at: datetime | None = None, **details: object) -> uuid.UUID:
    """Write one row, as ``record`` does, at ``occurred_at``; return its id."""
    entry = uuid.uuid4()
    async with connections.engine().begin() as connection:
        await connection.execute(
            insert(audit_log).values(
                id=entry,
                occurred_at=occurred_at or datetime.now(UTC),
                actor="system",
                action="test.added",
                outcome="success",
                details=details,
            )
        )
    return entry


async def seals() -> list[dict[str, Any]]:
    """The chain, in order."""
    async with connections.engine().connect() as connection:
        rows = await connection.execute(select(audit_seals).order_by(audit_seals.c.seq))
        return [dict(row._mapping) for row in rows]


async def rows() -> dict[uuid.UUID, dict[str, Any]]:
    """The trail's rows by id."""
    async with connections.engine().connect() as connection:
        found = await connection.execute(select(audit_log))
        return {row.id: dict(row._mapping) for row in found}


async def change(statement: Any) -> None:  # noqa: ANN401 - any DML statement.
    """Run ``statement``, as someone who can rewrite the tables would."""
    async with connections.engine().begin() as connection:
        await connection.execute(statement)


def test_digests_and_links_have_a_fixed_form(
    local_zone: Callable[[str], None],
) -> None:
    """Old rows must verify after upgrades: the canonical JSON never changes.

    Times are written in UTC, whatever the server's local time zone.
    """
    local_zone("Etc/GMT-14")
    occurred = datetime(2026, 10, 18, 12, 0, 0, 123456, tzinfo=UTC)
    row = {
        "id": uuid.UUID(int=1),
        "occurred_at": occurred,
        "actor": "alice",
        "action": "shop.order_paid",
        "outcome": "success",
        "target": None,
        "request_id": "req-1",
        "client_ip": None,
        "event_id": uuid.UUID(int=2),
        "details": {"b": 1, "a": "é"},
        "ignored": "columns outside the digest",
    }

    first = digest(row)

    assert first == "0de84a764366543b4fdd21716ad1c421fa555b98a8e4bfcda228faa5437c6601"
    assert link(GENESIS, first) == (
        "436924bbec8954368e9179addb52ac93ec3de1e43c0107ca3979fa0993c3ed47"
    )
    assert GENESIS == "0" * 64
    elsewhere = occurred.astimezone(timezone(timedelta(hours=-5)))
    assert digest({**row, "occurred_at": elsewhere}) == first  # Times are UTC's.


def test_anchors_round_trip_through_the_ledger_value() -> None:
    """An anchor is canonical JSON of its number, link, and time."""
    written = Anchor(7, "ab" * 32, datetime(2026, 10, 18, 9, 30, tzinfo=UTC))

    assert written.encode() == (
        b'{"anchored_at":"2026-10-18T09:30:00+00:00","link":"'
        + b"ab" * 32
        + b'","seq":7}'
    )
    assert Anchor.decode(written.encode()) == written


def test_lone_surrogates_are_digested_as_they_are() -> None:
    """JSON can escape half a surrogate pair; a row holding one has a digest."""
    row = dict.fromkeys(COLUMNS) | {"details": {"name": "\ud800"}}
    canonical = b'{"name":"\xed\xa0\x80"}'  # The surrogate, UTF-8 style.

    expected = hashlib.sha256(
        b'{"action":null,"actor":null,"client_ip":null,"details":'
        + canonical
        + b',"event_id":null,"id":null,"occurred_at":null,"outcome":null,'
        b'"request_id":null,"target":null}'
    ).hexdigest()
    assert digest(row) == expected


async def test_rows_with_lone_surrogates_are_sealed(tables: None) -> None:
    """One such row cannot stop the chain: it and the rows after it are sealed."""
    start = datetime.now(UTC)
    await add(start)
    await add(start + timedelta(seconds=1), name="\ud800")
    await add(start + timedelta(seconds=2))

    assert await seal("default") == 3
    assert (await verify("default")).intact


async def test_rows_are_sealed_in_a_chain_once(tables: None) -> None:
    """Each row gets the next number; its link follows the one before."""
    start = datetime.now(UTC)
    ids = [await add(start + timedelta(seconds=n), n=n) for n in range(3)]

    assert await seal("default") == 3
    assert await seal("default") == 0
    later = await add(start + timedelta(seconds=9))
    assert await seal("default") == 1

    chain = await seals()
    trail = await rows()
    assert [each["entry_id"] for each in chain] == [*ids, later]
    assert [each["seq"] for each in chain] == [1, 2, 3, 4]
    previous = GENESIS
    for each in chain:
        assert each["digest"] == digest(trail[each["entry_id"]])
        assert each["link"] == link(previous, each["digest"])
        previous = each["link"]


async def test_sealing_goes_in_batches(tables: None) -> None:
    """Many rows are sealed in several transactions, in one chain."""
    start = datetime.now(UTC)
    for n in range(5):
        await add(start + timedelta(seconds=n))

    inserts: list[int] = []

    def count(conn: Any, cursor: Any, statement: str, *args: Any) -> None:  # noqa: ANN401
        if statement.startswith("INSERT INTO domainkit_audit_seals"):
            inserts.append(1)

    engine = connections.engine().sync_engine
    event.listen(engine, "before_cursor_execute", count)
    try:
        assert await seal("default", batch=2) == 5
    finally:
        event.remove(engine, "before_cursor_execute", count)

    chain = await seals()
    assert [each["seq"] for each in chain] == [1, 2, 3, 4, 5]
    assert len(inserts) == 3  # 2, 2, then 1.
    assert (await verify("default")).intact


async def test_late_rows_are_sealed_within_the_late_window_only(tables: None) -> None:
    """A row that commits after newer ones is sealed after them, if not too late."""
    now = datetime.now(UTC)
    await add(now)
    await seal("default")
    late = await add(now - WINDOW + timedelta(seconds=1))
    too_late = await add(now - WINDOW - timedelta(seconds=1))

    assert await seal("default") == 1

    assert [each["entry_id"] for each in await seals()][-1] == late
    report = await verify("default", now=now + LOOKBACK)  # Once it is a day old.
    assert (report.unsealed, report.intact) == (1, False)
    assert too_late not in [each["entry_id"] for each in await seals()]


async def test_rows_of_the_same_instant_are_sealed_in_id_order(
    tables: None,
) -> None:
    """Ties are broken by id, not by the order rows happen to be stored."""
    now = datetime.now(UTC)
    later, earlier = uuid.UUID(int=2), uuid.UUID(int=1)
    async with connections.engine().begin() as connection:
        for entry in (later, earlier):
            await connection.execute(
                insert(audit_log).values(
                    id=entry,
                    occurred_at=now,
                    actor="system",
                    action="test.added",
                    outcome="success",
                    details={},
                )
            )

    await seal("default")

    assert [each["entry_id"] for each in await seals()] == [earlier, later]


async def test_the_late_window_includes_its_first_instant(tables: None) -> None:
    """A row exactly ``SEAL_LATE_WINDOW`` before the newest sealed one is sealed."""
    now = datetime.now(UTC)
    await add(now)
    await seal("default")
    edge = await add(now - WINDOW)

    assert await seal("default") == 1

    assert [each["entry_id"] for each in await seals()][-1] == edge


async def test_a_row_from_a_clock_ahead_leaves_none_unsealable(
    tables: None,
) -> None:
    """The look back starts at the present at most, not at a future row."""
    now = datetime.now(UTC)
    await add(now + 2 * LOOKBACK)
    await seal("default")
    rows_written_after = [await add(now + timedelta(seconds=n)) for n in range(2)]

    assert await seal("default") == 2

    assert [each["entry_id"] for each in await seals()][1:] == rows_written_after


async def test_sealing_goes_on_when_the_newest_sealed_row_is_gone(
    tables: None,
) -> None:
    """Without the head's row, every unsealed row is looked at."""
    now = datetime.now(UTC)
    newest = await add(now)
    await seal("default")
    await change(delete(audit_log).where(audit_log.c.id == newest))
    await add(now - 3 * LOOKBACK)

    assert await seal("default") == 1


async def test_a_sealer_whose_clock_runs_ahead_strands_no_rows(
    tables: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PostgreSQL's clock caps the mark and dates the seals, not the sealer's host.

    A host 40 minutes ahead seals a row it wrote on its own clock; then hosts
    with right clocks write on. On SQLite, which lives in the sealer's
    process, the host's clock is the database's: those rows are out of
    reach, and only the late pass seals them.
    """
    real = clock.now
    ahead = timedelta(minutes=40)
    now = real()
    await add(now - timedelta(minutes=1))
    await add(now + ahead)  # Written, and then sealed, by the fast host.
    monkeypatch.setattr(clock, "now", lambda: real() + ahead)
    await seal("default")
    monkeypatch.setattr(clock, "now", real)
    others = [await add(now + timedelta(seconds=n)) for n in (5, 60, 300)]
    monkeypatch.setattr(clock, "now", lambda: real() + ahead)
    reachable = await unsealed_backlog("default", WINDOW)

    sealed = await seal("default")

    monkeypatch.setattr(clock, "now", real)
    chain = await seals()
    on_postgres = connections.engine().dialect.name == "postgresql"
    assert (reachable, sealed) == ((3, 3) if on_postgres else (0, 0))
    stamped = [each["sealed_at"] - real() for each in chain]
    assert all(
        stamp < timedelta(minutes=1) if on_postgres else stamp > timedelta(minutes=30)
        for stamp in stamped
    )
    assert await seal("default", late=True) == (0 if on_postgres else 3)
    assert [each["entry_id"] for each in await seals()][2:] == others


async def test_rows_out_of_the_window_are_sealed_late_only_when_asked(
    tables: None,
) -> None:
    """``late=True`` seals them into the chain, flagged; ``verify`` counts them.

    Changing such a row afterwards is found like any other change.
    """
    now = datetime.now(UTC)
    first = await add(now)
    await seal("default")
    late = [await add(now - WINDOW - timedelta(hours=n)) for n in (1, 3, 2)]
    new = await add(now + timedelta(seconds=1))

    assert await seal("default") == 1
    assert await seal("default", late=True, batch=2) == 3

    chain = await seals()
    assert [(each["entry_id"], each["late"]) for each in chain] == [
        (first, False),
        (new, False),
        (late[1], True),  # Oldest first.
        (late[2], True),
        (late[0], True),
    ]
    report = await verify("default", now=now + LOOKBACK + timedelta(hours=4))
    assert (report.intact, report.unsealed, report.late) == (True, 0, 3)
    await change(update(audit_log).where(audit_log.c.id == late[0]).values(actor="eve"))
    report = await verify("default")
    assert (report.intact, report.changed) == (False, [late[0]])


async def test_late_seals_never_move_the_mark_back(tables: None) -> None:
    """The mark is the newest row sealed in time, whatever was sealed late after it."""
    now = datetime.now(UTC)
    await add(now)
    await seal("default")
    await add(now - timedelta(days=3))
    assert await seal("default", late=True) == 1
    within_window = await add(now - WINDOW + timedelta(seconds=1))
    await add(now - 2 * WINDOW)  # In reach only from the late row's time.

    assert await seal("default") == 1

    assert [each["entry_id"] for each in await seals()][-1] == within_window
    assert await unsealed_backlog("default", WINDOW) == 0


async def test_a_late_pass_leaves_the_windows_first_instant_alone(
    tables: None,
) -> None:
    """A row exactly ``SEAL_LATE_WINDOW`` before the mark is in reach, not late."""
    now = datetime.now(UTC)
    await add(now)
    await seal("default")
    edge = await add(now - WINDOW)

    assert await seals_module._seal_pass("default", 10, 1, WINDOW, late=True) == (
        0,
        True,
    )
    assert await seal("default") == 1
    assert [(each["entry_id"], each["late"]) for each in await seals()][-1] == (
        edge,
        False,
    )


async def test_a_frozen_clock_dates_the_seals_on_every_database(
    tables: None,
) -> None:
    """Under ``freeze_time`` the frozen clock decides, on PostgreSQL too."""
    with freeze_time("2040-01-01T00:00:00Z"):
        await add(datetime(2040, 1, 1, tzinfo=UTC))
        await seal("default")

    assert [each["sealed_at"] for each in await seals()] == [
        datetime(2040, 1, 1, tzinfo=UTC)
    ]


async def test_a_late_pass_without_a_mark_seals_nothing_more(tables: None) -> None:
    """With nothing sealed, or the mark's row gone, every row is in reach anyway.

    So a late pass then seals nothing, even rows committed since the usual
    pass: they are not late, and the next usual pass seals them.
    """
    assert await seal("default", late=True) == 0
    fresh = await add(datetime.now(UTC))
    assert await seals_module._seal_pass("default", 10, 1, WINDOW, late=True) == (
        0,
        True,
    )
    assert await seals() == []
    await change(delete(audit_log).where(audit_log.c.id == fresh))

    now = datetime.now(UTC)
    newest = await add(now)
    await seal("default")
    await change(delete(audit_log).where(audit_log.c.id == newest))
    await add(now - 3 * LOOKBACK)

    assert await seal("default", late=True) == 1
    assert [each["late"] for each in await seals()] == [False, False]


async def test_only_rows_older_than_the_lookback_count_as_unsealed(
    tables: None,
) -> None:
    """A fresh row may still be sealed; one at the boundary not yet late."""
    now = datetime(2026, 10, 18, 12, tzinfo=UTC)
    await add(now)
    await add(now - LOOKBACK)
    await add(now - LOOKBACK - timedelta(microseconds=1))

    report = await verify("default", now=now)

    assert report.unsealed == 1


async def test_an_intact_trail_verifies(tables: None) -> None:
    """Recorded rows, sealed and anchored, check out."""
    await record("report.viewed", target="r-1")
    await record("report.viewed", target="r-2")
    await seal_all()

    await record("report.viewed", target="r-3")
    second = await seal_all()  # Within ANCHOR_EVERY: sealed, not anchored.
    report = await verify("default", ledgers["audit"])

    assert second == {"default": (1, None)}
    assert report.intact
    assert (report.database, report.sealed, report.anchors) == ("default", 3, 1)
    assert report.changed == report.missing == report.broken == report.rewritten == []
    stored = await ledgers["audit"].read(anchor_key("default"))
    assert Anchor.decode(stored.value).anchored_at.tzinfo is UTC


async def test_changes_removals_and_broken_links_are_found(tables: None) -> None:
    """Each tampering is reported where it happened."""
    start = datetime.now(UTC)
    first, second, third, fourth = [
        await add(start + timedelta(seconds=n)) for n in range(4)
    ]
    await seal("default")

    await change(update(audit_log).where(audit_log.c.id == first).values(actor="eve"))
    await change(delete(audit_log).where(audit_log.c.id == second))
    await change(
        update(audit_seals).where(audit_seals.c.seq == 3).values(link="f" * 64)
    )
    report = await verify("default")

    assert report.changed == [first]
    assert report.missing == [second]
    assert report.broken == [3, 4]  # 3's link, and 4's, made from the real one.
    assert not report.intact
    assert {third, fourth}.isdisjoint(report.changed)


async def test_a_removed_seal_leaves_a_gap(tables: None) -> None:
    """The next number no longer follows, nor does its link."""
    start = datetime.now(UTC)
    for n in range(3):
        await add(start + timedelta(seconds=n))
    await seal("default")

    await change(delete(audit_seals).where(audit_seals.c.seq == 2))
    report = await verify("default")

    assert (report.broken, report.sealed) == ([3], 2)


async def test_a_rewritten_chain_no_longer_matches_its_anchors(tables: None) -> None:
    """Recomputing every seal after a change is caught by the ledger."""
    start = datetime.now(UTC)
    first = await add(start, amount="10.00")
    await add(start + timedelta(seconds=1))
    await seal_all()

    await change(
        update(audit_log)
        .where(audit_log.c.id == first)
        .values(details={"amount": "1000.00"})
    )
    await change(delete(audit_seals))
    await seal("default")  # The same numbers, the forged links.
    report = await verify("default", ledgers["audit"])

    assert (report.changed, report.broken, report.missing) == ([], [], [])
    assert report.rewritten == [2]


async def test_every_anchor_is_checked(tables: None) -> None:
    """A rewrite shows at each head anchored after it, not only the newest."""
    start = datetime.now(UTC)
    first = await add(start)
    await add(start + timedelta(seconds=1))
    await seal("default")
    await anchor("default", ledgers["audit"], every=timedelta(0))
    await add(start + timedelta(seconds=2))
    await seal("default")
    await anchor("default", ledgers["audit"], every=timedelta(0))
    before = await verify("default", ledgers["audit"])

    await change(update(audit_log).where(audit_log.c.id == first).values(actor="eve"))
    await change(delete(audit_seals))
    await seal("default")
    report = await verify("default", ledgers["audit"])

    assert (before.anchors, before.rewritten, before.intact) == (2, [], True)
    assert (report.anchors, report.rewritten) == (2, [2, 3])


async def test_an_anchored_seal_that_was_removed_is_reported(tables: None) -> None:
    """Truncating the chain below an anchored head shows."""
    start = datetime.now(UTC)
    for n in range(3):
        await add(start + timedelta(seconds=n))
    await seal_all()

    await change(delete(audit_seals).where(audit_seals.c.seq == 3))
    report = await verify("default", ledgers["audit"])

    assert report.rewritten == [3]


async def test_heads_are_anchored_at_most_once_per_period(tables: None) -> None:
    """New heads wait for ``every``; the same head is never anchored twice."""
    ledger = MemoryLedger()
    now = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
    every = timedelta(hours=1)

    assert await anchor("default", ledger, every=every, now=now) is None
    await add(now)
    await seal("default")
    first = await anchor("default", ledger, every=every, now=now)
    await add(now + timedelta(seconds=1))
    await seal("default")
    early = await anchor("default", ledger, every=every, now=now + every / 2)
    second = await anchor("default", ledger, every=every, now=now + every)
    again = await anchor("default", ledger, every=timedelta(0), now=now + 2 * every)

    assert first is not None
    assert (first.seq, first.anchored_at) == (1, now)
    assert early is None
    assert second is not None
    assert (second.seq, second.anchored_at) == (2, now + every)
    assert again is None
    stored = await ledger.read(anchor_key("default"))
    assert (stored.revision, Anchor.decode(stored.value)) == (2, second)
    assert anchor_key("default") == b"domainkit/audit/default"


async def test_an_anchor_from_a_clock_ahead_holds_none_back(tables: None) -> None:
    """Anchoring goes on; a head anchored at this very instant still waits."""
    ledger = MemoryLedger()
    now = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
    every = timedelta(hours=1)
    await add(now)
    await seal("default")
    await anchor("default", ledger, every=every, now=now + timedelta(days=365))
    await add(now + timedelta(seconds=1))
    await seal("default")

    caught_up = await anchor("default", ledger, every=every, now=now)
    await add(now + timedelta(seconds=2))
    await seal("default")
    same_instant = await anchor("default", ledger, every=every, now=now)

    assert caught_up is not None
    assert (caught_up.seq, caught_up.anchored_at) == (2, now)
    assert same_instant is None


async def test_an_anchor_written_meanwhile_is_not_written_again(
    tables: None,
) -> None:
    """Another sealer, or a late write, anchors between the read and the write."""

    class Raced(MemoryLedger):
        """Another writer anchors right after each read."""

        async def read(self, key: bytes, revision: int | None = None) -> LedgerEntry:
            found = await super().read(key, revision)
            await self.append(key, found.value)
            return found

    ledger = Raced()
    now = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
    every = timedelta(hours=1)
    await add(now)
    await seal("default")
    old = Anchor(0, "0" * 64, now - 2 * every)  # Due for a new one.
    await ledger.append(anchor_key("default"), old.encode())

    raced = await anchor("default", ledger, every=every, now=now)

    assert raced is None
    # The old anchor, and the other writer's; this one did not land.
    assert await ledger.revision_count(anchor_key("default"), limit=5) == 2


async def test_a_first_anchor_written_meanwhile_is_not_written_again(
    tables: None,
) -> None:
    """The same race, when the read found no anchor at all."""

    class Raced(MemoryLedger):
        """Another writer anchors first, right after a read that found none."""

        async def read(self, key: bytes, revision: int | None = None) -> LedgerEntry:
            try:
                return await super().read(key, revision)
            except LedgerKeyNotFound:
                await self.append(key, b"another sealer's anchor")
                raise

    ledger = Raced()
    now = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
    await add(now)
    await seal("default")

    raced = await anchor("default", ledger, every=timedelta(hours=1), now=now)

    assert raced is None
    assert await ledger.revision_count(anchor_key("default"), limit=5) == 1


def test_anchor_keys_carry_the_namespace() -> None:
    """``domainkit/audit/<namespace>/<alias>``; no namespace keeps the 0.9.0 key."""
    assert anchor_key("default", "shop") == b"domainkit/audit/shop/default"
    assert anchor_key("default") == b"domainkit/audit/default"


@pytest.mark.parametrize(
    "namespace", ["", "Shop", "1shop", "shop/eu", "shop\n", "s" * 33, "shop.eu"]
)
def test_audit_namespaces_are_checked(namespace: str) -> None:
    """A namespace is a short lower-case name, so it cannot reach another key."""
    with pytest.raises(ValidationError, match="NAMESPACE"):
        AuditSettings(NAMESPACE=namespace)


def test_audit_namespaces_accept_short_names() -> None:
    """Letters, digits, ``_`` and ``-``, up to 32, starting with a letter."""
    assert AuditSettings(NAMESPACE="shop-eu_2").NAMESPACE == "shop-eu_2"
    assert AuditSettings(NAMESPACE="s" * 32).NAMESPACE == "s" * 32
    assert AuditSettings().NAMESPACE is None
    assert AuditSettings().PREVIOUS_ANCHORS is True


async def test_projects_sharing_a_ledger_keep_their_anchors_apart(
    tables: None,
) -> None:
    """Another project's anchor neither holds ours back nor fails our chain (N11).

    At 0.9.0 both wrote ``domainkit/audit/default``: ours was skipped as
    too recent, and its head read as our rewritten anchor.
    """
    ledger = ledgers["audit"]
    now = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
    theirs = Anchor(seq=5, link="ab" * 32, anchored_at=now - timedelta(minutes=1))
    await ledger.append(anchor_key("default", "billing"), theirs.encode())
    await add(now - timedelta(seconds=2))
    await add(now - timedelta(seconds=1))
    assert await seal("default") == 2

    with override_settings(AUDIT={"LEDGER": "audit", "NAMESPACE": "shop"}):
        ours = await anchor("default", ledger, every=timedelta(hours=1), now=now)
        report = await verify("default", ledger, now=now)

    assert ours == Anchor(2, (await seals())[-1]["link"], now)
    assert report.intact
    assert (report.anchors, report.rewritten) == (1, [])
    stored = await ledger.read(anchor_key("default", "shop"))
    assert Anchor.decode(stored.value) == ours


async def test_heads_anchored_before_the_namespace_still_verify(
    tables: None,
) -> None:
    """Setting ``NAMESPACE`` on a running trail keeps its anchors checked.

    The heads anchored under the old key are read with the new key's, so the
    trail verifies across the change, and a rewrite of a head anchored
    before it is still found.
    """
    ledger = ledgers["audit"]
    now = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
    every = timedelta(hours=1)
    await add(now - timedelta(seconds=2))
    await seal("default")
    before = await anchor("default", ledger, every=every, now=now)
    namespaced = {"LEDGER": "audit", "NAMESPACE": "shop"}

    with override_settings(AUDIT=namespaced):
        unchanged = await verify("default", ledger, now=now)
        await add(now - timedelta(seconds=1))
        await seal("default")
        after = await anchor("default", ledger, every=every, now=now + every)
        both = await verify("default", ledger, now=now + every)
        await change(
            update(audit_seals).where(audit_seals.c.seq == 1).values(link="f" * 64)
        )
        rewritten = await verify("default", ledger, now=now + every)
    with override_settings(AUDIT={**namespaced, "PREVIOUS_ANCHORS": False}):
        new_key_only = await verify("default", ledger, now=now + every)

    assert before is not None
    assert after is not None
    assert (before.seq, after.seq) == (1, 2)
    assert (unchanged.intact, unchanged.anchors) == (True, 1)
    assert (both.intact, both.anchors) == (True, 2)
    assert rewritten.rewritten == [1]
    assert new_key_only.anchors == 1  # Only the head anchored under "shop".
    assert new_key_only.rewritten == []


async def test_other_projects_on_the_old_key_can_be_left_out(tables: None) -> None:
    """Without ``PREVIOUS_ANCHORS``, heads others wrote to the old key are ignored."""
    ledger = ledgers["audit"]
    now = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
    theirs = Anchor(seq=1, link="ab" * 32, anchored_at=now - timedelta(minutes=1))
    await ledger.append(anchor_key("default"), theirs.encode())
    await add(now - timedelta(seconds=1))
    await seal("default")
    namespaced = {"LEDGER": "audit", "NAMESPACE": "shop"}

    with override_settings(AUDIT=namespaced):
        await anchor("default", ledger, every=timedelta(hours=1), now=now)
        continued = await verify("default", ledger, now=now)
    with override_settings(AUDIT={**namespaced, "PREVIOUS_ANCHORS": False}):
        own = await verify("default", ledger, now=now)

    assert continued.rewritten == [1]  # Two links anchored for seal 1.
    assert own.intact


async def test_seals_left_unanchored_are_reported(tables: None) -> None:
    """Seals the newest anchor misses for over ``every`` + ``LOOKBACK``."""
    every = timedelta(hours=2)
    await add()
    await seal_all()
    for n in (1, 2):
        await add(datetime.now(UTC) + timedelta(seconds=n))
    await seal("default")  # Both in one batch, at one instant.
    sealed_at = (await seals())[-1]["sealed_at"]
    due = sealed_at + every + LOOKBACK
    tick = timedelta(microseconds=1)

    async def unanchored(now: datetime, ledger: Any = ledgers["audit"]) -> int:  # noqa: ANN401
        return (await verify("default", ledger, now=now, every=every)).unanchored

    assert await unanchored(due) == 0
    assert await unanchored(due + tick) == 2
    assert await unanchored(due + tick, ledger=None) == 0
    late = await verify("default", ledgers["audit"], now=due + tick, every=every)
    assert not late.intact
    await anchor("default", ledgers["audit"], every=timedelta(0))
    assert await unanchored(due + tick) == 0


async def test_unanchored_seals_are_due_after_anchor_every(tables: None) -> None:
    """By default, ``every`` is ``AUDIT.ANCHOR_EVERY``."""
    await add()
    await seal("default")
    sealed_at = (await seals())[-1]["sealed_at"]
    due = sealed_at + LOOKBACK + timedelta(hours=3)

    with override_settings(AUDIT={"LEDGER": "audit", "ANCHOR_EVERY": "PT3H"}):
        on_time = await verify("default", ledgers["audit"], now=due)
        late = await verify(
            "default", ledgers["audit"], now=due + timedelta(microseconds=1)
        )

    assert (on_time.unanchored, late.unanchored) == (0, 1)


async def test_without_a_ledger_rows_are_only_sealed(tables: None) -> None:
    """``AUDIT.LEDGER`` unset: nothing is anchored."""
    await add()
    with override_settings(AUDIT={}):
        assert await seal_all() == {"default": (1, None)}

    assert await ledgers["audit"].revision_count(anchor_key("default"), limit=5) == 0


async def test_a_run_goes_on_from_the_last_row_it_sealed(tables: None) -> None:
    """Late rows first, from the window; then the new ones, past each batch."""
    now = datetime.now(UTC)
    await add(now)
    await seal("default")
    late = await add(now - timedelta(minutes=1))
    new = [await add(now + timedelta(seconds=n)) for n in range(1, 5)]

    assert await seal("default", batch=2) == 5

    assert [each["entry_id"] for each in await seals()][1:] == [late, *new]
    assert (await verify("default")).intact


async def test_rows_of_one_instant_split_across_batches_are_all_sealed(
    tables: None,
) -> None:
    """A batch goes on past the last row's id too, not only its time."""
    now = datetime.now(UTC)
    async with connections.engine().begin() as connection:
        for n in (1, 2, 3):
            await connection.execute(
                insert(audit_log).values(
                    id=uuid.UUID(int=n),
                    occurred_at=now,
                    actor="system",
                    action="test.added",
                    outcome="success",
                    details={},
                )
            )

    assert await seal("default", batch=1) == 3
    assert [each["entry_id"] for each in await seals()] == [
        uuid.UUID(int=n) for n in (1, 2, 3)
    ]


def unsealed_gauge() -> float | None:
    """The backlog gauge of ``default``."""
    return REGISTRY.get_sample_value("audit_unsealed_rows", {"database": "default"})


async def test_a_run_seals_its_batches_at_most_and_counts_what_is_left(
    tables: None, caplog: pytest.LogCaptureFixture
) -> None:
    """The rest waits for the next run; a backlog past the threshold is logged."""
    start = datetime.now(UTC)
    for n in range(6):
        await add(start + timedelta(seconds=n))

    with (
        override_settings(AUDIT={"LEDGER": "audit", "SEAL_BACKLOG_WARNING": 1}),
        caplog.at_level(logging.WARNING, "domainkit.contrib.audit.seals"),
    ):
        assert await seal("default", batch=2, batches=2) == 4
        assert unsealed_gauge() == 2
        assert caplog.messages == [
            (
                "Audit sealing of 'default' is behind: 2 rows wait to be sealed. "
                "Raise AUDIT.SEAL_BATCHES or seal more often; until sealed, rows "
                "can be changed unnoticed"
            )
        ]
        caplog.clear()
        assert await seal("default", batch=2, batches=1) == 2  # 1 left: quiet.
        assert unsealed_gauge() == 0
        assert caplog.messages == []

    with override_settings(AUDIT={"LEDGER": "audit", "SEAL_BACKLOG_WARNING": 2}):
        for n in range(10, 13):
            await add(start + timedelta(seconds=n))
        assert await seal("default", batch=1, batches=1) == 1
        assert unsealed_gauge() == 2  # At the threshold: quiet.
        assert await seal("default", batch=3, batches=1) == 2  # Caught up.
        assert unsealed_gauge() == 0
    assert caplog.messages == []
    assert (await verify("default")).intact


async def test_sealing_takes_its_window_and_bound_from_settings(
    tables: None,
) -> None:
    """``SEAL_LATE_WINDOW`` and ``SEAL_BATCHES``, for ``seal`` and ``seal_all``."""
    now = datetime.now(UTC)
    await add(now)
    await seal("default")
    an_hour_late = await add(now - timedelta(hours=1))
    await add(now - timedelta(hours=4))  # Past the window, even from the late row.

    with override_settings(
        AUDIT={"SEAL_LATE_WINDOW": timedelta(hours=2), "SEAL_BATCHES": 1}
    ):
        assert await seal_all() == {"default": (1, None)}
        assert await seal("default", batch=1) == 0

    assert [each["entry_id"] for each in await seals()][-1] == an_hour_late


async def test_a_window_given_to_seal_wins_over_the_setting(tables: None) -> None:
    """``seal(late_window=...)`` looks as far back as it is told."""
    now = datetime.now(UTC)
    await add(now)
    await seal("default")
    await add(now - timedelta(hours=1))

    assert await seal("default") == 0
    assert await seal("default", late_window=timedelta(hours=2)) == 1


async def test_the_backlog_is_what_sealing_could_still_reach(tables: None) -> None:
    """Unsealed rows from the window on; none sealed yet: every one."""
    now = datetime.now(UTC)
    for n in range(3):
        await add(now + timedelta(seconds=n))
    assert await unsealed_backlog("default", WINDOW) == 3

    await seal("default")
    await add(now - 2 * WINDOW)  # Too late: verify's to report.
    await add(now + timedelta(seconds=9))
    assert await unsealed_backlog("default", WINDOW) == 1


async def test_the_backlog_counts_from_the_windows_first_instant(
    tables: None,
) -> None:
    """A row exactly ``late_window`` before the head's is still within reach."""
    head = datetime.now(UTC) - timedelta(minutes=1)  # In the past: the mark.
    await add(head)
    await seal("default")
    await add(head - WINDOW)
    await add(head - WINDOW - timedelta(microseconds=1))

    assert await unsealed_backlog("default", WINDOW) == 1


async def test_counting_a_backlog_stops_at_its_limit(
    tables: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past ``BACKLOG_COUNT_LIMIT`` rows the count reads the limit."""
    monkeypatch.setattr(seals_module, "BACKLOG_COUNT_LIMIT", 2)
    now = datetime.now(UTC)
    for n in range(3):
        await add(now + timedelta(seconds=n))
    assert await unsealed_backlog("default", WINDOW) == 2


@pytest.mark.parametrize(
    "bad",
    [
        {"SEAL_LATE_WINDOW": timedelta(0)},
        {"SEAL_LATE_WINDOW": timedelta(days=1, seconds=1)},
        {"SEAL_BATCHES": 0},
        {"SEAL_BATCHES": 10_001},
        {"SEAL_BACKLOG_WARNING": 0},
    ],
)
def test_sealing_limits_are_bounded(bad: dict[str, object]) -> None:
    """A window of nothing or past a day, and runs of nothing, are refused."""
    with pytest.raises(ValidationError):
        AuditSettings.model_validate(bad)


async def test_sealing_makes_the_anonymous_keys_of_today_and_tomorrow(
    tables: None,
) -> None:
    """At midnight the day's key exists: no request inserts it (N10)."""
    with freeze_time("2026-10-19T23:55:00Z") as frozen:
        await seal_all()
        async with connections.engine().connect() as connection:
            subjects = await connection.scalars(
                select(audit_keys.c.subject).order_by(audit_keys.c.subject)
            )
            assert list(subjects) == [
                "anonymous:2026-10-19",
                "anonymous:2026-10-20",
            ]

        frozen.move_to("2026-10-20T00:00:01Z")
        inserts: list[str] = []

        def count(conn: Any, cursor: Any, statement: str, *args: Any) -> None:  # noqa: ANN401
            if statement.startswith("INSERT INTO domainkit_audit_keys"):
                inserts.append(statement)

        engine = connections.engine().sync_engine
        event.listen(engine, "before_cursor_execute", count)
        try:
            with bind_request(RequestContext("r", "203.0.113.7", "POST", "/x")):
                await record("shop.ordered")
        finally:
            event.remove(engine, "before_cursor_execute", count)

        await seal_all()  # Keys there already are kept.

    assert inserts == []
    async with connections.engine().connect() as connection:
        keys = await connection.scalar(select(func.count()).select_from(audit_keys))
    assert keys == 3  # 19th, 20th, and now the 21st.


def test_seal_and_verify_commands(
    audited: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``auditseal`` reports what it sealed; ``auditverify`` names each problem."""
    shared = MemoryLedger()  # Commands close their ledgers; this one stays.
    monkeypatch.setattr(ledgers, "_factories", {"memory": lambda config: shared})
    reports = f"sqlite+aiosqlite:///{tmp_path / 'reports.sqlite3'}"
    databases = {**settings.DATABASES, "reports": {"URL": reports}}

    def run(*args: str) -> str:
        output = io.StringIO()
        call_command(*args, stdout=output)
        return output.getvalue()

    with override_settings(DATABASES=databases):

        async def prepare() -> list[uuid.UUID]:
            for alias in ("default", "reports"):
                async with connections.engine(alias).begin() as connection:
                    for table in (audit_log, audit_keys, audit_seals):
                        await connection.run_sync(table.create)
            start = datetime.now(UTC)
            entries = [await add(start + timedelta(seconds=n)) for n in range(4)]
            await connections.close_all()
            return entries

        first, second, third, _ = asyncio.run(prepare())

        assert run("auditseal") == "default: sealed 4 rows, anchored #4.\n"
        assert run("auditseal") == "default: sealed 0 rows.\n"
        assert run("auditseal", "--late") == "default: sealed 0 rows.\n"
        assert run("auditverify") == "default: 4 seals, 1 anchors: intact.\n"
        assert run("auditverify", "--database", "reports") == (
            "reports: 0 seals, 0 anchors: intact.\n"
        )
        with override_settings(AUDIT={}):
            assert run("auditverify") == "default: 4 seals, 0 anchors: intact.\n"

        async def tamper() -> None:
            for entry in (first, third):
                await change(
                    update(audit_log).where(audit_log.c.id == entry).values(actor="eve")
                )
            await change(delete(audit_log).where(audit_log.c.id == second))
            await change(
                update(audit_seals).where(audit_seals.c.seq == 2).values(link="f" * 64)
            )
            await change(delete(audit_seals).where(audit_seals.c.seq == 4))
            await add(datetime.now(UTC) - 2 * LOOKBACK)
            await connections.close_all()

        asyncio.run(tamper())
        output = io.StringIO()
        with pytest.raises(
            CommandError,
            match=r"^The audit trail failed verification: \['default'\]\.$",
        ):
            call_command("auditverify", "--database", "default", stdout=output)

    assert output.getvalue() == (
        "default: 3 seals, 1 anchors: NOT INTACT.\n"
        f"  changed rows: {first}, {third}\n"
        f"  missing rows: {second}\n"
        "  broken links at: 2, 3\n"
        "  rewritten anchors at: 4\n"
        "  unsealed rows: 1\n"
    )


def test_auditverify_counts_seals_left_unanchored() -> None:
    """Anchoring that stopped is a problem of its own."""
    assert _summary(Report("default", sealed=5, anchors=1, unanchored=2)) == (
        "default: 5 seals, 1 anchors: NOT INTACT.\n  seals left unanchored: 2"
    )


def test_auditverify_counts_rows_sealed_late() -> None:
    """Rows sealed late are no problem by themselves, but worth knowing."""
    assert _summary(Report("default", sealed=5, anchors=1, late=2)) == (
        "default: 5 seals, 1 anchors: intact.\n"
        "  rows sealed late (open to change for longer): 2"
    )


def test_auditseal_explains_its_late_option() -> None:
    """``--help`` says what ``--late`` seals, and what it costs."""
    usage = SealCommand().create_parser("manage.py", "auditseal").format_help()

    assert (
        "--late also seal rows that committed later than "
        "AUDIT.SEAL_LATE_WINDOW, flagged as sealed late (reads the whole trail)"
    ) in " ".join(usage.split())


@pytest.mark.parametrize("late", [False, True])
def test_auditseal_seals_late_rows_only_with_its_option(
    audited: str, late: bool
) -> None:
    """``auditseal --late`` reaches the rows a plain run leaves alone."""

    async def prepare() -> None:
        async with connections.engine().begin() as connection:
            for table in (audit_log, audit_keys, audit_seals):
                await connection.run_sync(table.create)
        now = datetime.now(UTC)
        await add(now)
        await seal("default")
        await add(now - 2 * WINDOW)
        await connections.close_all()

    asyncio.run(prepare())
    output = io.StringIO()
    with override_settings(AUDIT={}):  # Seal only: no anchors to count.
        call_command("auditseal", *(["--late"] if late else []), stdout=output)

    assert (
        output.getvalue() == f"default: sealed {int(late)} row{'' if late else 's'}.\n"
    )


def test_auditverify_explains_its_option() -> None:
    """``--help`` says what ``--database`` picks."""
    usage = VerifyCommand().create_parser("manage.py", "auditverify").format_help()

    assert "--database DATABASE the alias to check (default: every audited one)" in (
        " ".join(usage.split())
    )


def test_the_worker_seals_when_messaging_is_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ready() imports the sealing task only with messaging."""
    imported: list[str] = []
    monkeypatch.setattr(audit_apps, "import_module", imported.append)
    for installed in ([AUDIT, ALPHA], ["domainkit.contrib.messaging", AUDIT, ALPHA]):
        registry.clear()
        with override_settings(INSTALLED_DOMAINS=installed):
            setup()
    registry.clear()

    assert imported == ["domainkit.contrib.audit.tasks"]


async def test_the_sealing_task_runs_every_seal_every(tables: None) -> None:
    """The periodic task seals and anchors, every ``AUDIT.SEAL_EVERY``."""
    from domainkit.contrib.audit import tasks
    from domainkit.contrib.messaging.registry import get_task

    if get_task("domainkit.audit.seal") is None:  # Dropped after an earlier test.
        tasks = importlib.reload(tasks)
    await add()
    await tasks.seal_trail()

    spec = get_task("domainkit.audit.seal")
    assert spec is not None
    assert spec.every == timedelta(minutes=5)
    assert [each["seq"] for each in await seals()] == [1]


def test_the_seals_can_be_protected_like_the_trail() -> None:
    """``protect(op, audit_seals)`` installs the seals' own triggers."""
    protected, unprotected = statements(audit_seals)

    assert "domainkit_audit_seals_append_only()" in protected[0]
    assert all("ON domainkit_audit_seals" in each for each in protected[1:])
    assert all("domainkit_audit_seals_append_only" in each for each in unprotected)


@on_postgres
async def test_protected_seals_refuse_changes(tables: None) -> None:
    """On PostgreSQL the seals table refuses updates like the trail."""
    await add()
    await seal("default")
    async with connections.engine().begin() as connection:
        await connection.run_sync(lambda sync: protect(Migration(sync), audit_seals))

    with pytest.raises(Exception, match="domainkit_audit_seals is append-only"):
        await change(update(audit_seals).values(link="0" * 64))


@on_postgres
async def test_concurrent_sealers_take_turns(tables: None) -> None:
    """The advisory lock keeps two sealers from numbering the same rows."""
    start = datetime.now(UTC)
    for n in range(20):
        await add(start + timedelta(seconds=n))

    counts = await asyncio.gather(seal("default", batch=3), seal("default", batch=3))

    assert sum(counts) == 20
    assert [each["seq"] for each in await seals()] == list(range(1, 21))
    assert (await verify("default")).intact
