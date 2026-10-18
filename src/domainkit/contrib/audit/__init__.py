"""An append-only audit trail: who did what, when, from where.

Every committed domain event becomes an audit row in the same transaction,
attributed to the request's actor. Record anything else (refused logins,
reads of sensitive data) with ``record``::

    await record("auth.login", Outcome.DENIED, reason="mfa")

Personal data is kept only as pseudonyms of the person it is about, and
``forget`` makes a person's pseudonyms unlinkable for good::

    await record("auth.login", Outcome.DENIED, personal={"username": username})
    await forget(str(user.id))

The application only inserts rows; ``protection.protect`` makes the table
append-only in PostgreSQL itself, from a migration. Rows are sealed into a
hash chain whose head is anchored on a ledger, so ``verify`` finds rows
changed or removed even by the table's owner (see ``seals``).
"""

from .personal import forget, forget_anonymous, pseudonym_of
from .recorder import Outcome, action_for, record
from .seals import seal, seal_all, verify
from .settings import AuditSettings

__all__ = [
    "AuditSettings",
    "Outcome",
    "action_for",
    "forget",
    "forget_anonymous",
    "pseudonym_of",
    "record",
    "seal",
    "seal_all",
    "verify",
]
