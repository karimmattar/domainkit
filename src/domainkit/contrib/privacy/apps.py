"""Install ``domainkit.contrib.privacy`` for privacy requests and retention."""

from fastapi import FastAPI

from domainkit.apps import DomainConfig, registry
from domainkit.db.routing import DatabaseRouter
from domainkit.exceptions import ImproperlyConfigured

from .models import problems

AUDIT = "domainkit.contrib.audit"


class PrivacyConfig(DomainConfig):
    """Export and erase people's data, and apply retention, all audited."""

    label = "privacy"
    verbose_name = "Privacy"

    def ready(self) -> None:
        """Refuse to run without the audit trail, which records every operation."""
        if not registry.is_installed(AUDIT):
            raise ImproperlyConfigured(
                f"domainkit.contrib.privacy records every operation in the audit "
                f"trail: add {AUDIT!r} to INSTALLED_DOMAINS."
            )

    def check_application(self, app: FastAPI) -> list[str]:
        """Markers and retention policies that cannot be applied.

        Checked once every domain has mapped its aggregates; ``check``
        lists the problems, and staging and production refuse to start.
        """
        return problems()

    def databases(self, router: DatabaseRouter) -> frozenset[str]:
        """None: privacy keeps no tables of its own."""
        return frozenset()
