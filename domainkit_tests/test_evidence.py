"""Evidence: canonical records, the ledger store, durable recording, the command."""

import asyncio
import dataclasses
import hashlib
import hmac
import io
import re
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Final

import pytest
from pydantic import ValidationError
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
from domainkit.contrib.evidence import apps as evidence_apps
from domainkit.contrib.evidence.settings import (
    DEVELOPMENT_SUBJECT_KEY,
    EvidenceSettings,
)
from domainkit.contrib.evidence.tasks import (
    RECORD_TASK,
    defer_evidence,
    record_evidence,
)
from domainkit.contrib.messaging import Worker
from domainkit.contrib.messaging.tables import messages
from domainkit.db import connections
from domainkit.db.routing import router
from domainkit.exceptions import ImproperlyConfigured
from domainkit.ledger import (
    Ledger,
    LedgerEntry,
    LedgerTampered,
    LedgerWrite,
    MemoryLedger,
    ledgers,
)
from domainkit.management import CommandError
from domainkit_tests.conftest import MakeDatabase
from domainkit_tests.test_db import NotesUnitOfWork, notes

pytestmark = pytest.mark.anyio

EVIDENCE = "domainkit.contrib.evidence"
MESSAGING = "domainkit.contrib.messaging"
FIXED = Evidence(
    kind="kyc.document_verified",
    subject="customer:42",
    facts={"result": "match", "score": "0.97"},
    artifacts={"passport": "sha256:" + "0" * 64},
    id=uuid.UUID(int=7),
    recorded_at=datetime(2026, 10, 14, 9, 30, tzinfo=UTC),
    actor="user:7",
    request_id="req-1",
)
FLOAT_RECORD = (
    '{"actor":"user:7","artifacts":{"passport":"sha256:' + "0" * 64 + '"},'
    '"facts":{"result":"match","score":0.97},"format":1,'
    '"id":"00000000-0000-0000-0000-000000000007","kind":"kyc.document_verified",'
    '"recorded_at":"2026-10-14T09:30:00.000000+00:00","request_id":"req-1",'
    '"subject":"customer:42"}'
).encode()
"""A record written before floats were refused: FIXED, with a float score."""


SUBJECT_KEY = b"subject-key-for-the-evidence-tests"


def store(
    ledger: Ledger | None = None, *, namespace: str = "evidence", max_bytes: int = 4096
) -> LedgerEvidenceStore:
    """A store on ``ledger``, or on a fresh in-memory ledger."""
    return LedgerEvidenceStore(
        ledger or MemoryLedger(),
        namespace=namespace,
        max_bytes=max_bytes,
        subject_key=SUBJECT_KEY,
    )


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
        "sha256:23d0fb536aabffe8cf960d22ecfd877ff840b30c9862a32e2c374b2c6b287a6f"
    )
    assert Evidence.from_canonical(body) == FIXED


def test_records_written_with_floats_keep_their_bytes() -> None:
    """Read back, an older record is written again exactly; a changed copy is not."""
    evidence = Evidence.from_canonical(FLOAT_RECORD)

    assert sha256(FLOAT_RECORD) == (  # The frozen digest of the format before.
        "sha256:318fd5a72c0f55200492f4ea5f4f2a6d0a4207e2e8fd2f933800641e26dd8f0a"
    )
    assert evidence.canonical() == FLOAT_RECORD
    assert evidence.facts["score"] == 0.97  # Read as written.
    assert evidence == dataclasses.replace(FIXED, facts=evidence.facts)
    with pytest.raises(ValueError, match="may not hold floats"):
        dataclasses.replace(evidence, request_id="req-2").canonical()


def test_decimals_are_written_as_fixed_point_strings() -> None:
    """Amounts stay exact: Decimal goes in as a string and comes back as one."""
    facts = {
        "amount": Decimal("10.00"),
        "lines": [{"price": Decimal("1E+3")}, {"price": Decimal("-0.000001")}],
        "count": 3,
    }
    body = Evidence(kind="k", subject="s", facts=facts).canonical()
    read = Evidence.from_canonical(body)

    assert b'"facts":{"amount":"10.00","count":3,"lines":' in body
    assert b'[{"price":"1000"},{"price":"-0.000001"}]' in body
    assert read.facts == {
        "amount": "10.00",
        "lines": [{"price": "1000"}, {"price": "-0.000001"}],
        "count": 3,
    }
    assert dataclasses.replace(read).canonical() == body  # Strings re-encode alike.


FLOAT: Final = (
    "Evidence facts may not hold floats ({}): use Decimal or str, so amounts "
    "stay exact."
)
INFINITE: Final = "Evidence facts must be finite numbers ({})."


@pytest.mark.parametrize(
    ("facts", "message"),
    [
        ({"amount": 1e21}, FLOAT.format("facts.amount")),
        (
            {"lines": [{"price": 1}, {"price": 0.1}]},
            FLOAT.format("facts.lines[1].price"),
        ),
        ({"rates": (Decimal(1), 2.5)}, FLOAT.format("facts.rates[1]")),
        ({"amount": Decimal("NaN")}, INFINITE.format("facts.amount")),
        ({"amount": Decimal("-Infinity")}, INFINITE.format("facts.amount")),
    ],
)
def test_floats_and_non_finite_amounts_are_refused(
    facts: dict[str, Any], message: str
) -> None:
    """A float has no exact decimal value; the message names where, not what."""
    with pytest.raises(ValueError, match="^" + re.escape(message) + "$"):
        Evidence(kind="k", subject="s", facts=facts).canonical()


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        (
            {"kind": "KYC.Done"},
            "Evidence kind 'KYC.Done' must be a dotted lowercase name.",
        ),
        (
            {"kind": "a" * 101},
            f"Evidence kind '{'a' * 101}' must be a dotted lowercase name.",
        ),
        ({"subject": ""}, "Evidence subject must be 1 to 255 characters."),
        ({"subject": "s" * 256}, "Evidence subject must be 1 to 255 characters."),
        (
            {"artifacts": {"scan": "md5:abc"}},
            "Artifact 'scan' must be a sha256:<hex> digest.",
        ),
        (
            {"recorded_at": datetime(2026, 1, 1)},  # noqa: DTZ001 - the error.
            "Evidence recorded_at must be timezone-aware.",
        ),
    ],
)
def test_evidence_is_validated(changes: dict[str, Any], message: str) -> None:
    """Records that could not be stored or checked later are refused."""
    values = {"kind": "kyc.checked", "subject": "customer:1", **changes}
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        Evidence(**values)


def test_records_keep_time_in_utc(local_zone: Callable[[str], None]) -> None:
    """recorded_at is written in UTC, whatever zone it has or the host uses."""
    cairo = datetime(2026, 10, 14, 11, 30, tzinfo=timezone(timedelta(hours=2)))
    local_zone("Asia/Tokyo")

    body = dataclasses.replace(FIXED, recorded_at=cairo).canonical()

    assert b'"recorded_at":"2026-10-14T09:30:00.000000+00:00"' in body


SUBJECT_REFUSED = (
    "Evidence subject must be an internal id such as 'customer:42' "
    "(letters, digits, and _ . : / -), never personal data such as an email "
    "address, a name, or a phone number."
)


@pytest.mark.parametrize(
    "subject",
    [
        "jane@example.com",
        "customer:jane@example.com",
        "Jane Smith",
        "+201001234567",
        "customer:\u0639\u0645\u064a\u0644",
        "-customer:1",
        "customer:1\n",
    ],
)
def test_subjects_must_be_internal_ids(subject: str) -> None:
    """Email addresses, names, and phone numbers are refused, and not echoed."""
    with pytest.raises(ValueError, match=re.escape(SUBJECT_REFUSED)) as refused:
        Evidence(kind="kyc.checked", subject=subject)

    assert str(refused.value) == SUBJECT_REFUSED


@pytest.mark.parametrize(
    "subject",
    ["customer:42", str(uuid.UUID(int=1)), "tenant/7:invoice:2026-001", "A_b.c", "7"],
)
def test_internal_ids_are_subjects(subject: str) -> None:
    """Letters, digits, and _ . : / - make an id."""
    assert Evidence(kind="kyc.checked", subject=subject).subject == subject


def test_the_longest_kind_and_subject_are_accepted() -> None:
    """Limits are inclusive: 100 characters of kind, 255 of subject."""
    evidence = Evidence(kind="k" * 100, subject="s" * 255)

    assert (len(evidence.kind), len(evidence.subject)) == (100, 255)


def test_records_keep_text_as_utf8() -> None:
    """Non-ASCII facts are stored as UTF-8, not as escapes."""
    body = Evidence(kind="k", subject="s", facts={"name": "Café"}).canonical()

    assert "Café".encode() in body


def test_facts_must_be_plain_json() -> None:
    """Sets, other objects, and lone surrogates have no single UTF-8 JSON form."""
    for facts in (
        {"tags": {"a"}},
        {"at": datetime(2026, 1, 1, tzinfo=UTC)},
        {"name": "\ud800"},
    ):
        with pytest.raises(ValueError, match="plain JSON values"):
            Evidence(kind="k", subject="s", facts=facts).canonical()


@pytest.mark.parametrize(
    ("record", "shown"), [(b'{"format": 2}', "2"), (b"{}", "None")]
)
def test_unknown_record_formats_are_refused(record: bytes, shown: str) -> None:
    """A record from a future layout, or none, is not misread."""
    with pytest.raises(ValueError, match=rf"^Unknown evidence format {shown}\.$"):
        Evidence.from_canonical(record)


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
    keyed = subject_digest("customer:42", SUBJECT_KEY)
    assert receipt.key == f"evidence/{keyed}/{FIXED.id}"
    assert receipt.key == evidence_key("evidence", FIXED, SUBJECT_KEY)
    assert receipt.digest == sha256(FIXED.canonical())
    assert (recorded.evidence, recorded.revisions, recorded.intact) == (FIXED, 1, True)
    assert recorded.receipt == receipt  # With its ledger transaction.
    assert "customer" not in receipt.key


def test_keys_use_a_keyed_digest_of_the_subject() -> None:
    """Keys use HMAC-SHA-256 of the subject under the subject key, not plain SHA-256."""
    keyed = hmac.new(SUBJECT_KEY, b"customer:42", hashlib.sha256).hexdigest()[:32]

    assert subject_digest("customer:42", SUBJECT_KEY) == keyed
    assert subject_digest("customer:42", b"another-key") != keyed
    assert keyed != hashlib.sha256(b"customer:42").hexdigest()[:32]
    assert evidence_key("e", FIXED, SUBJECT_KEY) == f"e/{keyed}/{FIXED.id}"


async def test_retries_compare_with_the_original_record() -> None:
    """After a rewrite, recording the same evidence still matches revision 1."""
    ledger = MemoryLedger()
    evidence_store_ = store(ledger)
    receipt = await evidence_store_.record(FIXED)
    await ledger.append(receipt.key.encode(), b'{"forged": true}')

    assert await evidence_store_.record(FIXED) == receipt


async def test_records_may_be_exactly_the_limit() -> None:
    """MAX_BYTES itself is allowed; one byte more is not."""
    body = FIXED.canonical()
    at_limit = store(namespace="e", max_bytes=len(body))
    under = store(namespace="e", max_bytes=len(body) - 1)

    await at_limit.record(FIXED)
    message = (
        f"Evidence record is {len(body)} bytes; the limit is {len(body) - 1}. "
        "Store documents elsewhere and record their sha256 digests."
    )
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
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
    evidence_store_ = store(ledger)
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

    class CorruptReads(MemoryLedger):
        async def read(self, key: bytes, revision: int | None = None) -> LedgerEntry:
            raise LedgerTampered("proof failed")

    class CorruptWrites(MemoryLedger):
        async def create(self, key: bytes, value: bytes) -> LedgerWrite:
            raise LedgerTampered("proof failed")

    key = evidence_key("e", FIXED, SUBJECT_KEY)
    reads = CorruptReads()
    await reads.append(key.encode(), FIXED.canonical())

    with pytest.raises(EvidenceTampered) as writing:
        await store(CorruptWrites(), namespace="e").record(FIXED)
    with pytest.raises(EvidenceTampered) as replaying:
        await store(reads, namespace="e").record(FIXED)
    with pytest.raises(EvidenceTampered, match=f"failed verification reading {key}"):
        await store(reads, namespace="e").get(key)

    assert str(writing.value) == f"The ledger failed verification writing {key}."
    assert str(replaying.value) == f"The ledger failed verification reading {key}."


class Interleaving(MemoryLedger):
    """Answers reads after other tasks had a turn, as a remote ledger does."""

    async def read(self, key: bytes, revision: int | None = None) -> LedgerEntry:
        """Read, then let other tasks run before answering."""
        try:
            return await super().read(key, revision)
        finally:
            await asyncio.sleep(0)


async def test_racing_writers_leave_one_revision() -> None:
    """Concurrent records of one evidence share a receipt; none looks tampered."""
    ledger = Interleaving()
    evidence_store_ = store(ledger)

    receipts = await asyncio.gather(*(evidence_store_.record(FIXED) for _ in range(5)))

    assert len(set(receipts)) == 1
    assert (await evidence_store_.verify(receipts[0])).intact
    assert await ledger.revision_count(receipts[0].key.encode(), limit=5) == 1


async def test_racing_writers_of_different_records_conflict() -> None:
    """Of two records racing for one id, the first wins; the other conflicts."""
    ledger = Interleaving()
    evidence_store_ = store(ledger)
    rival = dataclasses.replace(FIXED, facts={"result": "mismatch"})

    first, second = await asyncio.gather(
        evidence_store_.record(FIXED),
        evidence_store_.record(rival),
        return_exceptions=True,
    )

    assert isinstance(second, EvidenceConflict)
    assert str(second) == f"Evidence {FIXED.id} already holds another record."
    assert isinstance(first, EvidenceReceipt)
    assert (await evidence_store_.verify(first)).evidence == FIXED


async def test_records_that_are_not_evidence_are_tampering() -> None:
    """Revision 1 under an evidence key must parse as a record."""
    ledger = MemoryLedger()
    evidence_store_ = store(ledger)
    keys = [f"evidence/{'0' * 32}/{uuid.uuid4()}" for _ in range(2)]
    await ledger.append(keys[0].encode(), b"not json")
    await ledger.append(keys[1].encode(), b'{"format": 1}')

    for key in keys:
        with pytest.raises(EvidenceTampered, match="does not hold an evidence record"):
            await evidence_store_.get(key)


async def test_records_under_another_key_are_tampering() -> None:
    """A real record copied under another key, or another subject, is caught."""
    ledger = MemoryLedger()
    evidence_store_ = store(ledger)
    theirs = Evidence(kind="consent.given", subject="customer:1")
    receipt = await evidence_store_.record(theirs)
    body = theirs.canonical()
    copied = f"evidence/{subject_digest('customer:3', SUBJECT_KEY)}/{uuid.uuid4()}"
    filed = f"evidence/{subject_digest('customer:2', SUBJECT_KEY)}/{theirs.id}"
    await ledger.append(copied.encode(), body)
    await ledger.append(filed.encode(), body)

    with pytest.raises(EvidenceTampered) as moved:
        await evidence_store_.get(copied)
    with pytest.raises(EvidenceTampered) as misfiled:
        await evidence_store_.for_subject("customer:2")

    assert str(moved.value) == f"{copied} holds the record of evidence {theirs.id}."
    assert str(misfiled.value) == (
        f"{filed} is filed under the subject of another record."
    )
    assert (await evidence_store_.get(filed)).evidence == theirs  # Its own id.
    assert [r.receipt for r in await evidence_store_.for_subject("customer:1")] == [
        receipt
    ]


async def test_keys_from_an_earlier_namespace_or_subject_key_still_read() -> None:
    """Receipts keep working after SUBJECT_KEY and NAMESPACE changes.

    After a NAMESPACE change, while ``PREVIOUS_NAMESPACES`` lists the old one.
    """
    ledger = MemoryLedger()
    evidence = Evidence(kind="consent.given", subject="customer:1")
    receipt = await store(ledger).record(evidence)
    later = LedgerEvidenceStore(
        ledger,
        namespace="moved",
        max_bytes=4096,
        subject_key=b"k" * 32,
        previous_namespaces=["evidence"],
    )
    rekeyed = LedgerEvidenceStore(
        ledger, namespace="evidence", max_bytes=4096, subject_key=b"k" * 32
    )

    assert (await later.verify(receipt)).evidence == evidence
    assert (await rekeyed.verify(receipt)).evidence == evidence
    assert await later.for_subject("customer:1") == []


async def test_keys_outside_the_namespace_are_not_found() -> None:
    """``get`` reads its own namespaces' evidence keys only (DATA-08).

    At 0.9.0 any key ending in a record's id was read: another project's or
    environment's evidence on a shared ledger, and an audit anchor came
    back as tampering instead of not found.
    """
    ledger = MemoryLedger()
    evidence = Evidence(kind="consent.given", subject="customer:1")
    receipt = await store(ledger, namespace="staging").record(evidence)
    await ledger.append(b"domainkit/audit/default", b'{"seq": 1}')
    production = store(ledger, namespace="production")
    digest = subject_digest("customer:1", SUBJECT_KEY)

    for key in (
        receipt.key,
        "domainkit/audit/default",
        f"production/{digest}",
        f"production/{digest}/{evidence.id}/more",
        f"production/{digest[:-1]}/{evidence.id}",
        f"/{digest}/{evidence.id}",
    ):
        with pytest.raises(EvidenceNotFound, match="No evidence under"):
            await production.get(key)
    with pytest.raises(EvidenceNotFound):
        await production.verify(receipt)
    assert (await store(ledger, namespace="staging").get(receipt.key)).intact


async def test_missing_and_oversized_evidence() -> None:
    """Unknown keys are 404s; records over MAX_BYTES are refused."""
    evidence_store_ = store()
    big = Evidence(kind="k", subject="s", facts={"blob": "x" * 5000})

    with pytest.raises(EvidenceNotFound, match="No evidence under"):
        await evidence_store_.get(f"evidence/{'0' * 32}/{uuid.uuid4()}")
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


async def test_subjects_are_listed_a_hundred_records_at_a_time() -> None:
    """The prefix scan asks for the limit (100 by default), not for everything."""

    class Counting(MemoryLedger):
        def __init__(self) -> None:
            super().__init__()
            self.limits: list[int] = []

        async def keys(self, prefix: bytes, *, limit: int) -> list[bytes]:
            self.limits.append(limit)
            return await super().keys(prefix, limit=limit)

    ledger = Counting()
    await store(ledger).for_subject("customer:1")
    await store(ledger).for_subject("customer:1", limit=7)

    assert ledger.limits == [100, 7]


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


def test_the_subject_key_is_a_long_secret() -> None:
    """32 characters or more; the public development key when unset."""
    assert EvidenceSettings().subject_key == DEVELOPMENT_SUBJECT_KEY
    assert EvidenceSettings(SUBJECT_KEY="s" * 32).subject_key == b"s" * 32
    with pytest.raises(ValidationError, match="at least 32"):
        EvidenceSettings(SUBJECT_KEY="s" * 31)


DEPLOYED = {
    "ALLOWED_HOSTS": ["api.example.com"],
    "DATABASES": {
        "default": {"URL": "postgresql+asyncpg://db/app", "SSLMODE": "verify-full"}
    },
    "LEDGERS": {
        "evidence": {
            "URL": "immudb://writer:pw@ledger.internal:3322/evidence",
            "TLS_CA": "/ca.pem",
            "SERVER_SIGNING_KEY": "/k.pub",
        }
    },
}


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_deployments_need_a_subject_key(environment: str) -> None:
    """The development key is public, so staging and production refuse it."""
    registry.clear()
    deployed = {**DEPLOYED, "ENVIRONMENT": environment}
    with (
        override_settings(INSTALLED_DOMAINS=[EVIDENCE], ALPHA=UNSET, **deployed),
        pytest.raises(ImproperlyConfigured) as refused,
    ):
        setup()
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[EVIDENCE],
        ALPHA=UNSET,
        EVIDENCE={"SUBJECT_KEY": "s" * 32},
        **deployed,
    ):
        setup()
    registry.clear()

    assert str(refused.value) == (
        f"EVIDENCE.SUBJECT_KEY is required in {environment}: a random secret of "
        "32 characters or more, kept for as long as the evidence."
    )


def test_durable_recording_is_registered_with_messaging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ready() imports the recording task only when messaging is installed."""
    imported: list[str] = []
    monkeypatch.setattr(evidence_apps, "import_module", imported.append)
    for domains in ([EVIDENCE], [MESSAGING, EVIDENCE]):
        registry.clear()
        with override_settings(
            INSTALLED_DOMAINS=domains,
            DATABASES={"default": {"URL": "sqlite+aiosqlite:///:memory:"}},
            LEDGERS={"evidence": {"URL": "memory://"}},
            ALPHA=UNSET,
        ):
            setup()
    registry.clear()

    assert imported == ["domainkit.contrib.evidence.tasks"]


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


async def test_deferred_keys_use_the_configured_subject_key(installed: None) -> None:
    """defer_evidence returns the key the worker writes, under SUBJECT_KEY."""
    evidence = Evidence(kind="invoice.issued", subject="invoice:3")
    subject_key = "k" * 32

    with override_settings(EVIDENCE={"SUBJECT_KEY": subject_key}):
        async with NotesUnitOfWork() as uow:
            key = defer_evidence(uow, evidence)
            await uow.commit()
        await Worker().run_once()
        recorded = await evidence_store().get(key)

    keyed = subject_digest("invoice:3", subject_key.encode())
    assert key == f"evidence/{keyed}/{evidence.id}"
    assert recorded.evidence == evidence


async def test_queued_records_with_floats_are_still_recorded(installed: None) -> None:
    """A record deferred before the upgrade is written as its receipt says."""
    await record_evidence(FLOAT_RECORD.decode())

    (recorded,) = await evidence_store().for_subject("customer:42")

    assert recorded.receipt.digest == sha256(FLOAT_RECORD)
    assert recorded.evidence.facts["score"] == 0.97
    assert recorded.intact


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
