"""Seal, and anchor, the audit trail on a schedule (needs messaging)."""

from domainkit.apps import registry
from domainkit.contrib.messaging import periodic

from .seals import seal_all
from .settings import AuditSettings


@periodic(
    every=registry.settings(AuditSettings).SEAL_EVERY, name="domainkit.audit.seal"
)
async def seal_trail() -> None:
    """Seal new rows in every audited database, then anchor the heads."""
    await seal_all()
