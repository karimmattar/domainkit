"""Install ``domainkit.contrib.objectpermissions`` for grants on single objects."""

from importlib import import_module

from domainkit.apps import DomainConfig
from domainkit.db.routing import DatabaseRouter

from .tables import start_mappers


class ObjectPermissionsConfig(DomainConfig):
    """Map the grants, and teach the SQL compiler ``VisibleTo``."""

    label = "objectpermissions"
    verbose_name = "Object permissions"

    def ready(self) -> None:
        """Map the aggregate; register ``VisibleTo``'s compiler."""
        start_mappers()
        import_module(f"{self.name}.criteria")

    def databases(self, router: DatabaseRouter) -> frozenset[str]:
        """Every hosting target: grants live beside the objects they are about."""
        return frozenset(router.hosting_targets())
