"""Ledger states kept in ``AUDIT.DATABASE``, for every process and host to start from.

``domainkit.ledger.trust`` keeps the state a ledger last proved to a
client; installed, the audit app keeps it here (``AuditConfig.ready``), so
a new process, a replaced container, or another replica refuses a ledger
rolled back below what any of them verified. A row only moves forward: a
save of an older state than the row's changes nothing.
"""

from typing import Any

from sqlalchemy import select
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

    async def save(self, ledger: str, state: TrustedState) -> None:
        """Keep ``state``, unless the row holds a later one, in one statement."""
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
            insert: Any = (
                postgresql.insert
                if connection.dialect.name == "postgresql"
                else sqlite.insert
            )
            statement = insert(ledger_states).values(values)
            await connection.execute(
                statement.on_conflict_do_update(
                    index_elements=[ledger_states.c.ledger],
                    set_={
                        name: statement.excluded[name]
                        for name in values
                        if name != "ledger"
                    },
                    where=ledger_states.c.transaction_id
                    < statement.excluded.transaction_id,
                )
            )
