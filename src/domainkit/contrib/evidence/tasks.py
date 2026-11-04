"""Durable evidence: recorded by the messaging worker once the change commits.

The record waits in the outbox, which the application's database role can
write: it is signed (``SECRET_KEY``), and the worker refuses a record whose
signature does not hold, so a record changed there never reaches the
ledger as if it were the one deferred.
"""

import logging
from typing import Final

from domainkit.apps import registry
from domainkit.contrib.evidence.models import Evidence, EvidenceTampered
from domainkit.contrib.evidence.settings import EvidenceSettings
from domainkit.contrib.evidence.store import evidence_key
from domainkit.contrib.messaging import task
from domainkit.ddd import UnitOfWork
from domainkit.log import SECURITY_LOGGER
from domainkit.signing import BadSignature, Signer

__all__ = ["defer_evidence"]

RECORD_TASK = "domainkit.evidence.record"
SALT: Final = "domainkit.evidence.deferred"
"""What deferred records are signed for: no other signed value verifies."""
security = logging.getLogger(SECURITY_LOGGER)


@task(name=RECORD_TASK)
async def record_evidence(record: str) -> None:
    """Write one signed canonical record; retries are harmless (writes are idempotent).

    Raises:
        EvidenceTampered: The record is not the one deferred: its signature
            does not hold (it was changed in the outbox, or never signed).
    """
    from domainkit.contrib.evidence import evidence_store

    try:
        canonical = Signer(salt=SALT).unsign(record)
    except BadSignature as exc:
        security.error("A deferred evidence record was changed before recording.")
        raise EvidenceTampered("A deferred record's signature does not hold.") from exc
    await evidence_store().record(Evidence.from_canonical(canonical.encode()))


def defer_evidence(uow: UnitOfWork, evidence: Evidence) -> str:
    """Record ``evidence`` after ``uow`` commits, at least once; return its key.

    Store the key with the business change, so the evidence can be read and
    verified later (``EvidenceStore.get``).
    """
    uow.defer(
        record_evidence,
        record=Signer(salt=SALT).sign(evidence.canonical().decode()),
        dedupe_key=f"evidence:{evidence.id}",
    )
    config = registry.settings(EvidenceSettings)
    return evidence_key(config.NAMESPACE, evidence, config.subject_key)
