"""``DomainConfig``: Django's ``AppConfig`` for a bounded context."""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, ClassVar, Final

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
PACKAGE_NAME: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
"""A dotted package name, as ``domainkit.contrib.messaging``."""
MAX_REQUIREMENTS: Final = 32
"""The most domains one domain may require."""


@dataclass(frozen=True, slots=True)
class Requirement:
    """A domain package another needs in ``INSTALLED_DOMAINS``, and why.

    ``DomainConfig.requires`` lists these, or bare package names when the
    reason goes without saying.
    """

    name: str
    """The package, as ``INSTALLED_DOMAINS`` lists it."""
    reason: str = ""
    """Why, ending the error that names it: "the worker sends the mail"."""

    @classmethod
    def of(cls, entry: "str | Requirement") -> "Requirement":
        """``entry`` as a requirement; a bare name has no reason."""
        return entry if isinstance(entry, Requirement) else cls(entry)

    def problem(self, needed_by: str) -> str:
        """The sentence ``check`` and ``setup`` refuse a missing package with."""
        reason = f": {self.reason}" if self.reason else ""
        return f"{needed_by} needs {self.name!r} in INSTALLED_DOMAINS{reason}."


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
    requires: ClassVar[Sequence["str | Requirement"]] = ()
    """Domain packages this one cannot work without, checked by ``setup()``.

    Each must be in ``INSTALLED_DOMAINS``, in any order; ``setup()`` refuses
    to load the domains otherwise, naming every one missing, before any
    domain's ``ready()`` runs. List optional companions here only if the
    domain cannot work without them.
    """

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

    def requirements(self) -> tuple[Requirement, ...]:
        """``requires``, checked: package names, at most ``MAX_REQUIREMENTS``.

        Raises:
            ImproperlyConfigured: An entry is not a package name or a
                ``Requirement``, names this domain, or there are too many.
        """
        entries = tuple(self.requires)
        where = f"{type(self).__qualname__}.requires"
        if len(entries) > MAX_REQUIREMENTS:
            raise ImproperlyConfigured(
                f"{where} lists {len(entries)} domains; at most {MAX_REQUIREMENTS}."
            )
        requirements = []
        for entry in entries:
            if not isinstance(entry, str | Requirement):
                raise ImproperlyConfigured(
                    f"{where} takes package names or Requirement, not {entry!r}."
                )
            requirement = Requirement.of(entry)
            if not PACKAGE_NAME.fullmatch(requirement.name):
                raise ImproperlyConfigured(
                    f"{where} names {requirement.name!r}, which is not a package name."
                )
            if requirement.name == self.name:
                raise ImproperlyConfigured(f"{where} names the domain itself.")
            requirements.append(requirement)
        return tuple(requirements)

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

    async def stage_transaction(
        self, connection: "AsyncConnection", database: str
    ) -> None:
        """Join a transaction the framework commits outside a unit of work.

        ``domainkit.contrib.messaging.enqueue`` commits its rows in a
        transaction of its own on alias ``database``; it calls this with
        that transaction's ``connection`` before the commit, as a unit of
        work calls ``stage``. Raising rolls the transaction back.
        """

    async def transaction_committed(self, database: str) -> None:
        """Follow up after such a transaction committed (see ``committed``).

        The data is already committed: a failure here is logged and
        reported, and the caller carries on.
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

    def deployment_warnings(self) -> list[str]:
        """Weak but valid choices of this domain's settings where deployed.

        ``check`` lists them after ``Settings.deployment_warnings``; they
        do not stop the application. Return none outside staging and
        production.
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
