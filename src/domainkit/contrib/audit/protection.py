"""Make the audit trail append-only in the database itself (PostgreSQL).

Revoking ``UPDATE`` and ``DELETE`` from the application's role is undone by
the next broad ``GRANT``. These triggers refuse updates, deletes, and
truncation for every role, the table owner included; only the owner can drop
them (for example, to apply a retention policy). Call them from a project
revision::

    from domainkit.contrib.audit.protection import protect, unprotect

    def upgrade() -> None:
        protect(op)

    def downgrade() -> None:
        unprotect(op)

Protect the seals of the hash chain the same way, in the revision that
creates them: ``protect(op, audit_seals)``. On other databases both do
nothing. In staging and production the application checks at startup that
both tables have their triggers, enabled, firing when and as these make
them, and running the function these write (``baseline``).

``migrate`` makes them itself: after each upgrade, ``protect_migrated``
makes the function and the triggers of both tables afresh on the schema
owner's connection, as accounting's are made, so no revision of the
project's needs to call ``protect``. Calling it there too does no harm:
it replaces what is there.
"""

from typing import Final, Protocol

from sqlalchemy import Connection, Table, text
from sqlalchemy.ext.asyncio import AsyncConnection

from domainkit.db.protection import ROW_TRIGGER as ROW_TRIGGER
from domainkit.db.protection import TRUNCATE_TRIGGER as TRUNCATE_TRIGGER
from domainkit.db.protection import append_only_statements, protect_append_only
from domainkit.db.protection import trigger_names as trigger_names

from .tables import audit_checkpoints, audit_log, audit_seals, ledger_states

__all__ = [
    "protect",
    "protect_migrated",
    "unprotect",
]


def statements(table: Table) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The statements that protect ``table``, and those that undo them.

    ``domainkit.db.protection``'s, which the ledger's entries share: the
    function names and bodies are those of 2.8, so triggers made then
    still count as made.
    """
    return append_only_statements(table)


PROTECT, UNPROTECT = statements(audit_log)
"""The audit trail's protection, and its removal."""

ADVANCE_ONLY: Final = "domainkit_ledger_states_advance_only"
"""The function of the triggers that keep each ledger's saved state moving forward."""


def advance_only() -> tuple[str, ...]:
    """The statements that keep ``ledger_states`` moving forward only.

    A state is saved again as its ledger moves on, so updates are allowed,
    but only to the same ledger at the same or a later transaction: a
    deleted or rolled-back state would reset the rollback detection it
    exists for.
    """
    table = ledger_states.name
    return (
        f"""
    CREATE OR REPLACE FUNCTION {ADVANCE_ONLY}() RETURNS trigger LANGUAGE plpgsql
    AS $$
    BEGIN
        IF TG_OP = 'UPDATE' AND NEW.ledger = OLD.ledger
                AND NEW.transaction_id >= OLD.transaction_id THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION '{table} only moves forward'
            USING ERRCODE = 'insufficient_privilege';
    END
    $$
    """,
        f"DROP TRIGGER IF EXISTS {ADVANCE_ONLY} ON {table}",
        (
            f"CREATE TRIGGER {ADVANCE_ONLY} BEFORE UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION {ADVANCE_ONLY}()"
        ),
        f"DROP TRIGGER IF EXISTS {ADVANCE_ONLY}_truncate ON {table}",
        (
            f"CREATE TRIGGER {ADVANCE_ONLY}_truncate BEFORE TRUNCATE ON {table} "
            f"FOR EACH STATEMENT EXECUTE FUNCTION {ADVANCE_ONLY}()"
        ),
    )


class Operations(Protocol):
    """The part of ``alembic.op`` used here."""

    def get_bind(self) -> Connection:
        """Return the migration's connection."""
        ...

    def execute(self, sqltext: str) -> object:
        """Run one SQL statement."""
        ...


def protect(op: Operations, table: Table = audit_log) -> None:
    """Refuse UPDATE, DELETE, and TRUNCATE on ``table`` (the audit trail)."""
    _run(op, statements(table)[0])


def unprotect(op: Operations, table: Table = audit_log) -> None:
    """Remove the protection (downgrades, retention jobs run as the owner)."""
    _run(op, statements(table)[1])


def _run(op: Operations, statements: tuple[str, ...]) -> None:
    """Run ``statements`` on PostgreSQL; elsewhere do nothing."""
    if op.get_bind().dialect.name != "postgresql":
        return
    for statement in statements:
        op.execute(statement)


async def protect_migrated(connection: AsyncConnection, database: str) -> None:
    """After ``migrate``: protect the trail, its seals, and its checkpoints afresh.

    Each ledger's saved state may only move forward (``advance_only``).
    Only on PostgreSQL, and only the tables ``database`` holds where the
    connection works (its ``search_path``: a tenant's schema, after
    ``migrate --tenant``): ``makemigrations`` may not have written them yet
    (``on_migrated``).
    """
    if connection.dialect.name != "postgresql":
        return
    await protect_append_only(connection, (audit_log, audit_seals, audit_checkpoints))
    found = text("SELECT to_regclass(:name)")
    if await connection.scalar(found, {"name": ledger_states.name}) is not None:
        for statement in advance_only():
            await connection.exec_driver_sql(statement)
