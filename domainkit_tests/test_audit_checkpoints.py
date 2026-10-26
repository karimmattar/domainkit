"""Retention that keeps the chain: pruning below signed checkpoints, and exports (N6).

A trail pruned below a checkpoint still verifies, from the checkpoint; a
trail tampered with after a prune, or a checkpoint forged to cover a
deletion, still fails. Exports of signed checkpoints verify offline with
the public key alone (DATA-05).
"""

import asyncio
import base64
import io
import json
import os
import stat
import sys
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest
from cryptography.hazmat.primitives.asymmetric.ec import SECP256R1, generate_private_key
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import DBAPIError

from domainkit import setup
from domainkit.apps import registry
from domainkit.clock import freeze_time
from domainkit.conf import override_settings
from domainkit.contrib.audit import pruning
from domainkit.contrib.audit.checkpoints import (
    Checkpoint,
    CheckpointInvalid,
    check_export,
    key_id,
    load_public_key,
    signing_key,
)
from domainkit.contrib.audit.protection import protect
from domainkit.contrib.audit.pruning import ExportRefused, PruneRefused, export, prune
from domainkit.contrib.audit.seals import (
    Anchor,
    anchor,
    anchor_key,
    first_anchored,
    pruned_key,
    pruned_record,
    seal,
    verify,
)
from domainkit.contrib.audit.tables import (
    audit_checkpoints,
    audit_keys,
    audit_log,
    audit_seals,
)
from domainkit.db import connections
from domainkit.exceptions import ImproperlyConfigured
from domainkit.ledger import Ledger, ledgers
from domainkit.management import CommandError, call_command
from domainkit_tests.conftest import ALPHA, POSTGRES_VARIABLE
from domainkit_tests.test_audit_seals import add, change, seals
from domainkit_tests.test_postgres import Migration

pytestmark = pytest.mark.anyio

AUDIT: Final = "domainkit.contrib.audit"
T0: Final = datetime(2026, 1, 5, 12, tzinfo=UTC)
T1: Final = T0 + timedelta(days=40)
CUT: Final = T0 + timedelta(days=10)
on_postgres = pytest.mark.skipif(
    not os.environ.get(POSTGRES_VARIABLE),
    reason=f"set {POSTGRES_VARIABLE} to a PostgreSQL server",
)


def private_pem(key: Any) -> str:  # noqa: ANN401 - any private key.
    """``key`` in PEM, unencrypted."""
    pem: bytes = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    return pem.decode()


KEY: Final = Ed25519PrivateKey.generate()
OTHER: Final = Ed25519PrivateKey.generate()
PUBLIC_PEM: Final = KEY.public_key().public_bytes(
    Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
)


def keys() -> dict[str, str]:
    """``AUDIT`` signing with ``KEY``, verifying with its public half."""
    return {
        "CHECKPOINT_KEY": private_pem(KEY),
        "CHECKPOINT_PUBLIC_KEY": PUBLIC_PEM.decode(),
    }


def settings_with(**audit: object) -> dict[str, Any]:
    """``AUDIT`` anchoring on the memory ledger, signing with ``KEY``."""
    return {"LEDGER": "audit", **keys(), **audit}


@pytest.fixture
def audited(database_url: str) -> Iterator[str]:
    """The audit app, a memory ledger, and the checkpoint key."""
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[AUDIT, ALPHA],
        DATABASES={"default": {"URL": database_url}},
        LEDGERS={"audit": {"URL": "memory://"}},
        AUDIT=settings_with(),
    ):
        setup()
        yield database_url
    registry.clear()


async def fill(ledger: Ledger | None) -> None:
    """Five rows sealed at ``T0``, three more at ``T1``; each head anchored."""
    async with connections.engine().begin() as connection:
        for table in (audit_log, audit_keys, audit_seals, audit_checkpoints):
            await connection.run_sync(table.create)
    for moment, count in ((T0, 5), (T1, 3)):
        with freeze_time(moment):
            for n in range(count):
                await add(moment + timedelta(seconds=n))
            await seal("default")
            if ledger is not None:
                await anchor("default", ledger, every=timedelta(hours=1))


@pytest.fixture
async def trail(audited: str) -> AsyncIterator[Ledger]:
    """``fill``, anchored on the memory ledger."""
    ledger = ledgers["audit"]
    await fill(ledger)
    yield ledger
    await connections.close_all()
    await ledgers.close_all()


@pytest.fixture
async def unanchored(database_url: str) -> AsyncIterator[None]:
    """``fill``, without a ledger: commands, which close ledgers, run on it."""
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[AUDIT, ALPHA],
        DATABASES={"default": {"URL": database_url}},
        AUDIT=keys(),
    ):
        setup()
        await fill(None)
        yield
        await connections.close_all()
    registry.clear()


async def checkpoints() -> list[Any]:
    """The stored checkpoints, oldest first."""
    async with connections.engine().connect() as connection:
        return list(
            (
                await connection.execute(
                    select(audit_checkpoints).order_by(audit_checkpoints.c.seq)
                )
            ).all()
        )


async def test_a_pruned_chain_still_verifies(trail: Ledger) -> None:
    """Rows sealed before the cut go; ``verify`` starts from the checkpoint."""
    with freeze_time(T1 + timedelta(days=1)):
        pruned = await prune("default", before=CUT, ledger=trail)
    report = await verify("default", trail, now=T1)
    left = await seals()
    (stored,) = await checkpoints()

    assert pruned.checkpoint is not None
    assert (pruned.checkpoint.seq, pruned.rows) == (5, 5)
    assert [each["seq"] for each in left] == [6, 7, 8]
    assert report.intact, report
    assert (report.sealed, report.pruned, report.anchors) == (3, 5, 2)
    assert stored.seq == 5
    assert Checkpoint.parse(stored.document) == pruned.checkpoint
    assert pruned.checkpoint.previous is None
    assert pruned.checkpoint.key_id == key_id(KEY.public_key())
    async with connections.engine().connect() as connection:
        audited = (
            await connection.execute(
                select(audit_log).where(audit_log.c.action == "audit.pruned")
            )
        ).one()
    assert (audited.target, audited.details["seq"], audited.details["rows"]) == (
        "default",
        5,
        5,
    )


async def test_a_second_prune_follows_the_first(trail: Ledger) -> None:
    """Checkpoints chain: the next names the one before it."""
    with freeze_time(T1):
        first = await prune("default", before=CUT, ledger=trail)
        for n in range(2):
            await add(T1 + timedelta(minutes=n))
        await seal("default")
        await anchor("default", trail, every=timedelta(0))
    with freeze_time(T1 + timedelta(days=50)):
        second = await prune("default", before=T1 + timedelta(days=1), ledger=trail)
    report = await verify("default", trail, now=T1 + timedelta(days=50))

    assert first.checkpoint is not None
    assert second.checkpoint is not None
    assert second.checkpoint.previous == (5, first.checkpoint.link)
    assert (second.checkpoint.seq, second.rows) == (8, 3)
    assert report.intact, report
    assert report.pruned == 8


async def test_nothing_older_prunes_nothing(trail: Ledger) -> None:
    """A cut before every seal, or at the last checkpoint, deletes nothing."""
    early = await prune("default", before=T0 - timedelta(days=1), ledger=trail)
    await prune("default", before=CUT, ledger=trail)
    again = await prune("default", before=CUT, ledger=trail)

    assert (early.checkpoint, early.rows) == (None, 0)
    assert (again.checkpoint, again.rows) == (None, 0)
    assert len(await checkpoints()) == 1


async def test_the_newest_seal_is_never_pruned(trail: Ledger) -> None:
    """Sealing goes on from the head, so a cut stops one short of it."""
    pruned = await prune("default", before=T1 + timedelta(days=1), ledger=None)

    assert pruned.checkpoint is not None
    assert pruned.checkpoint.seq == 7
    assert [each["seq"] for each in await seals()] == [8]
    assert (await verify("default")).intact
    unrecorded = await verify("default", trail)  # Pruned without it.
    assert unrecorded.forged == [7]


async def test_with_a_ledger_the_cut_is_an_anchored_seal(trail: Ledger) -> None:
    """The ledger vouches for the cut: the newest anchored seal before it.

    Seals 6 and 7 were sealed before the cut but anchored only with 8, the
    head, which is never pruned: the cut falls back to 5, the anchor before.
    """
    pruned = await prune("default", before=T1 + timedelta(days=1), ledger=trail)

    assert pruned.checkpoint is not None
    assert pruned.checkpoint.seq == 5


@pytest.mark.parametrize(
    "tamper",
    ["change a row", "remove a seal", "remove the checkpoint", "edit the checkpoint"],
)
async def test_a_tampered_pruned_chain_still_fails(trail: Ledger, tamper: str) -> None:
    """After a prune, any change to what is left, or to the checkpoint, is found."""
    await prune("default", before=CUT, ledger=trail)
    (kept, *_) = await seals()
    if tamper == "change a row":
        await change(
            update(audit_log)
            .where(audit_log.c.id == kept["entry_id"])
            .values(actor="mallory")
        )
    elif tamper == "remove a seal":
        await change(delete(audit_seals).where(audit_seals.c.seq == 6))
    elif tamper == "remove the checkpoint":
        await change(delete(audit_checkpoints))
    else:  # Moved a day: the signature no longer covers it.
        (stored,) = await checkpoints()
        moved = stored.document.replace(
            '"sealed_at":"2026-01-05', '"sealed_at":"2026-01-06'
        )
        assert moved != stored.document
        await change(update(audit_checkpoints).values(document=moved))

    report = await verify("default", trail, now=T1)

    assert not report.intact
    found = {
        "change a row": report.changed,
        "remove a seal": report.broken,
        "remove the checkpoint": report.broken,
        "edit the checkpoint": report.forged,
    }[tamper]
    assert found


async def test_a_forged_checkpoint_covers_no_deletion(trail: Ledger) -> None:
    """Rows deleted under a checkpoint signed by another key are found.

    Whoever can rewrite the tables can delete rows and add a checkpoint,
    but not sign it: it starts nothing, and the gap is a broken link.
    """
    forged = Checkpoint(
        database="default",
        namespace=None,
        seq=5,
        link=(await seals())[4]["link"],
        sealed_at=T0,
        made_at=T1,
        previous=None,
        key_id=key_id(OTHER.public_key()),
    )
    async with connections.engine().begin() as connection:
        await connection.execute(
            insert(audit_checkpoints).values(
                seq=5,
                document=forged.document(),
                signature=OTHER.sign(forged.document().encode()),
                made_at=T1,
            )
        )
        ids = select(audit_seals.c.entry_id).where(audit_seals.c.seq <= 5)
        await connection.execute(delete(audit_log).where(audit_log.c.id.in_(ids)))
        await connection.execute(delete(audit_seals).where(audit_seals.c.seq <= 5))

    report = await verify("default", trail, now=T1)

    assert report.forged == [5]
    assert report.broken == [6]
    assert not report.intact
    with pytest.raises(PruneRefused, match=r"at seals \[5\] do not verify"):
        await prune("default", before=T1 + timedelta(days=1))


async def test_a_checkpoint_of_another_chain_starts_nothing(audited: str) -> None:
    """Signed for another database, or another namespace: it does not count here."""
    async with connections.engine().begin() as connection:
        for table in (audit_log, audit_keys, audit_seals, audit_checkpoints):
            await connection.run_sync(table.create)
    for database, namespace in (("other", None), ("default", "elsewhere")):
        theirs = Checkpoint(
            database, namespace, 3, "a" * 64, T0, T1, None, key_id(KEY.public_key())
        )
        async with connections.engine().begin() as connection:
            await connection.execute(delete(audit_checkpoints))
            await connection.execute(
                insert(audit_checkpoints).values(
                    seq=3,
                    document=theirs.document(),
                    signature=KEY.sign(theirs.document().encode()),
                    made_at=T1,
                )
            )
        assert (await verify("default")).forged == [3]
    await connections.close_all()


async def test_a_checkpoint_left_inside_the_chain_must_match_it(trail: Ledger) -> None:
    """A checkpoint above seals still there (a run cut short): they must agree.

    Checked without the ledger, which does not record this one at all.
    """
    pruned = await prune("default", before=CUT, ledger=trail)
    assert pruned.checkpoint is not None
    wrong = Checkpoint(
        "default",
        None,
        7,
        "b" * 64,
        T0,
        T1,
        (5, pruned.checkpoint.link),
        key_id(KEY.public_key()),
    )
    async with connections.engine().begin() as connection:
        await connection.execute(
            insert(audit_checkpoints).values(
                seq=7,
                document=wrong.document(),
                signature=KEY.sign(wrong.document().encode()),
                made_at=T1,
            )
        )

    report = await verify("default", now=T1)

    assert report.broken == [7]


async def test_a_prune_cut_short_verifies_and_finishes_later(
    trail: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting newest first leaves a gap the checkpoint ends; a rerun finishes."""
    monkeypatch.setattr(pruning, "BATCH", 2)
    calls = 0
    real = delete

    def failing(table: Any) -> Any:  # noqa: ANN401 - SQLAlchemy's delete.
        nonlocal calls
        calls += 1
        if calls > 2:  # The second batch's first statement.
            raise RuntimeError("the process was killed")
        return real(table)

    monkeypatch.setattr(pruning, "delete", failing)
    with pytest.raises(RuntimeError, match="killed"):
        await prune("default", before=CUT, ledger=trail)
    halfway = await verify("default", trail, now=T1)
    monkeypatch.setattr(pruning, "delete", real)
    finished = await prune("default", before=CUT, ledger=trail)

    assert [each["seq"] for each in await seals()] == [6, 7, 8]
    assert halfway.intact, halfway
    assert halfway.pruned == 2
    assert finished.checkpoint is not None
    assert (finished.checkpoint.seq, finished.rows) == (5, 3)
    async with connections.engine().begin() as connection:
        left = (await connection.execute(select(audit_log.c.id))).scalars().all()
    assert len(left) == 3 + 1  # The rows sealed after, and the rerun's record.


async def test_a_prune_resumed_finishes_what_was_left(
    trail: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a run cut short, the next cut deletes from its own checkpoint down."""
    monkeypatch.setattr(pruning, "BATCH", 2)
    real = pruning._delete
    seen: list[tuple[int, int]] = []

    async def stop_early(database: str, lowest: int, cut: int) -> int:
        seen.append((lowest, cut))
        return 0

    monkeypatch.setattr(pruning, "_delete", stop_early)
    await prune("default", before=CUT, ledger=trail)
    monkeypatch.setattr(pruning, "_delete", real)
    halfway = await verify("default", trail, now=T1)

    assert seen == [(0, 5)]
    assert halfway.intact
    assert halfway.pruned == 0  # Nothing deleted: the checkpoint matches seal 5.


async def test_a_chain_that_does_not_verify_is_never_pruned(trail: Ledger) -> None:
    """Pruning would hide the change: refused, and nothing is stored or deleted."""
    (first, *_) = await seals()
    await change(
        update(audit_log).where(audit_log.c.id == first["entry_id"]).values(actor="x")
    )

    with pytest.raises(PruneRefused) as refused:
        await prune("default", before=CUT, ledger=trail)
    assert str(refused.value) == (
        "The trail of 'default' does not verify up to the cut ({'changed rows': 1}); "
        "pruning it would hide that. Nothing was pruned: run auditverify."
    )
    assert len(await seals()) == 8
    assert await checkpoints() == []


async def test_owners_may_disable_the_triggers_or_prune_is_refused(
    trail: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal to disable the triggers names the setting; other errors pass."""

    class Refusal(Exception):
        sqlstate = "42501"

    def refused(table: Any) -> Any:  # noqa: ANN401 - SQLAlchemy's delete.
        raise DBAPIError("DELETE", None, Refusal("must be owner"))

    monkeypatch.setattr(pruning, "delete", refused)
    with pytest.raises(
        PruneRefused, match=r"DATABASES\['default'\]\['MIGRATION_URL'\]"
    ):
        await prune("default", before=CUT, ledger=trail)

    def broken(table: Any) -> Any:  # noqa: ANN401 - SQLAlchemy's delete.
        raise DBAPIError("DELETE", None, Exception("disk full"))

    monkeypatch.setattr(pruning, "delete", broken)
    with pytest.raises(DBAPIError, match="disk full"):
        await prune("default", before=T1 + timedelta(days=1), ledger=None)


@on_postgres
async def test_protected_tables_are_pruned_and_stay_protected(trail: Ledger) -> None:
    """The ``protect()`` triggers are off for each batch only, as the owner."""
    from sqlalchemy import text

    def protect_both(sync: Any) -> None:  # noqa: ANN401 - a sync connection.
        for table in (audit_log, audit_seals):
            protect(Migration(sync), table)

    async with connections.engine().begin() as connection:
        await connection.run_sync(protect_both)

    pruned = await prune("default", before=CUT, ledger=trail)

    assert pruned.rows == 5
    with pytest.raises(Exception, match="domainkit_audit_log is append-only"):
        await change(delete(audit_log))
    async with connections.engine().connect() as connection:
        states = (
            (
                await connection.execute(
                    text(
                        "SELECT tgenabled::text FROM pg_trigger WHERE tgname IN "
                        "('domainkit_audit_log_append_only', "
                        "'domainkit_audit_seals_append_only')"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert sorted(states) == ["O", "O"]
    assert (await verify("default", trail, now=T1)).intact


async def test_exports_verify_offline_with_the_public_key(
    trail: Ledger, tmp_path: Path
) -> None:
    """Stored checkpoints and one of the head, each signed; any change fails."""
    await prune("default", before=CUT, ledger=trail)
    with freeze_time(T1 + timedelta(days=1)):
        document = await export(trail)
    data = json.dumps(document).encode()

    checked = check_export(data, PUBLIC_PEM)

    assert [c.seq for c in checked["default"]] == [5, 8]
    assert checked["default"][1].previous == (5, checked["default"][0].link)
    assert document["key_id"] == key_id(KEY.public_key())
    assert document["public_key"] == PUBLIC_PEM.decode()
    other_pem = OTHER.public_key().public_bytes(
        Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
    )
    with pytest.raises(CheckpointInvalid, match="signature does not verify"):
        check_export(data, other_pem)
    head = document["databases"]["default"]["checkpoints"][1]
    for changed in (
        head["document"].replace('"seq":8', '"seq":9'),
        head["document"] + " ",
    ):
        tampered = json.loads(data)
        tampered["databases"]["default"]["checkpoints"][1]["document"] = changed
        with pytest.raises(CheckpointInvalid):
            check_export(json.dumps(tampered).encode(), PUBLIC_PEM)
    dropped = json.loads(data)
    del dropped["databases"]["default"]["checkpoints"][0]
    with pytest.raises(CheckpointInvalid) as unlinked:
        check_export(json.dumps(dropped).encode(), PUBLIC_PEM)
    assert str(unlinked.value) == (
        "Checkpoint 8 of 'default' does not follow the one before it."
    )
    moved = json.loads(data)
    moved["databases"]["elsewhere"] = moved["databases"].pop("default")
    with pytest.raises(CheckpointInvalid, match="another chain's"):
        check_export(json.dumps(moved).encode(), PUBLIC_PEM)
    garbled = json.loads(data)
    garbled["databases"]["default"]["checkpoints"][0]["signature"] = "!" * 88
    for broken, message in (
        (b"[]", "Not an export."),
        (b"{}", "Not an export."),
        (json.dumps(garbled).encode(), "Not an export."),
        (
            json.dumps({**document, "format": "x"}).encode(),
            "Not an export of format domainkit.audit.export/1.",
        ),
    ):
        with pytest.raises(CheckpointInvalid) as refused:
            check_export(broken, PUBLIC_PEM)
        assert str(refused.value) == message


async def test_a_trail_that_does_not_verify_is_not_exported(trail: Ledger) -> None:
    """A checkpoint of it would vouch for what is wrong."""
    await change(delete(audit_seals).where(audit_seals.c.seq == 3))

    with pytest.raises(ExportRefused, match="does not verify"):
        await export(trail)


async def test_an_empty_trail_exports_no_checkpoint(audited: str) -> None:
    """Nothing sealed: nothing to vouch for."""
    async with connections.engine().begin() as connection:
        for table in (audit_log, audit_keys, audit_seals, audit_checkpoints):
            await connection.run_sync(table.create)

    document = await export()
    await connections.close_all()

    assert document["databases"] == {"default": {"checkpoints": []}}
    assert check_export(json.dumps(document).encode(), PUBLIC_PEM) == {"default": []}


def command(*args: str) -> str:
    """Run a command (on a thread of its own: it runs its own loop); its output."""
    out = io.StringIO()
    call_command(*args, stdout=out)
    return out.getvalue()


@pytest.mark.usefixtures("unanchored")
async def test_the_commands_prune_export_and_check(tmp_path: Path) -> None:
    """``auditprune``, ``auditverify --export``, and ``--check-export``.

    The prunes are audited, so the trail is sealed again before the export.
    """
    target = tmp_path / "checkpoints.json"
    public = tmp_path / "public.pem"
    public.write_bytes(PUBLIC_PEM)

    def run(*args: str) -> str:
        return command(*args)

    with freeze_time(T1 + timedelta(days=1)):
        pruned = await asyncio.to_thread(run, "auditprune", "--keep-days", "30")
        again = await asyncio.to_thread(
            run, "auditprune", "--keep-days", "30", "--database", "default"
        )
        sealed = await asyncio.to_thread(run, "auditseal")
        exported = await asyncio.to_thread(run, "auditverify", "--export", str(target))
    checked = await asyncio.to_thread(
        run, "auditverify", "--check-export", str(target), "--public-key", str(public)
    )

    assert pruned == "default: pruned 5 rows below seal #5.\n"
    assert again == "default: nothing to prune.\n"
    assert sealed == "default: sealed 3 rows.\n"
    assert exported.splitlines() == [
        "default: 6 seals, 0 anchors: intact.",
        "  seals pruned below checkpoints: 5",
        f"Wrote 2 signed checkpoints to {target} (key {key_id(KEY.public_key())}).",
    ]
    assert checked == "default: 2 checkpoints verified, newest at seal #11.\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    async with connections.engine().connect() as connection:
        actions = (
            await connection.execute(
                select(audit_log.c.action, audit_log.c.details).where(
                    audit_log.c.action.in_(["operator.auditprune", "audit.pruned"])
                )
            )
        ).all()
    assert sorted((action, details.get("rows")) for action, details in actions) == [
        ("audit.pruned", 5),
        ("operator.auditprune", 0),
        ("operator.auditprune", 5),
    ]


@pytest.mark.usefixtures("unanchored")
async def test_the_commands_refuse_what_they_cannot_do(tmp_path: Path) -> None:
    """Existing files, a missing key, a bad export, and a bad option are errors."""
    target = tmp_path / "exists.json"
    target.write_text("{}")
    public = tmp_path / "public.pem"
    public.write_bytes(PUBLIC_PEM)
    refusals = [
        (("auditverify", "--export", str(target)), "exists; name a new file"),
        (("auditverify", "--check-export", str(target)), "needs --public-key"),
        (
            ("auditverify", "--export", "x", "--database", "default"),
            "covers every audited database",
        ),
        (
            ("auditverify", "--check-export", str(target), "--public-key", str(public)),
            "does not verify: Not an export",
        ),
        (("auditprune", "--keep-days", "0"), "--keep-days is at least 1"),
    ]
    with freeze_time(T1 + timedelta(days=1)):
        await asyncio.to_thread(command, "auditseal")
        for args, message in refusals:
            with pytest.raises(CommandError, match=message):
                await asyncio.to_thread(command, *args)


@pytest.mark.usefixtures("unanchored")
async def test_refusals_of_the_library_are_the_commands_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A prune or an export the library refuses is the command's error."""
    from domainkit.contrib.audit.management.commands import auditprune, auditverify

    async def refuse_prune(*args: object, **options: object) -> Any:  # noqa: ANN401
        raise PruneRefused("no")

    async def refuse_export(*args: object, **options: object) -> Any:  # noqa: ANN401
        raise ExportRefused("not exported")

    monkeypatch.setattr(auditprune, "prune", refuse_prune)
    monkeypatch.setattr(auditverify, "export", refuse_export)
    with freeze_time(T1 + timedelta(days=1)):
        with pytest.raises(CommandError, match=r"^no$"):
            await asyncio.to_thread(command, "auditprune", "--keep-days", "1")
        await asyncio.to_thread(command, "auditseal")
        with pytest.raises(CommandError, match="not exported"):
            await asyncio.to_thread(
                command, "auditverify", "--export", str(tmp_path / "x.json")
            )


def test_checkpoint_documents_are_strict() -> None:
    """Only canonical documents of the format, with every field well formed."""
    good = Checkpoint("default", "ns", 5, "a" * 64, T0, T1, (2, "b" * 64), "k")
    assert Checkpoint.parse(good.document()) == good
    document = json.loads(good.document())
    bad = [
        good.document().replace(",", ", ", 1),
        "[]",
        "not json",
        json.dumps(
            {**document, "format": "domainkit.audit.checkpoint/2"},
            sort_keys=True,
            separators=(",", ":"),
        ),
        json.dumps({**document, "extra": 1}, sort_keys=True, separators=(",", ":")),
        json.dumps({**document, "seq": 0}, sort_keys=True, separators=(",", ":")),
        json.dumps({**document, "seq": True}, sort_keys=True, separators=(",", ":")),
        json.dumps(
            {**document, "link": "A" * 64}, sort_keys=True, separators=(",", ":")
        ),
        json.dumps({**document, "link": 7}, sort_keys=True, separators=(",", ":")),
        json.dumps(
            {**document, "previous": {"seq": 5, "link": "b" * 64}},
            sort_keys=True,
            separators=(",", ":"),
        ),
    ]
    for document_text in bad:
        with pytest.raises(CheckpointInvalid):
            Checkpoint.parse(document_text)
    first = Checkpoint("default", None, 1, "a" * 64, T0, T1, None, "k")
    assert Checkpoint.parse(first.document()) == first


@pytest.mark.parametrize(
    ("kind", "message"),
    [
        ("text", "not an Ed25519 public key in PEM"),
        ("ecdsa", "not an Ed25519 public key in PEM"),
    ],
)
def test_a_key_that_cannot_verify_refuses_to_start(kind: str, message: str) -> None:
    """``AUDIT.CHECKPOINT_PUBLIC_KEY`` is checked when the application starts."""
    value = (
        "not a key"
        if kind == "text"
        else generate_private_key(SECP256R1())
        .public_key()
        .public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    registry.clear()
    try:
        with (
            override_settings(
                INSTALLED_DOMAINS=[AUDIT, ALPHA],
                AUDIT={"CHECKPOINT_PUBLIC_KEY": value},
            ),
            pytest.raises(ImproperlyConfigured, match=message),
        ):
            setup()
    finally:
        registry.clear()


@pytest.mark.parametrize(
    ("kind", "message"),
    [
        ("text", "not a private key in PEM without a password"),
        ("ecdsa", "must be an Ed25519 key"),
        ("unpaired", "must be the public half of AUDIT.CHECKPOINT_KEY"),
    ],
)
def test_a_key_that_cannot_sign_is_refused_when_signing(
    kind: str, message: str
) -> None:
    """The private key is read by the commands that sign, not at startup (SEC-10b-3).

    So a project that keeps it out of the application's settings runs,
    and one that sets it wrong starts too, but cannot sign.
    """
    value = {
        "text": "not a key",
        "ecdsa": private_pem(generate_private_key(SECP256R1())),
        "unpaired": private_pem(OTHER),
    }[kind]
    registry.clear()
    try:
        with override_settings(
            INSTALLED_DOMAINS=[AUDIT, ALPHA],
            AUDIT={
                "CHECKPOINT_KEY": value,
                "CHECKPOINT_PUBLIC_KEY": PUBLIC_PEM.decode(),
            },
        ):
            setup()
            with pytest.raises(ImproperlyConfigured, match=message):
                signing_key()
    finally:
        registry.clear()


def test_signing_without_the_public_key_is_refused() -> None:
    """What the key signs is verified with ``CHECKPOINT_PUBLIC_KEY``: it must be set."""
    registry.clear()
    try:
        with override_settings(
            INSTALLED_DOMAINS=[AUDIT, ALPHA], AUDIT={"CHECKPOINT_KEY": private_pem(KEY)}
        ):
            setup()
            with pytest.raises(ImproperlyConfigured, match="public half"):
                signing_key()
    finally:
        registry.clear()


def test_without_a_key_nothing_is_signed() -> None:
    """Pruning and exports need the key; the message says how to make one."""
    registry.clear()
    try:
        with override_settings(INSTALLED_DOMAINS=[AUDIT, ALPHA]):
            setup()
            with pytest.raises(
                ImproperlyConfigured, match="openssl genpkey -algorithm ed25519"
            ):
                signing_key()
    finally:
        registry.clear()


@pytest.mark.parametrize("module", ["ed25519", "serialization"])
def test_signing_needs_cryptography(
    monkeypatch: pytest.MonkeyPatch, module: str
) -> None:
    """Without cryptography, a key set is refused with the extra to install."""
    monkeypatch.setitem(
        sys.modules,
        {
            "ed25519": "cryptography.hazmat.primitives.asymmetric.ed25519",
            "serialization": "cryptography.hazmat.primitives.serialization",
        }[module],
        None,
    )
    registry.clear()
    try:
        with (
            override_settings(
                INSTALLED_DOMAINS=[AUDIT, ALPHA],
                AUDIT={"CHECKPOINT_PUBLIC_KEY": PUBLIC_PEM.decode()},
            ),
            pytest.raises(
                ImproperlyConfigured, match=r'pip install "domainkit\[audit\]"'
            ),
        ):
            setup()
    finally:
        registry.clear()


def test_public_keys_must_be_ed25519() -> None:
    """A verifier pins an Ed25519 public key in PEM; anything else is refused."""
    other = (
        generate_private_key(SECP256R1())
        .public_key()
        .public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    )

    assert key_id(load_public_key(PUBLIC_PEM)) == key_id(KEY.public_key())
    with pytest.raises(CheckpointInvalid) as unreadable:
        load_public_key(b"not a key")
    with pytest.raises(CheckpointInvalid) as wrong:
        load_public_key(other)
    assert str(unreadable.value) == "Not a public key in PEM."
    assert str(wrong.value) == "Checkpoints are signed with Ed25519 keys."


async def test_a_checkpoint_naming_another_key_is_refused() -> None:
    """Signed with the key, but naming another: refused."""
    from domainkit.contrib.audit.checkpoints import verified

    named = Checkpoint("default", None, 3, "a" * 64, T0, T1, None, "0" * 16)

    with pytest.raises(CheckpointInvalid, match="names another key"):
        verified(
            named.document(), KEY.sign(named.document().encode()), KEY.public_key()
        )


async def test_without_the_key_no_checkpoint_verifies(trail: Ledger) -> None:
    """A pruned trail checked where ``CHECKPOINT_KEY`` is unset does not verify."""
    await prune("default", before=CUT, ledger=trail)

    with override_settings(AUDIT={"LEDGER": "audit"}):
        report = await verify("default", trail, now=T1)

    assert report.forged == [5]
    assert report.broken == [6]
    assert not report.intact


async def test_exports_name_their_namespace(audited: str) -> None:
    """With ``AUDIT.NAMESPACE``, checkpoints and the export name it; it must agree."""
    with override_settings(AUDIT=settings_with(NAMESPACE="acme")):
        await fill(ledgers["audit"])
        document = await export()
    await connections.close_all()
    await ledgers.close_all()
    checked = check_export(json.dumps(document).encode(), PUBLIC_PEM)
    moved = {**document, "namespace": "other"}

    assert document["namespace"] == "acme"
    assert [c.namespace for c in checked["default"]] == ["acme"]
    with pytest.raises(CheckpointInvalid, match="another chain's"):
        check_export(json.dumps(moved).encode(), PUBLIC_PEM)


async def test_a_rewritten_anchor_below_the_cut_refuses_the_prune(
    trail: Ledger,
) -> None:
    """The ledger is checked too: a head anchored with another link stops it."""
    from domainkit.contrib.audit.seals import Anchor, anchor_key

    entry = await trail.read(anchor_key("default"))
    await trail.append(
        anchor_key("default"),
        Anchor(3, "f" * 64, T0).encode(),
        unchanged_since=entry.transaction,
    )

    with pytest.raises(PruneRefused, match=r"\{'rewritten anchors': 1\}"):
        await prune("default", before=CUT, ledger=trail)


async def test_a_cut_just_after_the_first_seals_still_verifies(trail: Ledger) -> None:
    """A second after the first batch was sealed: that batch goes, and verifies."""
    first = (await seals())[0]

    pruned = await prune("default", before=first["sealed_at"] + timedelta(0, 1))
    report = await verify("default", now=T1)

    assert pruned.checkpoint is not None
    assert pruned.checkpoint.seq == 5  # Every seal at T0 was sealed at once.
    assert report.intact, report


async def test_a_seal_sealed_at_the_cut_is_kept(trail: Ledger) -> None:
    """``before`` is exclusive: seals sealed at that moment stay."""
    assert (await prune("default", before=T0, ledger=None)).checkpoint is None


async def test_pruned_checkpoints_name_the_namespace(audited: str) -> None:
    """Under ``AUDIT.NAMESPACE`` a checkpoint names it, and verify needs it to."""
    with override_settings(AUDIT=settings_with(NAMESPACE="acme")):
        ledger = ledgers["audit"]
        await fill(ledger)
        pruned = await prune("default", before=CUT, ledger=ledger)
        report = await verify("default", ledger, now=T1)
    await connections.close_all()
    await ledgers.close_all()

    assert pruned.checkpoint is not None
    assert pruned.checkpoint.namespace == "acme"
    assert report.intact, report


async def delete_up_to(seq: int) -> None:
    """What a writer of the tables does: delete the rows and seals up to ``seq``."""
    async with connections.engine().begin() as connection:
        ids = select(audit_seals.c.entry_id).where(audit_seals.c.seq <= seq)
        await connection.execute(delete(audit_log).where(audit_log.c.id.in_(ids)))
        await connection.execute(delete(audit_seals).where(audit_seals.c.seq <= seq))


async def store(checkpoint: Checkpoint, key: Ed25519PrivateKey) -> None:
    """Store ``checkpoint`` signed with ``key``, as a writer of the tables could."""
    async with connections.engine().begin() as connection:
        await connection.execute(
            insert(audit_checkpoints).values(
                seq=checkpoint.seq,
                document=checkpoint.document(),
                signature=key.sign(checkpoint.document().encode()),
                made_at=checkpoint.made_at,
            )
        )


async def test_an_exported_head_checkpoint_covers_no_deletion(trail: Ledger) -> None:
    """SEC-10b-2: a head checkpoint handed out in an export starts nothing.

    It is signed with the project's key and leaves the system; whoever can
    write the tables stores it and deletes the trail up to it. It was
    signed for an export, not a prune, so the gap is a broken link.
    """
    with freeze_time(T1 + timedelta(days=40)):  # Old enough to prune at.
        document = await export(trail)
    (head,) = document["databases"]["default"]["checkpoints"]
    assert Checkpoint.parse(head["document"]).kind == "export"
    with freeze_time(T1 + timedelta(days=41)):
        for n in range(2):
            await add(T1 + timedelta(days=41, minutes=n))
        await seal("default")
        await anchor("default", trail, every=timedelta(0))
    async with connections.engine().begin() as connection:
        await connection.execute(
            insert(audit_checkpoints).values(
                seq=8,
                document=head["document"],
                signature=base64.b64decode(head["signature"]),
                made_at=T1,
            )
        )
    await delete_up_to(8)

    for ledger in (trail, None):
        report = await verify("default", ledger, now=T1 + timedelta(days=200))
        assert report.forged == [8]
        assert report.broken == [9]
        assert not report.intact


async def test_a_checkpoint_out_of_the_chain_starts_nothing(trail: Ledger) -> None:
    """A prune checkpoint must follow the one before it (SEC-10b-2)."""
    pruned = await prune("default", before=CUT, ledger=trail)
    assert pruned.checkpoint is not None
    seven = (await seals())[1]
    unlinked = Checkpoint("default", None, 7, seven["link"], T0, T1, None, KEY_ID)
    await store(unlinked, KEY)
    await delete_up_to(7)

    report = await verify("default")

    assert report.forged == [7]
    assert report.broken == [8]


KEY_ID: Final = key_id(KEY.public_key())


async def test_the_application_cannot_sign_away_its_trail(trail: Ledger) -> None:
    """SEC-10b-3: the application verifies with the public key; signing is not enough.

    It need not hold the private key at all. Whoever has the key as well
    as the tables must record the checkpoint on the ledger too, and
    even then cannot cut where the ledger saw the seal less than
    ``AUDIT.MIN_RETENTION`` ago, however the checkpoint is dated.
    """
    with (
        override_settings(AUDIT=settings_with(CHECKPOINT_KEY=None)),
        pytest.raises(ImproperlyConfigured),
    ):
        signing_key()
    now = T1 + timedelta(days=1)
    seven = (await seals())[6]
    stolen = Checkpoint("default", None, 7, seven["link"], T0, now, None, KEY_ID)
    await store(stolen, KEY)
    await delete_up_to(7)

    unrecorded = await verify("default", trail, now=now)
    await trail.append(pruned_key("default"), pruned_record(stolen.document()))
    backdated = Anchor(7, seven["link"], T0)
    await trail.append(anchor_key("default"), backdated.encode())
    too_young = await verify("default", trail, now=now)

    assert unrecorded.forged == too_young.forged == [7]
    assert not unrecorded.intact
    assert not too_young.intact


async def test_without_a_ledger_a_checkpoint_dates_its_own_seal(
    trail: Ledger,
) -> None:
    """Signed as sealed within ``MIN_RETENTION`` of its making: it starts nothing."""
    five = (await seals())[4]
    young = Checkpoint(
        "default", None, 5, five["link"], T1 - timedelta(days=29), T1, None, KEY_ID
    )
    await store(young, KEY)
    await delete_up_to(5)

    report = await verify("default", now=T1 + timedelta(days=400))

    assert report.forged == [5]
    assert report.broken == [6]


def test_a_seal_is_first_anchored_when_the_ledger_first_saw_it() -> None:
    """An anchor appended later and dated back moves nothing before it."""
    link = "a" * 64
    history = [
        Anchor(5, link, T0),
        Anchor(8, link, T1),
        Anchor(7, link, T0),  # Appended after, dated back.
    ]

    assert first_anchored(history, 3) == T0
    assert first_anchored(history, 7) == T1
    assert first_anchored(history, 9) is None
    assert first_anchored([Anchor(5, link, T1), Anchor(9, link, T0)], 9) == T1


async def test_prune_keeps_the_minimum_retention(trail: Ledger) -> None:
    """``auditprune`` refuses a cut within ``AUDIT.MIN_RETENTION``; nothing goes."""
    with (
        freeze_time(T1),
        pytest.raises(PruneRefused, match="MIN_RETENTION keeps every row for 30 days"),
    ):
        await prune("default", before=CUT + timedelta(days=1), ledger=trail)
    with freeze_time(T1 + timedelta(days=10)):
        early = await prune("default", before=T1 - timedelta(days=20), ledger=trail)

    assert early.checkpoint is not None
    assert early.checkpoint.seq == 5
    assert len(await seals()) == 3


async def test_with_a_ledger_the_cut_was_anchored_long_enough_ago(
    trail: Ledger,
) -> None:
    """Sealed long ago but first anchored lately: the cut stays below it."""
    with freeze_time(T1 + timedelta(days=31)):
        for n in range(2):
            await add(T1 + timedelta(days=31, minutes=n))
        await seal("default")
        await anchor("default", trail, every=timedelta(0))  # Seal 10.
        await add(T1 + timedelta(days=31, minutes=5))
        await seal("default")
    async with connections.engine().begin() as connection:
        await connection.execute(
            update(audit_seals)
            .where(audit_seals.c.seq.in_([9, 10]))
            .values(sealed_at=T0 + timedelta(days=1))
        )
    with freeze_time(T1 + timedelta(days=50)):
        pruned = await prune("default", before=T1 + timedelta(days=20), ledger=trail)

    assert pruned.checkpoint is not None
    assert pruned.checkpoint.seq == 8


def test_checkpoints_say_what_they_were_signed_for() -> None:
    """``kind`` is ``prune`` or ``export``, signed with the rest."""
    good = Checkpoint("default", None, 5, "a" * 64, T0, T1, None, "k", "export")
    assert Checkpoint.parse(good.document()).kind == "export"
    odd = good.document().replace('"export"', '"anything"')
    with pytest.raises(CheckpointInvalid, match="kind is one of"):
        Checkpoint.parse(odd)


async def test_an_export_checkpoint_ends_its_chain(trail: Ledger) -> None:
    """In an export, only pruning checkpoints are followed."""
    with freeze_time(T1 + timedelta(days=1)):
        document = await export(trail)
    (head,) = document["databases"]["default"]["checkpoints"]
    after = Checkpoint(
        "default",
        None,
        9,
        "c" * 64,
        T1,
        T1,
        (8, Checkpoint.parse(head["document"]).link),
        KEY_ID,
        "export",
    )
    document["databases"]["default"]["checkpoints"].append(
        {
            "document": after.document(),
            "signature": base64.b64encode(KEY.sign(after.document().encode())).decode(),
        }
    )
    with pytest.raises(CheckpointInvalid, match="follows a head checkpoint"):
        check_export(json.dumps(document).encode(), PUBLIC_PEM)
