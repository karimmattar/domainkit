"""The evidence store port, and its implementation on a ledger."""

import hashlib
import hmac
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
from domainkit.ledger import Ledger, LedgerKeyExists, LedgerKeyNotFound, LedgerTampered

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


def subject_digest(subject: str, key: bytes) -> str:
    """The key part for ``subject``, so keys never show it.

    HMAC-SHA-256 under ``key`` (``EVIDENCE.SUBJECT_KEY``), so nobody without
    the key can find a subject's records by hashing guesses.
    """
    digest = hmac.new(key, subject.encode(), hashlib.sha256)
    return digest.hexdigest()[:_SUBJECT_DIGEST_LENGTH]


def evidence_key(namespace: str, evidence: Evidence, subject_key: bytes) -> str:
    """``<namespace>/<subject digest>/<evidence id>``."""
    return f"{namespace}/{subject_digest(evidence.subject, subject_key)}/{evidence.id}"


class LedgerEvidenceStore(EvidenceStore):
    """Evidence on a ``Ledger``: immudb in deployments, ``MemoryLedger`` in tests."""

    def __init__(
        self, ledger: Ledger, *, namespace: str, max_bytes: int, subject_key: bytes
    ) -> None:
        """Keep records under ``namespace``, refusing ones over ``max_bytes``.

        ``subject_key`` is the secret the subject digests in keys use.
        """
        self.ledger = ledger
        self.namespace = namespace
        self.max_bytes = max_bytes
        self.subject_key = subject_key

    async def record(self, evidence: Evidence) -> EvidenceReceipt:
        """Write the canonical record once; a retry of the same record is a no-op.

        The ledger creates the key only if it is new, so writers racing to
        record the same evidence leave one revision, and all get its receipt.
        """
        body = evidence.canonical()
        if len(body) > self.max_bytes:
            raise ValueError(
                f"Evidence record is {len(body)} bytes; the limit is {self.max_bytes}. "
                "Store documents elsewhere and record their sha256 digests."
            )
        key = evidence_key(self.namespace, evidence, self.subject_key)
        try:
            written = await self.ledger.create(key.encode(), body)
        except LedgerKeyExists:
            return await self._recorded(evidence, key, body)
        except LedgerTampered as exc:
            raise EvidenceTampered(
                f"The ledger failed verification writing {key}."
            ) from exc
        return EvidenceReceipt(evidence.id, key, sha256(body), written.transaction)

    async def _recorded(
        self, evidence: Evidence, key: str, body: bytes
    ) -> EvidenceReceipt:
        """The receipt of ``body``, already under ``key``; another body conflicts."""
        try:
            existing = await self.ledger.read(key.encode(), revision=1)
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
        digest = subject_digest(subject, self.subject_key)
        prefix = f"{self.namespace}/{digest}/".encode()
        keys = await self.ledger.keys(prefix, limit=limit)
        return [await self.get(key.decode()) for key in keys]
