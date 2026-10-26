"""Which classes are content types: those mapped onto installed domains' tables.

``content_type_key`` runs on every request a ``ModelPermissions`` rule
guards, so it remembers every class's key, worked out once from
``installed_models``. It works them out again after anything that could
change them: ``registry.populate`` or ``clear``, a class mapped, a table
added to or removed from the project's metadata. Event loops on several
threads share what it remembers, which only changes with its lock held.
"""

import threading
from dataclasses import dataclass
from typing import Final

from sqlalchemy import Table, event
from sqlalchemy.orm import Mapper

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
    try:
        return _keys()[cls]
    except KeyError:
        raise LookupError(
            f"{cls.__qualname__} is not mapped in an installed domain."
        ) from None


type _State = tuple[int, int, int]
"""What ``installed_models`` reads that may change, cheap to compare."""

_lock: Final = threading.Lock()
"""Held while the keys are worked out and replaced."""
_counting: Final = threading.Lock()
_mappings = 0
"""Classes mapped and tables attached to a metadata since import, by any registry."""
_known: tuple[_State | None, dict[type, tuple[str, str]]] = (None, {})
"""Each class's key, and the state it was worked out in; replaced whole."""


def _keys() -> dict[type, tuple[str, str]]:
    """Every content type's key by class, worked out again if the state changed.

    The state is read before the keys are worked out: a change made
    meanwhile leaves a state that no longer matches, so the next call
    works them out again.
    """
    global _known
    state = _state()
    known = _known
    if known[0] == state:
        return known[1]
    with _lock:
        state = _state()
        if _known[0] != state:
            _known = (state, {model.cls: model.key for model in installed_models()})
        return _known[1]


def _state() -> _State:
    """What decides the keys: the domains, the mappings, and the project's tables.

    ``registry.generation`` moves with ``populate`` and ``clear``; mappings
    and tables attached anywhere are counted (``_mapped``); the count of the
    project's tables changes when one is removed. Classes unmapped by
    ``registry.dispose()`` or ``clear_mappers()`` are not seen until
    ``registry.clear()``.
    """
    return (registry.generation, len(metadata.tables), _mappings)


def _mapped(*args: object) -> None:
    """Count a class mapped or a table attached: the keys may change."""
    global _mappings
    with _counting:
        _mappings += 1


event.listen(Mapper, "after_mapper_constructed", _mapped)
event.listen(Table, "after_parent_attach", _mapped)


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
