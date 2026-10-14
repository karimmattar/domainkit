"""Evidence: canonical records, the ledger store, durable recording, the command."""

import io
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import UNSET, override_settings
from domainkit.context import RequestContext, bind_request
from domainkit.contrib.evidence import (
    Evidence,
    EvidenceConflict,
    EvidenceNotFound,
    EvidenceReceipt,
    EvidenceTampered,
    LedgerEvidenceStore,
    evidence_key,
    evidence_store,
    sha256,
    subject_digest,
)
from domainkit.contrib.evidence.tasks import RECORD_TASK, defer_evidence
from domainkit.contrib.messaging import Worker
from domainkit.contrib.messaging.tables import messages
from domainkit.db import connections
from domainkit.db.routing import router
from domainkit.exceptions import ImproperlyConfigured
from domainkit.ledger import LedgerEntry, LedgerTampered, MemoryLedger, ledgers
from domainkit.management import CommandError
from domainkit_tests.conftest import MakeDatabase
from domainkit_tests.test_db import NotesUnitOfWork, notes

pytestmark = pytest.mark.anyio

EVIDENCE = "domainkit.contrib.evidence"
MESSAGING = "domainkit.contrib.messaging"
FIXED = Evidence(
    kind="kyc.document_verified",
    subject="customer:42",
    facts={"result": "match", "score": 0.97},
    artifacts={"passport": "sha256:" + "0" * 64},
    id=uuid.UUID(int=7),
    recorded_at=datetime(2026, 10, 14, 9, 30, tzinfo=UTC),
    actor="user:7",
    request_id="req-1",
)


def store() -> LedgerEvidenceStore:
    """A store on a fresh in-memory ledger."""
    return LedgerEvidenceStore(MemoryLedger(), namespace="evidence", max_bytes=4096)


@pytest.fixture
async def installed(make_database: MakeDatabase) -> AsyncIterator[None]:
    """Evidence and messaging installed; an in-memory ledger; outbox tables."""
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[MESSAGING, EVIDENCE],
        DATABASES={"default": {"URL": make_database("default")}},
        LEDGERS={"evidence": {"URL": "memory://"}},
        ALPHA=UNSET,
    ):
        setup()
        async with connections.engine().begin() as connection:
            await connection.run_sync(messages.create)
            await connection.run_sync(notes.create)
        yield
        await connections.close_all()
        await ledgers.close_all()
    registry.clear()


# Records -------------------------------------------------------------------------


def test_canonical_records_never_change() -> None:
    """Frozen digest: changing the format would break every receipt issued."""
    body = FIXED.canonical()

    assert sha256(body) == (
        "sha256:318fd5a72c0f55200492f4ea5f4f2a6d0a4207e2e8fd2f933800641e26dd8f0a"
    )
    assert Evidence.from_canonical(body) == FIXED


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"kind": "KYC.Done"}, "dotted lowercase"),
        ({"kind": "a" * 101}, "dotted lowercase"),
        ({"subject": ""}, "1 to 255 characters"),
        ({"subject": "s" * 256}, "1 to 255 characters"),
        ({"artifacts": {"scan": "md5:abc"}}, "sha256:<hex>"),
        ({"recorded_at": datetime(2026, 1, 1)}, "timezone-aware"),  # noqa: DTZ001 - the error.
    ],
)
def test_evidence_is_validated(changes: dict[str, Any], message: str) -> None:
    """Records that could not be stored or checked later are refused."""
    values = {"kind": "kyc.checked", "subject": "customer:1", **changes}
    with pytest.raises(ValueError, match=message):
        Evidence(**values)


def test_the_longest_kind_and_subject_are_accepted() -> None:
    """Limits are inclusive: 100 characters of kind, 255 of subject."""
    evidence = Evidence(kind="k" * 100, subject="s" * 255)

    assert (len(evidence.kind), len(evidence.subject)) == (100, 255)


def test_records_keep_text_as_utf8() -> None:
    """Non-ASCII facts are stored as UTF-8, not as escapes."""
    body = Evidence(kind="k", subject="s", facts={"name": "Café"}).canonical()

    assert "Café".encode() in body


def test_facts_must_be_plain_json() -> None:
    """Sets, objects, and NaN have no single JSON form."""
    for facts in ({"tags": {"a"}}, {"score": float("nan")}):
        with pytest.raises(ValueError, match="plain JSON values"):
            Evidence(kind="k", subject="s", facts=facts).canonical()


def test_unknown_record_formats_are_refused() -> None:
    """A record from a future layout is not misread."""
    with pytest.raises(ValueError, match="Unknown evidence format 2"):
        Evidence.from_canonical(b'{"format": 2}')


def test_evidence_remembers_who_and_which_request() -> None:
    """The current request's actor and id are captured when created."""
    with bind_request(RequestContext("req-9", None, "POST", "/", "user:3")):
        evidence = Evidence(kind="consent.given", subject="customer:9")

    assert (evidence.actor, evidence.request_id) == ("user:3", "req-9")
    assert Evidence(kind="consent.given", subject="customer:9").actor is None


# The ledger store ----------------------------------------------------------------


async def test_records_are_written_once_and_read_verified() -> None:
    """A receipt names the key and digest; retries return the same receipt."""
    evidence_store_ = store()

    receipt = await evidence_store_.record(FIXED)
    retried = await evidence_store_.record(FIXED)
    recorded = await evidence_store_.verify(receipt)

    assert receipt == retried
    assert receipt.key == f"evidence/{subject_digest('customer:42')}/{FIXED.id}"
    assert receipt.key == evidence_key("evidence", FIXED)
    assert receipt.digest == sha256(FIXED.canonical())
    assert (recorded.evidence, recorded.revisions, recorded.intact) == (FIXED, 1, True)
    assert "customer" not in receipt.key


async def test_retries_compare_with_the_original_record() -> None:
    """After a rewrite, recording the same evidence still matches revision 1."""
    ledger = MemoryLedger()
    evidence_store_ = LedgerEvidenceStore(ledger, namespace="evidence", max_bytes=4096)
    receipt = await evidence_store_.record(FIXED)
    await ledger.append(receipt.key.encode(), b'{"forged": true}')

    assert await evidence_store_.record(FIXED) == receipt


async def test_records_may_be_exactly_the_limit() -> None:
    """MAX_BYTES itself is allowed; one byte more is not."""
    body = FIXED.canonical()
    at_limit = LedgerEvidenceStore(MemoryLedger(), namespace="e", max_bytes=len(body))
    under = LedgerEvidenceStore(MemoryLedger(), namespace="e", max_bytes=len(body) - 1)

    await at_limit.record(FIXED)
    with pytest.raises(ValueError, match="the limit is"):
        await under.record(FIXED)


async def test_a_different_record_cannot_reuse_an_id() -> None:
    """Same id, other content: a conflict, never a second revision."""
    evidence_store_ = store()
    await evidence_store_.record(FIXED)
    altered = Evidence(
        kind=FIXED.kind,
        subject=FIXED.subject,
        facts={"result": "no match"},
        id=FIXED.id,
        recorded_at=FIXED.recorded_at,
    )

    with pytest.raises(EvidenceConflict, match="already holds another record"):
        await evidence_store_.record(altered)
    assert EvidenceConflict.code == "evidence_conflict"


async def test_writing_a_key_again_is_detected() -> None:
    """Revision 1 stays the evidence; a second revision fails verification."""
    ledger = MemoryLedger()
    evidence_store_ = LedgerEvidenceStore(ledger, namespace="evidence", max_bytes=4096)
    receipt = await evidence_store_.record(FIXED)
    await ledger.append(receipt.key.encode(), b'{"forged": true}')

    recorded = await evidence_store_.get(receipt.key)

    assert (recorded.evidence, recorded.intact) == (FIXED, False)
    with pytest.raises(EvidenceTampered, match="written again"):
        await evidence_store_.verify(receipt)
    forged = EvidenceReceipt(receipt.id, receipt.key, "sha256:" + "f" * 64, 1)
    with pytest.raises(EvidenceTampered, match="does not match its receipt"):
        await evidence_store_.verify(forged)


async def test_ledger_verification_failures_surface_as_tampering() -> None:
    """A failed proof is never reported as missing or as a conflict."""

    class Corrupt(MemoryLedger):
        async def read(self, key: bytes, revision: int | None = None) -> LedgerEntry:
            raise LedgerTampered("proof failed")

    evidence_store_ = LedgerEvidenceStore(Corrupt(), namespace="e", max_bytes=4096)

    with pytest.raises(EvidenceTampered, match="failed verification"):
        await evidence_store_.record(FIXED)
    with pytest.raises(EvidenceTampered, match="failed verification"):
        await evidence_store_.get("e/x/y")


async def test_records_that_are_not_evidence_are_tampering() -> None:
    """Revision 1 under an evidence key must parse as a record."""
    ledger = MemoryLedger()
    evidence_store_ = LedgerEvidenceStore(ledger, namespace="evidence", max_bytes=4096)
    await ledger.append(b"evidence/x/y", b"not json")
    await ledger.append(b"evidence/x/z", b'{"format": 1}')

    for key in ("evidence/x/y", "evidence/x/z"):
        with pytest.raises(EvidenceTampered, match="does not hold an evidence record"):
            await evidence_store_.get(key)


async def test_missing_and_oversized_evidence() -> None:
    """Unknown keys are 404s; records over MAX_BYTES are refused."""
    evidence_store_ = store()
    big = Evidence(kind="k", subject="s", facts={"blob": "x" * 5000})

    with pytest.raises(EvidenceNotFound, match="No evidence under"):
        await evidence_store_.get("evidence/none/none")
    with pytest.raises(ValueError, match="the limit is 4096"):
        await evidence_store_.record(big)


async def test_evidence_is_listed_by_subject() -> None:
    """A subject's records come back; other subjects' do not."""
    evidence_store_ = store()
    mine = [Evidence(kind="consent.given", subject="customer:1") for _ in range(3)]
    for evidence in [*mine, Evidence(kind="consent.given", subject="customer:2")]:
        await evidence_store_.record(evidence)

    listed = await evidence_store_.for_subject("customer:1")

    assert sorted(r.evidence.id for r in listed) == sorted(e.id for e in mine)
    assert len(await evidence_store_.for_subject("customer:1", limit=2)) == 2


# The app -------------------------------------------------------------------------


async def test_the_configured_store_uses_the_evidence_ledger(installed: None) -> None:
    """evidence_store() reads EVIDENCE; the app keeps no SQL tables."""
    receipt = await evidence_store().record(FIXED)

    assert receipt.key.startswith("evidence/")
    assert (
        await ledgers["evidence"].read(receipt.key.encode())
    ).value == FIXED.canonical()
    config = registry.get_domain_config("evidence")
    assert config.databases(router) == frozenset()


def test_the_evidence_ledger_must_be_in_ledgers() -> None:
    """EVIDENCE.LEDGER naming a SQL alias fails at setup."""
    registry.clear()
    with (
        override_settings(
            INSTALLED_DOMAINS=[EVIDENCE],
            DATABASES={"default": {"URL": "sqlite+aiosqlite:///:memory:"}},
            EVIDENCE={"LEDGER": "default"},
            ALPHA=UNSET,
        ),
        pytest.raises(
            ImproperlyConfigured,
            match="LEDGERS has no 'default' alias; it is a SQL database",
        ),
    ):
        setup()
    registry.clear()


def test_evidence_works_without_messaging() -> None:
    """Direct recording needs no outbox; only defer_evidence does."""
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[EVIDENCE],
        DATABASES={"default": {"URL": "sqlite+aiosqlite:///:memory:"}},
        LEDGERS={"evidence": {"URL": "memory://"}},
        ALPHA=UNSET,
    ):
        setup()

        assert registry.is_installed(EVIDENCE)
        assert not registry.is_installed(MESSAGING)
    registry.clear()


async def test_deferred_evidence_is_recorded_after_commit(installed: None) -> None:
    """The outbox carries the record; the worker writes it, once."""
    evidence = Evidence(kind="invoice.issued", subject="invoice:1", facts={"total": 10})

    async with NotesUnitOfWork() as uow:
        key = defer_evidence(uow, evidence)
        defer_evidence(uow, evidence)
        await uow.commit()
    async with connections.engine().connect() as connection:
        queued = (await connection.execute(select(messages.c.target))).all()
    processed = await Worker().run_once()
    recorded = await evidence_store().get(key)

    assert [row.target for row in queued] == [RECORD_TASK]
    assert processed == 1
    assert (recorded.evidence, recorded.intact) == (evidence, True)


async def test_rolled_back_work_records_no_evidence(installed: None) -> None:
    """Without a commit, nothing reaches the outbox or the ledger."""
    evidence = Evidence(kind="invoice.issued", subject="invoice:2")

    async with NotesUnitOfWork() as uow:
        key = defer_evidence(uow, evidence)

    assert await Worker().run_once() == 0
    with pytest.raises(EvidenceNotFound):
        await evidence_store().get(key)


async def test_the_evidence_command_lists_and_verifies(installed: None) -> None:
    """--subject lists records; --verify proves one, or fails loudly."""
    receipt = await evidence_store().record(FIXED)
    other = await evidence_store().record(
        Evidence(kind="consent.given", subject="customer:42")
    )
    await ledgers["evidence"].append(other.key.encode(), b"rewritten")

    listing = await _run_command("--subject", "customer:42")
    limited = await _run_command("--subject", "customer:42", "--limit", "1")
    intact = await _run_command("--verify", receipt.key)

    assert sorted(line.split()[-1] for line in listing.splitlines()) == [
        "AGAIN",
        "intact",
    ]
    assert len(limited.splitlines()) == 1
    assert intact.startswith("Intact: kyc.document_verified about customer:42")
    with pytest.raises(CommandError, match="written again"):
        await _run_command("--verify", other.key)


async def _run_command(*args: str) -> str:
    """Run the async ``evidence`` command inside this event loop."""
    from domainkit.contrib.evidence.management.commands.evidence import Command

    output = io.StringIO()
    command = Command(stdout=output)
    parser = command.create_parser("manage.py", "evidence")
    await command.handle(**vars(parser.parse_args(list(args))))
    return output.getvalue()
