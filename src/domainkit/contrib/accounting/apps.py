"""Install ``domainkit.contrib.accounting`` to keep double-entry books."""

from collections.abc import Mapping
from typing import Final

from domainkit.apps import DomainConfig, registry
from domainkit.apps.hooks import on_erase_subject, on_export_subject, on_startup
from domainkit.db.hooks import on_migrated

from .settings import AccountingSettings

MESSAGING: Final = "domainkit.contrib.messaging"


class AccountingConfig(DomainConfig):
    """A double-entry ledger: accounts, entries, holds, periods, reconciliation."""

    label = "accounting"
    verbose_name = "Accounting"
    settings_class = AccountingSettings
    requires = ()

    def ready(self) -> None:
        """Check the routing and the rules; map the tables; register the hooks.

        The ledger lives in one database (``DATABASE_ROUTING['accounting']``,
        never a shard group). Its matching rules are imported now, so a
        wrong path fails ``setup()``. With messaging, entries can be posted
        from messages and holds expire on a schedule (``tasks``).
        """
        from .protection import check_protection, protect
        from .reconciliation import configured_rules
        from .tables import start_mappers
        from .uow import accounting_alias

        accounting_alias()
        configured_rules()
        start_mappers()
        on_migrated(protect)
        on_startup(check_protection)
        on_erase_subject(self.erase_holder)
        on_export_subject(self.export_holder)
        if registry.is_installed(MESSAGING):
            from .tasks import register

            register(registry.settings(AccountingSettings))

    async def erase_holder(self, subject: str, reason: str) -> Mapping[str, int]:
        """Unlink ``subject`` from the accounts kept for them; the books stay."""
        from .personal import erase_subject

        return await erase_subject(subject)

    async def export_holder(self, subject: str) -> Mapping[str, object]:
        """The accounts kept for ``subject``, with their postings."""
        from .personal import export_subject

        return await export_subject(subject)
