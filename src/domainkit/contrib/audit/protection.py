"""Make the audit trail append-only in the database itself (PostgreSQL).

Revoking ``UPDATE`` and ``DELETE`` from the application's role is undone by
the next broad ``GRANT``. These triggers refuse updates, deletes, and
truncation for every role but the table owner, who alone can drop them (for
example, to apply a retention policy). Call them from a project revision::

    from domainkit.contrib.audit.protection import protect, unprotect

    def upgrade() -> None:
        protect(op)

    def downgrade() -> None:
        unprotect(op)

On other databases both do nothing.
"""

from typing import Final, Protocol

from sqlalchemy import Connection

from .tables import audit_log

_FUNCTION: Final = f"{audit_log.name}_append_only"

PROTECT: Final = (
    f"""
    CREATE FUNCTION {_FUNCTION}() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION '{audit_log.name} is append-only'
            USING ERRCODE = 'insufficient_privilege';
    END
    $$
    """,
    (
        f"CREATE TRIGGER {_FUNCTION} BEFORE UPDATE OR DELETE ON {audit_log.name} "
        f"FOR EACH ROW EXECUTE FUNCTION {_FUNCTION}()"
    ),
    (
        f"CREATE TRIGGER {_FUNCTION}_truncate BEFORE TRUNCATE ON {audit_log.name} "
        f"FOR EACH STATEMENT EXECUTE FUNCTION {_FUNCTION}()"
    ),
)
UNPROTECT: Final = (
    f"DROP TRIGGER IF EXISTS {_FUNCTION}_truncate ON {audit_log.name}",
    f"DROP TRIGGER IF EXISTS {_FUNCTION} ON {audit_log.name}",
    f"DROP FUNCTION IF EXISTS {_FUNCTION}()",
)


class Operations(Protocol):
    """The part of ``alembic.op`` used here."""

    def get_bind(self) -> Connection:
        """Return the migration's connection."""
        ...

    def execute(self, sqltext: str) -> object:
        """Run one SQL statement."""
        ...


def protect(op: Operations) -> None:
    """Refuse UPDATE, DELETE, and TRUNCATE on the audit trail."""
    _run(op, PROTECT)


def unprotect(op: Operations) -> None:
    """Remove the protection (downgrades, retention jobs run as the owner)."""
    _run(op, UNPROTECT)


def _run(op: Operations, statements: tuple[str, ...]) -> None:
    """Run ``statements`` on PostgreSQL; elsewhere do nothing."""
    if op.get_bind().dialect.name != "postgresql":
        return
    for statement in statements:
        op.execute(statement)
