"""The evidence store port, and its implementation on a ledger."""

import hashlib
from abc import ABC, abstractmethod
from typing import Final

from domainkit.contrib.evidence.model import (
    Evidence,
    EvidenceConflict,
    EvidenceNotFound,
    EvidenceReceipt,
    EvidenceTampered,
    RecordedEvidence,
    sha256,
)
from domainkit.ledger import Ledger, LedgerKeyNotFound, LedgerTampered

_SUBJECT_DIGEST_LENGTH: Final = 32
_TAMPER_CHECK_LIMIT: Final = 2


class EvidenceStore(ABC):
    """Record evidence once; read it back verified (the application's port)."""

    @abstractmethod
    async def record(self, evidence: Evidence) -> EvidenceReceipt:
        """Store ``evidence``; recording the same evidence again returns its receipt."""

    @abstractmethod
    async def get(self, key: str) -> RecordedEvidence:
        """Read the evidence under ``key``; raises ``EvidenceNotFound``."""

    @abstractmethod
    async def for_subject(
        self, subject: str, *, limit: int = 100
    ) -> list[RecordedEvidence]:
        """Evidence about ``subject``, at most ``limit`` records."""

    async def verify(self, receipt: EvidenceReceipt) -> RecordedEvidence:
        """Read the evidence of ``receipt`` and prove it is unchanged.

        Raises ``EvidenceTampered`` if the stored record differs from the
        receipt, or its key was written again after it was recorded.
        """
        recorded = await self.get(receipt.key)
        if (recorded.receipt.id, recorded.receipt.digest) != (
            receipt.id,
            receipt.digest,
        ):
            raise EvidenceTampered(
                f"Evidence {receipt.key} does not match its receipt."
            )
        if not recorded.intact:
            raise EvidenceTampered(
                f"Evidence {receipt.key} was written again after it was recorded."
            )
        return recorded


def subject_digest(subject: str) -> str:
    """The key part for ``subject``: a digest, so keys never show personal data."""
    return hashlib.sha256(subject.encode()).hexdigest()[:_SUBJECT_DIGEST_LENGTH]


def evidence_key(namespace: str, evidence: Evidence) -> str:
    """``<namespace>/<subject digest>/<evidence id>``."""
    return f"{namespace}/{subject_digest(evidence.subject)}/{evidence.id}"


class LedgerEvidenceStore(EvidenceStore):
    """Evidence on a ``Ledger``: immudb in deployments, ``MemoryLedger`` in tests."""

    def __init__(self, ledger: Ledger, *, namespace: str, max_bytes: int) -> None:
        """Keep records under ``namespace``, refusing ones over ``max_bytes``."""
        self.ledger = ledger
        self.namespace = namespace
        self.max_bytes = max_bytes

    async def record(self, evidence: Evidence) -> EvidenceReceipt:
        """Write the canonical record once; a retry of the same record is a no-op."""
        body = evidence.canonical()
        if len(body) > self.max_bytes:
            raise ValueError(
                f"Evidence record is {len(body)} bytes; the limit is {self.max_bytes}. "
                "Store documents elsewhere and record their sha256 digests."
            )
        key = evidence_key(self.namespace, evidence)
        try:
            existing = await self.ledger.read(key.encode(), revision=1)
        except LedgerKeyNotFound:
            written = await self.ledger.append(key.encode(), body)
            return EvidenceReceipt(evidence.id, key, sha256(body), written.transaction)
        except LedgerTampered as exc:
            raise EvidenceTampered(
                f"The ledger failed verification reading {key}."
            ) from exc
        if existing.value != body:
            raise EvidenceConflict(
                f"Evidence {evidence.id} already holds another record."
            )
        return EvidenceReceipt(evidence.id, key, sha256(body), existing.transaction)

    async def get(self, key: str) -> RecordedEvidence:
        """Read revision 1, the original, and count the key's revisions."""
        try:
            entry = await self.ledger.read(key.encode(), revision=1)
            revisions = await self.ledger.revision_count(
                key.encode(), limit=_TAMPER_CHECK_LIMIT
            )
        except LedgerKeyNotFound as exc:
            raise EvidenceNotFound(f"No evidence under {key}.") from exc
        except LedgerTampered as exc:
            raise EvidenceTampered(
                f"The ledger failed verification reading {key}."
            ) from exc
        try:
            evidence = Evidence.from_canonical(entry.value)
        except (ValueError, KeyError, TypeError) as exc:
            raise EvidenceTampered(f"{key} does not hold an evidence record.") from exc
        receipt = EvidenceReceipt(
            evidence.id, key, sha256(entry.value), entry.transaction
        )
        return RecordedEvidence(evidence, receipt, revisions)

    async def for_subject(
        self, subject: str, *, limit: int = 100
    ) -> list[RecordedEvidence]:
        """Scan the subject's key prefix and read each record."""
        prefix = f"{self.namespace}/{subject_digest(subject)}/".encode()
        keys = await self.ledger.keys(prefix, limit=limit)
        return [await self.get(key.decode()) for key in keys]
