"""Settings of ``domainkit.contrib.audit``: ``AUDIT = {...}``."""

from pydantic import BaseModel, ConfigDict

from domainkit.conf import DEFAULT_DB_ALIAS


class AuditSettings(BaseModel):
    """What to record; every key is optional."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    DATABASE: str = DEFAULT_DB_ALIAS
    """Alias holding the audit trail; event audits need units of work on it."""
    EVENTS: bool = True
    """Record every committed domain event, in the same transaction."""
    EXCLUDE: tuple[str, ...] = ()
    """Actions to skip, as shell patterns (``catalog.*``, ``*.viewed``)."""
