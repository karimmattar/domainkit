"""Installed models: the mapped classes of installed domains, and their ``Meta``.

Every class mapped onto a table of an installed domain (an aggregate, or any
other mapped entity) is a model, named by its key ``(domain label, model)``,
where the model is the class name in lower case: ``("billing", "invoice")``
(Django's ``_meta.app_label`` and ``model_name``). A class describes itself
in a nested ``Meta``::

    class Invoice(AggregateRoot[InvoiceId]):
        class Meta:
            verbose_name = "invoice"
            permissions = (("approve_invoice", "Can approve invoice"),)

The keys name permissions (``billing.change_invoice``) and the records of
generic writes, with or without ``domainkit.contrib.contenttypes``, which
stores one row per model and re-exports these names.

``model_key`` runs on every request a model permission guards, so it
remembers every class's key, worked out once from ``installed_models``. It
works them out again after anything that could change them:
``registry.populate`` or ``clear``, a class mapped, a table added to or
removed from the project's metadata. Event loops on several threads share
what it remembers, which only changes with its lock held.
"""

import threading
from dataclasses import dataclass
from typing import Final

from sqlalchemy import Table, event
from sqlalchemy.orm import Mapper

from domainkit.apps import DomainConfig, registry
from domainkit.db.orm import mapper_registry, metadata
from domainkit.utils.text import snake_case

__all__ = [
    "DEFAULT_PERMISSIONS",
    "InstalledModel",
    "ModelOptions",
    "installed_models",
    "model_key",
    "model_options",
]

DEFAULT_PERMISSIONS: Final = ("add", "change", "delete", "view")
"""Actions that get a permission for every model, unless ``Meta`` says."""


@dataclass(frozen=True, slots=True)
class ModelOptions:
    """What a mapped class declares about itself (Django's ``Meta``)."""

    model: str
    """The class name in lower case: ``invoiceline``."""
    verbose_name: str
    """Human name, used in permission names: ``invoice line``."""
    default_permissions: tuple[str, ...]
    """Actions that get a ``<action>_<model>`` permission."""
    permissions: tuple[tuple[str, str], ...]
    """Extra ``(codename, name)`` permissions, such as ``approve_invoice``."""


def model_options(cls: type) -> ModelOptions:
    """Read ``cls.Meta``: ``verbose_name``, ``default_permissions``, ``permissions``.

    Every option is optional; ``Meta`` itself is too.
    """
    meta = getattr(cls, "Meta", None)
    verbose_name = getattr(meta, "verbose_name", None)
    return ModelOptions(
        model=cls.__name__.lower(),
        verbose_name=verbose_name or snake_case(cls.__name__).replace("_", " "),
        default_permissions=tuple(
            getattr(meta, "default_permissions", DEFAULT_PERMISSIONS)
        ),
        permissions=tuple(
            (str(codename), str(name))
            for codename, name in getattr(meta, "permissions", ())
        ),
    )


@dataclass(frozen=True, slots=True)
class InstalledModel:
    """A mapped class of an installed domain, and what it declares."""

    domain: str
    cls: type
    options: ModelOptions

    @property
    def key(self) -> tuple[str, str]:
        """``(domain, model)``, the model's natural key."""
        return self.domain, self.options.model


def installed_models() -> list[InstalledModel]:
    """Every class mapped onto a table that an installed domain holds now.

    A table belongs to the domain in its ``info["domain"]``; an untagged
    table, to the installed domain whose package defines the class. Tables
    outside the project's metadata, and tables a domain does not hold (a
    swapped-out user table), have no model.
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


def model_key(cls: type) -> tuple[str, str]:
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
    """Every model's key by class, worked out again if the state changed.

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
