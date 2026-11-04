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

from .tables import audit_checkpoints, audit_log, audit_seals, ledger_states

__all__ = [
    "protect",
    "protect_migrated",
    "unprotect",
]

ROW_TRIGGER: Final = 1 | 2 | 8 | 16
"""``pg_trigger.tgtype`` of ``BEFORE UPDATE OR DELETE ... FOR EACH ROW``.

The bits are ``ROW`` (1), ``BEFORE`` (2), ``DELETE`` (8), and ``UPDATE``
(16), as PostgreSQL's ``catalog/pg_trigger.h`` defines them."""
TRUNCATE_TRIGGER: Final = 2 | 32
"""``pg_trigger.tgtype`` of ``BEFORE TRUNCATE ... FOR EACH STATEMENT``
(``BEFORE``, 2, and ``TRUNCATE``, 32)."""


def trigger_names(table: Table) -> tuple[str, str]:
    """The triggers that protect ``table``: of rows, and of truncation."""
    function = f"{table.name}_append_only"
    return function, f"{function}_truncate"


def function_body(table: Table) -> str:
    """The body of the triggers' function for ``table``, as ``protect`` writes it."""
    return f"""
    BEGIN
        RAISE EXCEPTION '{table.name} is append-only'
            USING ERRCODE = 'insufficient_privilege';
    END
    """


def statements(table: Table) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The statements that protect ``table``, and those that undo them.

    Protecting replaces what is there: the function, and each trigger
    dropped and made again, so a trigger changed in place (disabled, under
    a ``WHEN``, running another function) is put back as it must be.
    """
    function, truncate = trigger_names(table)
    body = function_body(table)
    protect = (
        f"""
    CREATE OR REPLACE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql
    AS $${body}$$
    """,
        f"DROP TRIGGER IF EXISTS {function} ON {table.name}",
        (
            f"CREATE TRIGGER {function} BEFORE UPDATE OR DELETE ON {table.name} "
            f"FOR EACH ROW EXECUTE FUNCTION {function}()"
        ),
        f"DROP TRIGGER IF EXISTS {truncate} ON {table.name}",
        (
            f"CREATE TRIGGER {truncate} BEFORE TRUNCATE ON {table.name} "
            f"FOR EACH STATEMENT EXECUTE FUNCTION {function}()"
        ),
    )
    unprotect = (
        f"DROP TRIGGER IF EXISTS {truncate} ON {table.name}",
        f"DROP TRIGGER IF EXISTS {function} ON {table.name}",
        f"DROP FUNCTION IF EXISTS {function}()",
    )
    return protect, unprotect


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
    present = {
        table.name
        for table in (audit_log, audit_seals, audit_checkpoints, ledger_states)
        if await connection.scalar(
            text("SELECT to_regclass(:name)"), {"name": table.name}
        )
        is not None
    }
    for table in (audit_log, audit_seals, audit_checkpoints):
        if table.name in present:
            for statement in statements(table)[0]:
                await connection.exec_driver_sql(statement)
    if ledger_states.name in present:
        for statement in advance_only():
            await connection.exec_driver_sql(statement)
