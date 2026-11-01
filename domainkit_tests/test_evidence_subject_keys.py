"""Evidence's subject keys as a ring: rotate them without losing records or proofs."""

import uuid
from datetime import UTC, datetime
from typing import Final

import pytest
from pydantic import ValidationError

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import UNSET, override_settings
from domainkit.contrib.evidence import (
    Evidence,
    EvidenceTampered,
    LedgerEvidenceStore,
    evidence_store,
    subject_digest,
)
from domainkit.contrib.evidence.settings import (
    DEVELOPMENT_SUBJECT_KEY,
    EvidenceSettings,
)
from domainkit.ledger import MemoryLedger, ledgers

EVIDENCE: Final = "domainkit.contrib.evidence"
OLD: Final = "old-subject-key-of-the-evidence-tests"
NEW: Final = "new-subject-key-of-the-evidence-tests"


def evidence(number: int, subject: str = "customer:42") -> Evidence:
    """Evidence ``number`` about ``subject``."""
    return Evidence(
        kind="kyc.document_verified",
        subject=subject,
        id=uuid.UUID(int=number),
        recorded_at=datetime(2026, 10, 31, tzinfo=UTC),
    )


def store(ledger: MemoryLedger, current: str, *previous: str) -> LedgerEvidenceStore:
    """A store keyed by ``current``, still finding records of ``previous``."""
    return LedgerEvidenceStore(
        ledger,
        namespace="evidence",
        max_bytes=4096,
        subject_key=current.encode(),
        previous_subject_keys=[key.encode() for key in previous],
    )


@pytest.mark.anyio
async def test_a_new_subject_key_writes_new_keys_and_still_finds_old_records() -> None:
    """Rotation: new records under the new key; ``for_subject`` reads both."""
    ledger = MemoryLedger()
    before = await store(ledger, OLD).record(evidence(1))
    rotated = store(ledger, NEW, OLD)
    after = await rotated.record(evidence(2))
    assert after.key.split("/")[1] == subject_digest("customer:42", NEW.encode())
    assert before.key.split("/")[1] == subject_digest("customer:42", OLD.encode())
    found = await rotated.for_subject("customer:42")
    assert [each.evidence.id for each in found] == [uuid.UUID(int=2), uuid.UUID(int=1)]
    assert (await rotated.verify(before)).intact  # The receipt still proves it.
    assert [
        each.evidence.id for each in await rotated.for_subject("customer:42", limit=1)
    ] == [uuid.UUID(int=2)]
    await store(ledger, OLD).record(evidence(3))
    found = await rotated.for_subject("customer:42", limit=2)
    assert [each.evidence.id for each in found] == [uuid.UUID(int=2), uuid.UUID(int=1)]
    forgotten = store(ledger, NEW)
    assert [
        each.evidence.id for each in await forgotten.for_subject("customer:42")
    ] == [uuid.UUID(int=2)]
    assert (await forgotten.verify(before)).intact


@pytest.mark.anyio
async def test_a_record_filed_under_an_old_key_but_about_another_is_tampering() -> None:
    """Under every key of the ring, the records found must be the subject's."""
    ledger = MemoryLedger()
    other = evidence(3, subject="customer:7")
    digest = subject_digest("customer:42", OLD.encode())
    await ledger.create(f"evidence/{digest}/{other.id}".encode(), other.canonical())
    with pytest.raises(EvidenceTampered, match="another record"):
        await store(ledger, NEW, OLD).for_subject("customer:42")


def test_the_ring_of_subject_keys_puts_the_current_one_first() -> None:
    """``SUBJECT_KEY`` is ``default``; ``SUBJECT_KEY_ID`` names the current key."""
    assert EvidenceSettings().subject_keys == {"development": DEVELOPMENT_SUBJECT_KEY}
    assert not EvidenceSettings().has_subject_key
    one = EvidenceSettings(SUBJECT_KEY=OLD)
    assert (one.subject_keys, one.subject_key) == (
        {"default": OLD.encode()},
        OLD.encode(),
    )
    assert EvidenceSettings(SUBJECT_KEYS={"2027": NEW}).subject_key == NEW.encode()
    rotated = EvidenceSettings(
        SUBJECT_KEY=OLD, SUBJECT_KEYS={"2027": NEW}, SUBJECT_KEY_ID="2027"
    )
    assert list(rotated.subject_keys) == ["2027", "default"]
    assert rotated.subject_key == NEW.encode()
    assert rotated.has_subject_key
    back = EvidenceSettings(
        SUBJECT_KEY=OLD, SUBJECT_KEYS={"2027": NEW}, SUBJECT_KEY_ID="default"
    )
    assert list(back.subject_keys) == ["default", "2027"]


@pytest.mark.parametrize(
    ("given", "message"),
    [
        ({"SUBJECT_KEY": OLD, "SUBJECT_KEYS": {"2027": NEW}}, "must name the current"),
        (
            {"SUBJECT_KEYS": {"2027": NEW}, "SUBJECT_KEY_ID": "2026"},
            "'2026' names no key",
        ),
        ({"SUBJECT_KEYS": {"default": NEW}}, "may not use the id 'default'"),
        ({"SUBJECT_KEYS": {"2027": "short"}}, "at least 32"),
        ({"SUBJECT_KEYS": {"bad id": NEW}}, "pattern"),
    ],
)
def test_a_ring_without_a_clear_current_key_is_refused(
    given: dict[str, object], message: str
) -> None:
    """Several keys need ``SUBJECT_KEY_ID``, naming one of them."""
    with pytest.raises(ValidationError, match=message):
        EvidenceSettings(**given)


@pytest.mark.anyio
async def test_the_configured_store_uses_the_ring() -> None:
    """``evidence_store()`` keys new records with the current key, finds the rest."""
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[EVIDENCE],
        ALPHA=UNSET,
        LEDGERS={"evidence": {"URL": "memory://"}},
        EVIDENCE={
            "SUBJECT_KEY": OLD,
            "SUBJECT_KEYS": {"2027": NEW},
            "SUBJECT_KEY_ID": "2027",
        },
    ):
        setup()
        configured = evidence_store()
        assert isinstance(configured, LedgerEvidenceStore)
        assert configured.subject_key == NEW.encode()
        assert configured.previous_subject_keys == (OLD.encode(),)
        await ledgers.close_all()
    registry.clear()


def test_subject_keys_alone_satisfy_a_deployment() -> None:
    """``SUBJECT_KEYS`` without ``SUBJECT_KEY`` is a subject key where deployed."""
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[EVIDENCE],
        ALPHA=UNSET,
        ENVIRONMENT="production",
        ALLOWED_HOSTS=["api.example.com"],
        DATABASES={
            "default": {"URL": "postgresql+asyncpg://db/app", "SSLMODE": "verify-full"}
        },
        LEDGERS={
            "evidence": {
                "URL": "immudb://writer:pw@ledger.internal:3322/evidence",
                "TLS_CA": "/ca.pem",
                "SERVER_SIGNING_KEY": "/k.pub",
            }
        },
        EVIDENCE={"SUBJECT_KEYS": {"2027": NEW}},
    ):
        setup()
    registry.clear()
