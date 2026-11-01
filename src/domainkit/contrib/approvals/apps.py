"""Install ``domainkit.contrib.approvals`` for maker-checker on guarded commands."""

from collections.abc import Mapping
from typing import Final

from domainkit.apps import DomainConfig, Requirement, registry
from domainkit.apps.hooks import on_erase_subject, on_export_subject

from .settings import ApprovalsSettings

ACCOUNTING: Final = "domainkit.contrib.accounting"
AUTH: Final = "domainkit.contrib.auth"


class ApprovalsConfig(DomainConfig):
    """Pending commands that run once people other than their maker approve them."""

    label = "approvals"
    verbose_name = "Approvals"
    settings_class = ApprovalsSettings
    requires = (
        Requirement(
            "domainkit.contrib.messaging", "approved commands run in the worker"
        ),
        Requirement("domainkit.contrib.audit", "every decision is audited"),
    )

    def ready(self) -> None:
        """Check the routing and the approve rule; map the tables; register.

        The requests live in one database (``DATABASE_ROUTING['approvals']``).
        ``APPROVE_RULE`` is imported now, so a wrong path fails ``setup()``.
        The guards of large postings and of making superusers are
        registered when their apps are installed and their settings ask.
        """
        from .builtin import guard_entries, guard_superusers
        from .rules import configured_rule
        from .tables import start_mappers
        from .tasks import register
        from .uow import approvals_alias

        config = registry.settings(ApprovalsSettings)
        approvals_alias()
        configured_rule()
        start_mappers()
        register(config)
        on_erase_subject(self.erase_person)
        on_export_subject(self.export_person)
        if config.ENTRY_THRESHOLDS and registry.is_installed(ACCOUNTING):
            guard_entries(config)
        if config.SUPERUSER_APPROVALS and registry.is_installed(AUTH):
            guard_superusers(config)

    async def erase_person(self, subject: str, reason: str) -> Mapping[str, int]:
        """Unlink ``subject`` from the requests they made and decided."""
        from .personal import erase_subject

        return await erase_subject(subject)

    async def export_person(self, subject: str) -> Mapping[str, object]:
        """The requests ``subject`` made, and their decisions."""
        from .personal import export_subject

        return await export_subject(subject)
