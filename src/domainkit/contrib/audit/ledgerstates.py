"""Ledger states kept in ``AUDIT.DATABASE``, for every process and host to start from.

``domainkit.ledger.trust`` keeps the state a ledger last proved to a
client; installed, the audit app keeps it here (``AuditConfig.ready``), so
a new process, a replaced container, or another replica refuses a ledger
rolled back below what any of them verified. A save replaces only the
state the saver expects (compare-and-set, one statement), so no replica
overwrites a state another proved since (SEC-10b-5).
"""

from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects import postgresql, sqlite

from domainkit import clock
from domainkit.db import connections
from domainkit.ledger.trust import StateStore, TrustedState

from .tables import ledger_states

__all__ = [
    "DatabaseStateStore",
]


class DatabaseStateStore(StateStore):
    """Trusted ledger states, in ``domainkit_ledger_states`` on alias ``database``."""

    def __init__(self, database: str) -> None:
        """Keep the states on alias ``database``."""
        self.database = database

    async def load(self, ledger: str) -> TrustedState | None:
        """The state kept for ``ledger``."""
        async with connections.engine(self.database).connect() as connection:
            row = (
                await connection.execute(
                    select(ledger_states).where(ledger_states.c.ledger == ledger)
                )
            ).first()
        if row is None:
            return None
        return TrustedState(
            row.database,
            row.transaction_id,
            bytes(row.digest),
            bytes(row.signature),
            bytes(row.public_key),
        )

    async def save(
        self, ledger: str, state: TrustedState, *, expected: TrustedState | None
    ) -> bool:
        """Keep ``state`` if the row holds ``expected`` (None: no row), in one step."""
        values = {
            "ledger": ledger,
            "database": state.database,
            "transaction_id": state.transaction,
            "digest": state.digest,
            "signature": state.signature,
            "public_key": state.public_key,
            "saved_at": clock.now(),
        }
        async with connections.engine(self.database).begin() as connection:
            if expected is None:
                insert: Any = (
                    postgresql.insert
                    if connection.dialect.name == "postgresql"
                    else sqlite.insert
                )
                statement = insert(ledger_states).values(values)
                done = await connection.execute(
                    statement.on_conflict_do_nothing(
                        index_elements=[ledger_states.c.ledger]
                    )
                )
            else:
                done = await connection.execute(
                    update(ledger_states)
                    .where(
                        ledger_states.c.ledger == ledger,
                        ledger_states.c.transaction_id == expected.transaction,
                        ledger_states.c.digest == expected.digest,
                    )
                    .values(values)
                )
        return bool(done.rowcount == 1)
