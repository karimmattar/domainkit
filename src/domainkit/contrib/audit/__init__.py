"""An append-only audit trail: who did what, when, from where.

Every committed domain event becomes an audit row in the same transaction,
attributed to the request's actor. Record anything else (refused logins,
reads of sensitive data) with ``record``::

    await record("auth.login", Outcome.DENIED, target=username, reason="mfa")

The application only inserts rows; ``protection.protect`` makes the table
append-only in PostgreSQL itself, from a migration.
"""

from .recorder import Outcome, action_for, record
from .settings import AuditSettings

__all__ = ["AuditSettings", "Outcome", "action_for", "record"]
