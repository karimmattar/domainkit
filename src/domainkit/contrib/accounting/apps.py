"""Install ``domainkit.contrib.accounting`` to keep double-entry books."""

from collections.abc import Mapping
from typing import Final

from domainkit.apps import DomainConfig, registry
from domainkit.apps.hooks import (
    on_deployment_warnings,
    on_erase_subject,
    on_export_subject,
    on_startup,
)
from domainkit.checks import CheckMessage
from domainkit.conf import settings
from domainkit.conf.global_settings import DEPLOYED_ENVIRONMENTS
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
        on_deployment_warnings(self.weak_settings)
        if registry.is_installed(MESSAGING):
            from .tasks import register

            register(registry.settings(AccountingSettings))

    def weak_settings(self) -> list[str]:
        """``accounting.W003``: holds that expire with no worker to release them.

        Without messaging nothing releases an expired hold on a schedule,
        and until then it counts against what its account has available.
        A project that runs ``manage.py accounting expireholds`` from its
        own scheduler silences it.
        """
        if settings.ENVIRONMENT not in DEPLOYED_ENVIRONMENTS or registry.is_installed(
            MESSAGING
        ):
            return []
        return [
            CheckMessage(
                "accounting.W003",
                f"Expired holds are not released on a schedule: {MESSAGING} is "
                "not installed, so no worker runs the expiry, and an expired "
                "hold keeps counting against what its account has available.",
                hint=f"Install {MESSAGING}, or run 'manage.py accounting "
                "expireholds' from a scheduler and list accounting.W003 in "
                "SILENCED_CHECKS.",
            )
        ]

    async def erase_holder(self, subject: str, reason: str) -> Mapping[str, int]:
        """Unlink ``subject`` from the accounts kept for them; the books stay."""
        from .personal import erase_subject

        return await erase_subject(subject)

    async def export_holder(self, subject: str) -> Mapping[str, object]:
        """The accounts kept for ``subject``, with their postings."""
        from .personal import export_subject

        return await export_subject(subject)
