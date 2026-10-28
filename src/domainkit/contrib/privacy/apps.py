"""Install ``domainkit.contrib.privacy`` for privacy requests and retention."""

from fastapi import FastAPI

from domainkit.apps import DomainConfig, Requirement
from domainkit.apps.hooks import on_check_application
from domainkit.db.routing import DatabaseRouter

from .models import problems

AUDIT = "domainkit.contrib.audit"


class PrivacyConfig(DomainConfig):
    """Export and erase people's data, and apply retention, all audited."""

    label = "privacy"
    verbose_name = "Privacy"
    requires = (Requirement(AUDIT, "it records every operation in the audit trail"),)

    def ready(self) -> None:
        """Check the markers and retention policies once the app is built."""
        on_check_application(self.application_problems)

    def application_problems(self, app: FastAPI) -> list[str]:
        """Markers and retention policies that cannot be applied.

        Checked once every domain has mapped its aggregates; ``check``
        lists the problems, and staging and production refuse to start.
        """
        return problems()

    def databases(self, router: DatabaseRouter) -> frozenset[str]:
        """None: privacy keeps no tables of its own."""
        return frozenset()
