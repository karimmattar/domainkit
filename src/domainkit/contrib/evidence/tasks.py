"""Durable evidence: recorded by the messaging worker once the change commits."""

from domainkit.apps import registry
from domainkit.contrib.evidence.models import Evidence
from domainkit.contrib.evidence.settings import EvidenceSettings
from domainkit.contrib.evidence.store import evidence_key
from domainkit.contrib.messaging import task
from domainkit.ddd import UnitOfWork

RECORD_TASK = "domainkit.evidence.record"


@task(name=RECORD_TASK)
async def record_evidence(record: str) -> None:
    """Write one canonical record; retries are harmless (writes are idempotent)."""
    from domainkit.contrib.evidence import evidence_store

    await evidence_store().record(Evidence.from_canonical(record.encode()))


def defer_evidence(uow: UnitOfWork, evidence: Evidence) -> str:
    """Record ``evidence`` after ``uow`` commits, at least once; return its key.

    Store the key with the business change, so the evidence can be read and
    verified later (``EvidenceStore.get``).
    """
    uow.defer(
        record_evidence,
        record=evidence.canonical().decode(),
        dedupe_key=f"evidence:{evidence.id}",
    )
    config = registry.settings(EvidenceSettings)
    return evidence_key(config.NAMESPACE, evidence, config.subject_key)
