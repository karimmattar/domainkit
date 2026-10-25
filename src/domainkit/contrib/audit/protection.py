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
both tables have their triggers, enabled (``baseline``).
"""

from typing import Protocol

from sqlalchemy import Connection, Table

from .tables import audit_log


def trigger_names(table: Table) -> tuple[str, str]:
    """The triggers that protect ``table``: of rows, and of truncation."""
    function = f"{table.name}_append_only"
    return function, f"{function}_truncate"


def statements(table: Table) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The statements that protect ``table``, and those that undo them."""
    function, truncate = trigger_names(table)
    protect = (
        f"""
    CREATE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION '{table.name} is append-only'
            USING ERRCODE = 'insufficient_privilege';
    END
    $$
    """,
        (
            f"CREATE TRIGGER {function} BEFORE UPDATE OR DELETE ON {table.name} "
            f"FOR EACH ROW EXECUTE FUNCTION {function}()"
        ),
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
