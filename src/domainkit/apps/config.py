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
    from sqlalchemy import Table

    from domainkit.contrib.privacy import Retention
    from domainkit.db.routing import DatabaseRouter

CONFIG_MODULE = "apps"
PACKAGE_NAME: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
"""A dotted package name, as ``domainkit.contrib.messaging``."""
MAX_REQUIREMENTS: Final = 32
"""The most domains one domain may require."""
REMOVED_HOOKS: Final = {
    "startup": "domainkit.apps.hooks.on_startup",
    "shutdown": "domainkit.apps.hooks.on_shutdown",
    "check": "domainkit.apps.hooks.on_check",
    "seed": "domainkit.apps.hooks.on_seed",
    "erase_subject": "domainkit.apps.hooks.on_erase_subject",
    "export_subject": "domainkit.apps.hooks.on_export_subject",
    "check_application": "domainkit.apps.hooks.on_check_application",
    "deployment_warnings": "domainkit.apps.hooks.on_deployment_warnings",
    "stage": "domainkit.db.hooks.on_stage",
    "committed": "domainkit.db.hooks.on_committed",
    "stage_transaction": "domainkit.db.hooks.on_stage_transaction",
    "transaction_committed": "domainkit.db.hooks.on_transaction_committed",
    "migrated": "domainkit.db.hooks.on_migrated",
}
"""Each hook method 0.13 removed, and the point a domain registers on instead.

``setup()`` refuses a domain that still defines one (``apps.E002``) until
1.0: nothing would call it (ADR 0111).
"""
HOOKS_REMOVED_IN: Final = "0.13"


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
    """Describe one installed domain: its label, settings, and what it requires.

    Each domain package may define exactly one subclass in its ``apps``
    module; a package without one gets this base class. ``ready()`` wires
    the domain, and registers the hooks it has: what it does at startup,
    on each commit, when a person's data is erased, and the rest
    (``domainkit.apps.hooks``, ``domainkit.db.hooks``). Only the domains
    that registered are called.

    ``databases`` and ``holds_table`` answer where the domain's tables are;
    override them when the defaults are wrong. The hook methods 0.12
    deprecated (``startup``, ``check``, ``stage``, ``erase_subject``, ...;
    see ``REMOVED_HOOKS``) are gone: ``setup()`` refuses a domain that
    still defines one, since nothing would call it.
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


def defined_hook_methods(config: DomainConfig) -> list[tuple[str, str]]:
    """Each hook method 0.13 removed that ``config`` still defines, and its owner.

    A name counts wherever ``config`` would find it: on its class or any
    base, as a method, a property, a ``staticmethod``, or any other value,
    written in the class or set on it later; or on the instance itself.
    Each pair is the name and the class that defines it (the domain's own
    class, for the instance), in ``REMOVED_HOOKS`` order.
    """
    found = []
    for method in REMOVED_HOOKS:
        if method in vars(config):
            found.append((method, type(config).__qualname__))
            continue
        owner = next((cls for cls in type(config).__mro__ if method in vars(cls)), None)
        if owner is not None:
            found.append((method, owner.__qualname__))
    return found


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
