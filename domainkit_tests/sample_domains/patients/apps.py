"""Configuration of the patients sample domain, with its retention."""

from datetime import timedelta

from domainkit.apps import DomainConfig
from domainkit.contrib.privacy import Retention

from .models import Visit
from .tables import start_mappers


class PatientsConfig(DomainConfig):
    """Map the aggregates; keep the access log 30 days and visit notes a year."""

    RETENTION = (
        Retention(
            table="patients_access_log", field="accessed_at", keep=timedelta(days=30)
        ),
        Retention(table="patients_daily_counts", field="day", keep=timedelta(days=7)),
        Retention(
            aggregate=Visit, field="visited_at", keep=timedelta(days=365), erase=True
        ),
    )

    def ready(self) -> None:
        """Map the aggregates."""
        start_mappers()
