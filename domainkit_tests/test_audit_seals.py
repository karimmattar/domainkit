"""The audit trail's hash chain, its anchors on a ledger, and verification."""

import asyncio
import importlib
import io
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import delete, insert, select, update

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.contrib.audit import apps as audit_apps
from domainkit.contrib.audit import record
from domainkit.contrib.audit.protection import protect, statements
from domainkit.contrib.audit.seals import (
    GENESIS,
    LOOKBACK,
    Anchor,
    anchor,
    anchor_key,
    digest,
    link,
    seal,
    seal_all,
    verify,
)
from domainkit.contrib.audit.tables import audit_keys, audit_log, audit_seals
from domainkit.db import connections
from domainkit.ledger import MemoryLedger, ledgers
from domainkit.management import CommandError, call_command
from domainkit_tests.conftest import POSTGRES_VARIABLE
from domainkit_tests.test_postgres import Migration

pytestmark = pytest.mark.anyio

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


def test_digests_and_links_have_a_fixed_form() -> None:
    """Old rows must verify after upgrades: the canonical JSON never changes."""
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

    assert await seal("default", batch=2) == 5

    chain = await seals()
    assert [each["seq"] for each in chain] == [1, 2, 3, 4, 5]
    assert (await verify("default")).intact


async def test_late_rows_are_sealed_within_the_lookback_only(tables: None) -> None:
    """A row that commits after newer ones is sealed after them, if not too old."""
    now = datetime.now(UTC)
    await add(now)
    await seal("default")
    late = await add(now - LOOKBACK + timedelta(minutes=1))
    too_late = await add(now - LOOKBACK - timedelta(minutes=1))

    assert await seal("default") == 1

    assert [each["entry_id"] for each in await seals()][-1] == late
    report = await verify("default")
    assert (report.unsealed, report.intact) == (1, False)
    assert too_late not in [each["entry_id"] for each in await seals()]


async def test_an_intact_trail_verifies(tables: None) -> None:
    """Recorded rows, sealed and anchored, check out."""
    await record("report.viewed", target="r-1")
    await record("report.viewed", target="r-2")
    await seal_all()

    report = await verify("default", ledgers["audit"])

    assert report.intact
    assert (report.database, report.sealed, report.anchors) == ("default", 2, 1)
    assert report.changed == report.missing == report.broken == report.rewritten == []


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

    await change(update(audit_log).where(audit_log.c.id == first).values(actor="eve"))
    await change(delete(audit_seals))
    await seal("default")
    report = await verify("default", ledgers["audit"])

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


async def test_without_a_ledger_rows_are_only_sealed(tables: None) -> None:
    """``AUDIT.LEDGER`` unset: nothing is anchored."""
    await add()
    with override_settings(AUDIT={}):
        assert await seal_all() == {"default": (1, None)}

    assert await ledgers["audit"].revision_count(anchor_key("default"), limit=5) == 0


def test_seal_and_verify_commands(audited: str) -> None:
    """``auditseal`` reports what it sealed; ``auditverify`` fails on tampering."""

    async def prepare() -> uuid.UUID:
        async with connections.engine().begin() as connection:
            for table in (audit_log, audit_keys, audit_seals):
                await connection.run_sync(table.create)
        entry = await add()
        await connections.close_all()
        return entry

    entry = asyncio.run(prepare())

    def run(*args: str) -> str:
        output = io.StringIO()
        call_command(*args, stdout=output)
        return output.getvalue()

    # Each command closes its resources; a memory ledger starts empty in each.
    assert run("auditseal") == "default: sealed 1 row, anchored #1.\n"
    assert run("auditverify") == "default: 1 seals, 0 anchors: intact.\n"
    with override_settings(AUDIT={}):
        assert run("auditseal") == "default: sealed 0 rows.\n"

    async def tamper() -> None:
        await change(
            update(audit_log).where(audit_log.c.id == entry).values(actor="eve")
        )
        await add(datetime.now(UTC) - 2 * LOOKBACK)
        await connections.close_all()

    asyncio.run(tamper())
    output = io.StringIO()
    with pytest.raises(
        CommandError, match=r"^The audit trail failed verification: \['default'\]\.$"
    ):
        call_command("auditverify", "--database", "default", stdout=output)
    assert output.getvalue() == (
        "default: 1 seals, 0 anchors: NOT INTACT.\n"
        f"  changed rows: {entry}\n"
        "  unsealed rows: 1\n"
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
