"""``DomainConfig``: Django's ``AppConfig`` for a bounded context."""

from collections.abc import Sequence
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel

from domainkit.exceptions import ImproperlyConfigured

if TYPE_CHECKING:
    from fastapi import FastAPI
    from sqlalchemy import Table
    from sqlalchemy.ext.asyncio import AsyncConnection

    from domainkit.contrib.privacy import Retention
    from domainkit.db.routing import DatabaseRouter
    from domainkit.ddd import DeferredTask, DomainEvent, UnitOfWork

CONFIG_MODULE = "apps"


class DomainConfig:
    """Describe one installed domain and its lifecycle hooks.

    Each domain package may define exactly one subclass in its ``apps``
    module; a package without one gets this base class. Override only the
    hooks the domain needs.
    """

    label: str = ""
    """Unique short name; defaults to the last part of the package path."""
    verbose_name: str = ""
    settings_class: type[BaseModel] | None = None
    """Schema for this domain's settings, read from ``settings_name``."""
    RETENTION: ClassVar[Sequence["Retention"]] = ()
    """How long rows of its tables are kept (``domainkit.contrib.privacy``)."""

    def __init__(self, name: str, module: ModuleType) -> None:
        """Bind the config to the domain package ``name``."""
        self.name = name
        self.module = module
        self.label = self.label or name.rpartition(".")[2]
        if not self.label.isidentifier():
            raise ImproperlyConfigured(
                f"Domain label {self.label!r} is not a valid Python identifier."
            )
        self.verbose_name = self.verbose_name or self.label.replace("_", " ").title()

    def __repr__(self) -> str:
        """Show the class and the domain label."""
        return f"<{type(self).__name__}: {self.label}>"

    @property
    def settings_name(self) -> str:
        """Settings-module name holding this domain's settings, e.g. ``BILLING``."""
        return self.label.upper()

    @property
    def path(self) -> Path:
        """Directory of the domain package."""
        return Path(next(iter(self.module.__path__)))

    @classmethod
    def create(cls, name: str) -> "DomainConfig":
        """Import package ``name`` and instantiate its ``DomainConfig``."""
        module = _import_domain(name)
        config_module_name = f"{name}.{CONFIG_MODULE}"
        try:
            config_module = import_module(config_module_name)
        except ModuleNotFoundError as exc:
            if exc.name != config_module_name:
                raise
            return cls(name, module)
        configs = [
            value
            for value in vars(config_module).values()
            if isinstance(value, type)
            and issubclass(value, DomainConfig)
            and value.__module__ == config_module_name
        ]
        if len(configs) != 1:
            raise ImproperlyConfigured(
                f"{config_module_name!r} must define exactly one DomainConfig "
                f"subclass, found {len(configs)}."
            )
        return configs[0](name, module)

    def ready(self) -> None:
        """Wire the domain once settings are loaded: ORM mappers, event handlers."""

    async def startup(self) -> None:
        """Open process resources such as clients and pools."""

    async def shutdown(self) -> None:
        """Release resources opened by ``startup``."""

    async def check(self) -> None:
        """Raise when the domain cannot serve traffic (readiness probe)."""

    async def stage(
        self,
        uow: "UnitOfWork",
        events: Sequence["DomainEvent"],
        tasks: Sequence["DeferredTask"],
    ) -> None:
        """Add rows to a committing SQL unit of work (outbox, audit trail)."""

    async def committed(self, uow: "UnitOfWork") -> None:
        """Follow up after a SQL unit of work committed (publish its messages).

        The data is already committed: a failure here is logged and reported,
        and the unit of work carries on.
        """

    async def migrated(self, connection: "AsyncConnection", database: str) -> None:
        """Fill alias ``database`` after ``migrate`` upgraded it (``post_migrate``).

        Runs for every installed domain, in order, in one transaction on the
        schema owner's ``connection``; check ``database`` before writing.
        Content types and permissions are created this way.
        """

    async def seed(self) -> None:
        """Add the rows a fresh database needs to be useful (``manage.py seed``).

        Sample data for development, or defaults a deployment starts with.
        ``seed`` calls the hooks in ``INSTALLED_DOMAINS`` order, so a domain
        may use what those before it seeded. Write rows through the domain's
        own units of work, and make running twice harmless: rows that exist
        stay as they are.
        """

    def check_application(self, app: "FastAPI") -> list[str]:
        """Problems with the built application, such as routes no rule guards.

        ``check`` reports them, and staging and production refuse to start
        with any; elsewhere the application starts and logs them.
        """
        return []

    def holds_table(self, table: "Table") -> bool:
        """Whether ``table``, tagged with this domain, is in use now.

        A table that is not is neither migrated nor given a content type,
        such as the default user table while ``AUTH.USER_MODEL`` swaps it.
        """
        return True

    def databases(self, router: "DatabaseRouter") -> frozenset[str]:
        """The targets that hold this domain's tables (see ``DATABASE_ROUTING``).

        By default, the domain's routing target. Apps whose rows must commit
        with every domain's data (the outbox, the audit trail) return every
        hosting target; apps without tables return none.
        """
        return frozenset({router.target_for(self.label)})


def _import_domain(name: str) -> ModuleType:
    """Import a domain package, explaining a missing one."""
    try:
        module = import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name is not None and (
            name == exc.name or name.startswith(f"{exc.name}.")
        ):
            raise ImproperlyConfigured(
                f"Installed domain {name!r} cannot be imported."
            ) from exc
        raise
    if not hasattr(module, "__path__"):
        raise ImproperlyConfigured(f"Installed domain {name!r} must be a package.")
    return module
