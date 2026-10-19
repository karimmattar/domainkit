"""The audit trail as content types see it: a model that carries a permission."""

from uuid import UUID

from domainkit.ddd import Entity


class AuditEntry(Entity[UUID]):
    """One row of the audit trail; mapped only so it can hold a permission.

    ``audit.view_history`` lets staff read records' histories over HTTP
    (``domainkit.contrib.audit.api``). Rows are never added, changed, or
    deleted through it, so it has no other permission.
    """

    class Meta:
        """One permission, to read histories."""

        verbose_name = "audit entry"
        default_permissions = ()
        permissions = (("view_history", "Can view the history of records"),)
