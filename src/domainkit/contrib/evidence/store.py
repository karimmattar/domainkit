"""The evidence store port, and its implementation on a ledger."""

import hashlib
import hmac
import re
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Final

from domainkit.contrib.evidence.models import (
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
_KEY: Final = re.compile(
    rf"(?P<namespace>[^/]+)/[0-9a-f]{{{_SUBJECT_DIGEST_LENGTH}}}/[^/]+"
)
"""An evidence key: ``<namespace>/<subject digest>/<evidence id>``."""


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
        self,
        ledger: Ledger,
        *,
        namespace: str,
        max_bytes: int,
        subject_key: bytes,
        previous_namespaces: Sequence[str] = (),
        previous_subject_keys: Sequence[bytes] = (),
    ) -> None:
        """Keep records under ``namespace``, refusing ones over ``max_bytes``.

        ``subject_key`` is the secret the subject digests in new keys use;
        ``previous_subject_keys`` made earlier records' keys, which
        ``for_subject`` still finds. ``get`` reads keys under ``namespace``
        and ``previous_namespaces`` only.
        """
        self.ledger = ledger
        self.namespace = namespace
        self.previous_namespaces = tuple(previous_namespaces)
        self.max_bytes = max_bytes
        self.subject_key = subject_key
        self.previous_subject_keys = tuple(previous_subject_keys)

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
        """Read the latest revision, verified, and revision 1, the original.

        Both reads are proven by the ledger, so the count of revisions
        (``intact``) rests on a proof too: the latest one's number, not an
        unverified list of the key's history. A key written once is read
        once. Only evidence keys under the store's namespace, or one of its
        previous namespaces, are read: any other key, such as another
        project's on a shared ledger or an audit anchor, is not found. A
        record must end its own key: one copied under another key is
        tampering. The subject digest is not checked, since a new
        ``SUBJECT_KEY`` leaves earlier keys valid.
        """
        if not self._owns(key):
            raise EvidenceNotFound(f"No evidence under {key}.")
        try:
            latest = await self.ledger.read(key.encode())
            entry = (
                latest
                if latest.revision == 1
                else await self.ledger.read(key.encode(), revision=1)
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
        if not key.endswith(f"/{evidence.id}"):
            raise EvidenceTampered(f"{key} holds the record of evidence {evidence.id}.")
        receipt = EvidenceReceipt(
            evidence.id, key, sha256(entry.value), entry.transaction
        )
        return RecordedEvidence(evidence, receipt, latest.revision)

    def _owns(self, key: str) -> bool:
        """Whether ``key`` is an evidence key of this store's namespaces."""
        shape = _KEY.fullmatch(key)
        return shape is not None and shape["namespace"] in {
            self.namespace,
            *self.previous_namespaces,
        }

    async def for_subject(
        self, subject: str, *, limit: int = 100
    ) -> list[RecordedEvidence]:
        """Scan the subject's key prefix under each subject key; read each record.

        The current key's prefix first, then each previous key's, until
        ``limit`` records are found. Every record found must be about
        ``subject``: one filed under the subject's prefix but about someone
        else is tampering.
        """
        keys: list[bytes] = []
        for secret in (self.subject_key, *self.previous_subject_keys):
            if len(keys) >= limit:
                break
            digest = subject_digest(subject, secret)
            prefix = f"{self.namespace}/{digest}/".encode()
            keys += await self.ledger.keys(prefix, limit=limit - len(keys))
        recorded = [await self.get(key.decode()) for key in keys]
        for found in recorded:
            if found.evidence.subject != subject:
                raise EvidenceTampered(
                    f"{found.receipt.key} is filed under the subject of another record."
                )
        return recorded
