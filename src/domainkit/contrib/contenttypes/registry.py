"""Which classes are content types: those mapped onto installed domains' tables."""

from dataclasses import dataclass

from sqlalchemy import Table

from domainkit.apps import DomainConfig, registry
from domainkit.db import mapper_registry, metadata

from .models import ModelOptions, model_options


@dataclass(frozen=True, slots=True)
class InstalledModel:
    """A mapped class of an installed domain, and what it declares."""

    domain: str
    cls: type
    options: ModelOptions

    @property
    def key(self) -> tuple[str, str]:
        """``(domain, model)``, the content type's natural key."""
        return self.domain, self.options.model


def installed_models() -> list[InstalledModel]:
    """Every class mapped onto a table that an installed domain holds now.

    A table belongs to the domain in its ``info["domain"]``; an untagged
    table, to the installed domain whose package defines the class. Tables
    outside the project's metadata, and tables a domain does not hold (a
    swapped-out user table), have no content type.
    """
    found: dict[tuple[str, str], InstalledModel] = {}
    for mapper in sorted(mapper_registry.mappers, key=lambda m: m.class_.__qualname__):
        table = mapper.local_table
        if not isinstance(table, Table) or metadata.tables.get(table.key) is not table:
            continue
        config = _owner(table, mapper.class_)
        if config is None or not config.holds_table(table):
            continue
        model = InstalledModel(
            config.label, mapper.class_, model_options(mapper.class_)
        )
        found[model.key] = model
    return [found[key] for key in sorted(found)]


def content_type_key(cls: type) -> tuple[str, str]:
    """The ``(domain, model)`` of a mapped class.

    Raises:
        LookupError: ``cls`` is not a class of an installed domain.
    """
    for model in installed_models():
        if model.cls is cls:
            return model.key
    raise LookupError(f"{cls.__qualname__} is not mapped in an installed domain.")


def _owner(table: Table, cls: type) -> DomainConfig | None:
    """The installed domain that holds ``table``, or None."""
    label = table.info.get("domain")
    if label is not None:
        try:
            return registry.get_domain_config(label)
        except LookupError:
            return None
    module = cls.__module__
    owners = [
        config
        for config in registry
        if module == config.name or module.startswith(f"{config.name}.")
    ]
    return max(owners, key=lambda config: len(config.name)) if owners else None
