"""The keyed request running now, and whether its changes committed.

``IdempotencyMiddleware`` runs each keyed request as an operation. When a SQL
unit of work commits during it, the key is marked committed: in the same
transaction when the unit of work uses ``IDEMPOTENCY.DATABASE``, right after
the commit otherwise. A committed key is never released, so a retry cannot
run the changes twice.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import Update, update

from .tables import idempotency_keys


@dataclass(slots=True)
class Operation:
    """A request running under an ``Idempotency-Key``."""

    key_hash: str
    committed: bool = False
    """Whether a SQL unit of work committed during the request."""


_running: Final[ContextVar[Operation | None]] = ContextVar(
    "idempotency_operation", default=None
)


@contextmanager
def run_operation(key_hash: str) -> Iterator[Operation]:
    """Run the code inside as the request keyed by ``key_hash``."""
    operation = Operation(key_hash)
    token = _running.set(operation)
    try:
        yield operation
    finally:
        _running.reset(token)


def running_operation() -> Operation | None:
    """Return the keyed request running in this context, if any."""
    return _running.get()


def mark_committed(key_hash: str) -> Update:
    """Record that the key's request committed changes."""
    return (
        update(idempotency_keys)
        .where(idempotency_keys.c.key_hash == key_hash)
        .values(committed_at=datetime.now(UTC))
    )
