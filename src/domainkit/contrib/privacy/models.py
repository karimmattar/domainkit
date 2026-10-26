"""Which aggregates hold personal data, and how each field is erased.

A field of an aggregate marked ``Annotated[..., Personal(of="customer_id")]``
holds personal data about the subject its ``of`` field names (``of="id"``
when the aggregate is the person). Both must be mapped to columns.

Erasing a field sets it to None if it may be None, to ``""`` if it is text,
or, for text a unique constraint covers, to a unique ``erased-...`` value.
Any other field needs the aggregate to erase it itself, in a method::

    def erase_personal_data(self, fields: frozenset[str]) -> None: ...

which then erases every field it is given, as its rules allow, and should
do nothing to fields already erased.
"""

import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from types import NoneType, UnionType
from typing import (
    Annotated,
    Any,
    Final,
    Union,
    cast,
    get_args,
    get_origin,
    get_type_hints,
)

from sqlalchemy import Column, Table, UniqueConstraint
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Mapper

from domainkit.apps import DomainConfig, registry
from domainkit.contrib.audit.tables import audit_log, audit_seals
from domainkit.db import UTCDateTime, metadata
from domainkit.db.models import installed_models
from domainkit.db.routing import router
from domainkit.ddd.personal import personal_fields
from domainkit.exceptions import ImproperlyConfigured

from .retention import Retention

HOOK: Final = "erase_personal_data"
"""The aggregate method that erases fields the defaults cannot."""
ERASED: Final = "erased-"
"""How unique text erased by default starts."""
APPEND_ONLY: Final = frozenset({audit_log.name, audit_seals.name})
"""Tables whose rows are never deleted (the audit trail, its seals)."""


@dataclass(frozen=True, slots=True)
class Erasure:
    """How one field is erased by default, and how to tell it already was."""

    value: Callable[[], object]
    erased: Callable[[object], bool]


NULL: Final = Erasure(lambda: None, lambda value: value is None)
EMPTY: Final = Erasure(lambda: "", lambda value: value == "")


def unique_text(length: int | None) -> Erasure:
    """``erased-`` and random hex, cut to ``length``: unique, and recognized."""
    return Erasure(
        lambda: (ERASED + secrets.token_hex(12))[:length],
        lambda value: isinstance(value, str) and value.startswith(ERASED),
    )


@dataclass(frozen=True)
class PersonalModel:
    """An aggregate of an installed domain with fields marked ``Personal``."""

    domain: str
    model: str
    cls: type
    subjects: Mapping[str, tuple[str, ...]]
    """Each field naming a subject, and the fields about that subject."""
    columns: Mapping[str, Column[Any]]
    """The column of each field above."""
    erasures: Mapping[str, Erasure] = field(default_factory=dict)
    """How each personal field is erased, unless the aggregate does it."""

    @property
    def key(self) -> str:
        """``<domain>.<model>``, as content types name it."""
        return f"{self.domain}.{self.model}"

    @property
    def fields(self) -> tuple[str, ...]:
        """Every personal field, in subject order."""
        return tuple(name for names in self.subjects.values() for name in names)

    def aliases(self) -> tuple[str, ...]:
        """Every database holding the aggregate's rows (each shard, say)."""
        return router.aliases_of(router.target_for(self.domain))

    def erases_itself(self) -> bool:
        """Whether the aggregate erases its fields itself (``erase_personal_data``)."""
        return callable(getattr(self.cls, HOOK, None))


@dataclass(frozen=True)
class Policy:
    """A domain's ``Retention``, resolved to a table and its dating column."""

    domain: str
    retention: Retention
    table: Table
    column: Column[Any]
    model: PersonalModel | None = None
    """The aggregate whose personal fields are erased; None: rows are deleted."""

    @property
    def name(self) -> str:
        """``<domain>.<table>``."""
        return f"{self.domain}.{self.table.name}"

    def cutoff(self, now: datetime) -> datetime | date:
        """The moment (or day, for a ``Date`` column) rows before it go."""
        moment = now - self.retention.keep
        return moment if isinstance(self.column.type, UTCDateTime) else moment.date()

    def aliases(self) -> tuple[str, ...]:
        """Every database holding the table's rows."""
        return router.aliases_of(router.target_for(self.domain))


def personal_models() -> list[PersonalModel]:
    """Every installed aggregate with personal fields.

    Raises:
        ImproperlyConfigured: A marker cannot be followed (see ``problems``).
    """
    models, found = _inspect_models()
    _refuse(found)
    return models


def retention_policies() -> list[Policy]:
    """Every installed domain's ``RETENTION``, resolved.

    Raises:
        ImproperlyConfigured: A policy cannot be applied (see ``problems``).
    """
    models, found = _inspect_models()
    policies, refused = _inspect_policies({model.cls: model for model in models})
    _refuse([*found, *refused])
    return policies


def problems() -> list[str]:
    """What keeps privacy requests or retention from working, as sentences."""
    models, found = _inspect_models()
    _, refused = _inspect_policies({model.cls: model for model in models})
    return [*found, *refused]


def _refuse(found: list[str]) -> None:
    """Raise with every problem found, if any."""
    if found:
        raise ImproperlyConfigured(
            "Privacy cannot be applied:\n  " + "\n  ".join(found)
        )


def _inspect_models() -> tuple[list[PersonalModel], list[str]]:
    """The personal models, and the problems with their markers."""
    models: list[PersonalModel] = []
    found: list[str] = []
    for installed in installed_models():
        cls = installed.cls
        try:
            markers = personal_fields(cls)
        except TypeError as exc:  # A marker below the field: it names the field.
            found.append(str(exc))
            continue
        if not markers:
            continue
        mapper: Mapper[Any] = sa_inspect(cls)
        attributes = {attribute.key: attribute for attribute in mapper.column_attrs}
        subjects: dict[str, tuple[str, ...]] = {}
        for name, marker in markers.items():
            subjects[marker.of] = (*subjects.get(marker.of, ()), name)
        columns: dict[str, Column[Any]] = {}
        for name in dict.fromkeys([*subjects, *markers]):
            attribute = attributes.get(name)
            if attribute is None:
                found.append(f"{cls.__qualname__}.{name} is not mapped to a column.")
            else:
                columns[name] = cast("Column[Any]", attribute.columns[0])
        if len(columns) < len(subjects.keys() | markers.keys()):
            continue
        model = PersonalModel(
            installed.domain, installed.options.model, cls, subjects, columns
        )
        if not model.erases_itself():
            model = replace(model, erasures=_erasures(model, found))
        models.append(model)
    return models, found


def _erasures(model: PersonalModel, found: list[str]) -> dict[str, Erasure]:
    """The default erasure of each field; a problem for each that has none."""
    hints = get_type_hints(model.cls, include_extras=True)
    erasures: dict[str, Erasure] = {}
    for name in model.fields:
        column = model.columns[name]
        erasure = _default(hints[name], column)
        if erasure is None or column.primary_key:
            found.append(
                f"{model.cls.__qualname__}.{name} cannot be erased by default "
                f"(only optional fields and text can); define {HOOK}() on "
                f"{model.cls.__qualname__}."
            )
        else:
            erasures[name] = erasure
    return erasures


def _default(hint: object, column: Column[Any]) -> Erasure | None:
    """None for optional fields, ``""`` or a unique value for text, else nothing."""
    members = _members(hint)
    if NoneType in members:
        return NULL
    if members != [str]:
        return None
    if not _unique(column):
        return EMPTY
    length = getattr(column.type, "length", None)
    return unique_text(length) if length is None or length >= 16 else None


def _members(hint: object) -> list[object]:
    """The types a field holds, ``Annotated`` and unions taken apart."""
    if get_origin(hint) is Annotated:
        return _members(get_args(hint)[0])
    if get_origin(hint) in {Union, UnionType}:
        return [kind for member in get_args(hint) for kind in _members(member)]
    return [hint]


def _unique(column: Column[Any]) -> bool:
    """Whether a unique constraint or index covers ``column`` alone.

    ``Column(unique=True)`` is a unique constraint too (or a unique index,
    with ``index=True``).
    """
    table = column.table
    constraints = [
        constraint.columns
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    ]
    indexes = [index.columns for index in table.indexes if index.unique]
    return any(list(columns) == [column] for columns in [*constraints, *indexes])


def _inspect_policies(
    models: Mapping[type, PersonalModel],
) -> tuple[list[Policy], list[str]]:
    """Each installed domain's retention policies, and their problems."""
    policies: list[Policy] = []
    found: list[str] = []
    for config in registry:
        for retention in config.RETENTION:
            policy, problem = _resolve(config, retention, models)
            if policy is not None:
                policies.append(policy)
            if problem is not None:
                found.append(f"{config.label}: {problem}")
    return policies, found


def _resolve(
    config: DomainConfig, retention: Retention, models: Mapping[type, PersonalModel]
) -> tuple[Policy | None, str | None]:
    """A policy of ``config`` resolved, or why it cannot be."""
    model: PersonalModel | None = None
    if retention.aggregate is not None:
        mapper: Mapper[Any] | None = sa_inspect(retention.aggregate, raiseerr=False)
        if mapper is None or not isinstance(mapper.local_table, Table):
            return None, f"{retention.aggregate.__qualname__} is not mapped."
        table = mapper.local_table
        columns = {
            attribute.key: cast("Column[Any]", attribute.columns[0])
            for attribute in mapper.column_attrs
        }
        model = models.get(retention.aggregate)
    else:
        found = metadata.tables.get(retention.table or "")
        if found is None:
            return None, f"no table is called {retention.table!r}."
        table = found
        columns = dict(table.c.items())
    if table.name in APPEND_ONLY or table.info.get("append_only"):
        return None, (
            f"{table.name} is append-only: its rows are never deleted. Forget "
            "people's keys instead (auditforget)."
        )
    if table.info.get("domain") != config.label:
        return None, f"{table.name} is not a table of {config.label!r}."
    column = columns.get(retention.field)
    if column is None:
        return None, f"{table.name} has no column {retention.field!r}."
    if not (isinstance(column.type, UTCDateTime) or _python_type(column) is date):
        return None, f"{table.name}.{retention.field} is not a UTCDateTime or a Date."
    if retention.erase and (model is None or not _erased_to_none(model)):
        return None, (
            f"{table.name}: erase=True needs an aggregate whose Personal fields "
            f"are all optional, erased to None without {HOOK}()."
        )
    erased = model if retention.erase else None
    return Policy(config.label, retention, table, column, erased), None


def _python_type(column: Column[Any]) -> object:
    """The column's Python type, if SQLAlchemy knows it.

    A type that cannot tell answers ``object`` (SQLAlchemy 2.1) or raises
    ``NotImplementedError`` (types written for 2.0).
    """
    try:
        python_type = column.type.python_type
    except NotImplementedError:
        return None
    return None if python_type is object else python_type


def _erased_to_none(model: PersonalModel) -> bool:
    """Whether every personal field of ``model`` is erased to None by default."""
    return not model.erases_itself() and all(
        erasure is NULL for erasure in model.erasures.values()
    )
