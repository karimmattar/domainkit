"""The keyed request running now, and whether its changes committed.

``IdempotencyMiddleware`` runs each keyed request as an operation. When a SQL
unit of work commits during it, the key is marked committed: in the same
transaction when the unit of work uses ``IDEMPOTENCY.DATABASE``, right after
the commit otherwise. A committed key is never released, so a retry cannot
run the changes twice.

Each operation holds its key under a lease, with a random token. A retry may
take over a key whose lease ran out before anything committed; the key then
carries the retry's token. Marks and results are written only under the
holder's token, so a request that lost its key cannot commit: its unit of
work fails with ``KeyTakenOver``. Before a unit of work on another database
commits, the key is pinned (held until it expires, and marked ``pinned``),
and the operation notes it: from then on the key is never taken over or
released, since those changes may have committed even if their commit
raised (a lost reply, a cancellation while it ran).
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Final

from sqlalchemy import ColumnElement, Update, and_, update

from domainkit import clock
from domainkit.ddd import ConcurrentUpdate

from .tables import idempotency_keys


class KeyTakenOver(ConcurrentUpdate):
    """A retry took the key over after this request's lease ran out."""

    code = "idempotency_key_taken_over"


@dataclass(slots=True)
class Operation:
    """A request running under an ``Idempotency-Key``."""

    key_hash: str
    token: str
    """Proves this request still holds the key."""
    committed: bool = False
    """Whether a SQL unit of work committed during the request."""
    pinned: bool = False
    """Whether the key was pinned for a commit on another database.

    Set before that commit, so it holds whatever the commit reports.
    """

    def holds_key(self) -> ColumnElement[bool]:
        """Match the key's row while this request holds it."""
        return and_(
            idempotency_keys.c.key_hash == self.key_hash,
            idempotency_keys.c.token == self.token,
        )


_running: Final[ContextVar[Operation | None]] = ContextVar(
    "idempotency_operation", default=None
)


@contextmanager
def run_operation(operation: Operation) -> Iterator[Operation]:
    """Run the code inside as ``operation``."""
    reset = _running.set(operation)
    try:
        yield operation
    finally:
        _running.reset(reset)


def running_operation() -> Operation | None:
    """Return the keyed request running in this context, if any."""
    return _running.get()


def pin_lease(operation: Operation) -> Update:
    """Hold the key until it expires, never released, if the operation holds it."""
    return (
        update(idempotency_keys)
        .where(operation.holds_key())
        .values(locked_until=idempotency_keys.c.expires_at, pinned=True)
    )


def mark_committed(operation: Operation) -> Update:
    """Record that the operation committed changes, if it still holds its key."""
    return (
        update(idempotency_keys)
        .where(operation.holds_key())
        .values(committed_at=clock.now())
    )
