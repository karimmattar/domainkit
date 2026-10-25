"""The clock that decides when outbox work is due and when locks expire.

Workers on several hosts share an outbox, so on PostgreSQL its own server
decides (ADR 0043). This is ``domainkit.db.dbclock``, which idempotency
keys share; the names stay here for the outbox's code.
"""

from domainkit.db.dbclock import Moment, at, now, uses_database

__all__ = ["Moment", "at", "now", "uses_database"]
