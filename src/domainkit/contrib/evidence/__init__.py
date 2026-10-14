"""Evidence: facts kept on a ledger, provably unchanged (see ADR 0011).

Install ``domainkit.contrib.evidence``, configure a ledger, and record::

    LEDGERS = {"evidence": {"URL": "immudb://user:pw@immudb:3322/evidence"}}

    store = evidence_store()
    receipt = await store.record(
        Evidence(kind="kyc.document_verified", subject="customer:42",
                 facts={"result": "match"}, artifacts={"passport": sha256(data)})
    )
    await store.verify(receipt)

Inside a use case, ``defer_evidence(uow, evidence)`` records it through the
outbox once the change commits.
"""

from domainkit.apps import registry
from domainkit.contrib.evidence.model import (
    Evidence,
    EvidenceConflict,
    EvidenceNotFound,
    EvidenceReceipt,
    EvidenceTampered,
    RecordedEvidence,
    sha256,
)
from domainkit.contrib.evidence.settings import EvidenceSettings
from domainkit.contrib.evidence.store import (
    EvidenceStore,
    LedgerEvidenceStore,
    evidence_key,
    subject_digest,
)
from domainkit.ledger import ledgers


def evidence_store() -> EvidenceStore:
    """The configured store, on the ledger ``EVIDENCE.LEDGER`` names."""
    config = registry.settings(EvidenceSettings)
    return LedgerEvidenceStore(
        ledgers[config.LEDGER], namespace=config.NAMESPACE, max_bytes=config.MAX_BYTES
    )


__all__ = [
    "Evidence",
    "EvidenceConflict",
    "EvidenceNotFound",
    "EvidenceReceipt",
    "EvidenceSettings",
    "EvidenceStore",
    "EvidenceTampered",
    "LedgerEvidenceStore",
    "RecordedEvidence",
    "evidence_key",
    "evidence_store",
    "sha256",
    "subject_digest",
]
