"""Request and response schemas derived from domain classes (DRF's ``ModelSerializer``).

``schema_for`` reads an aggregate's fields (``domainkit.ddd.fields``) and
builds pydantic models, so a field is declared once, in the domain::

    ProductOut = schema_for(Product)                        # what clients see
    ProductIn = schema_for(Product, write=True, include=("name", "price"))
    ProductPatch = schema_for(Product, partial=True, include=("name", "price"))

    ProductOut.from_domain(product)

Single-field value objects become what they wrap, with their limits
(``MaxLength`` is ``maxLength``, ``Digits`` the digits of an amount); ids
(``NewType`` over ``UUID``) and ``Reference[XId]`` become their id type;
enums keep their values; other value objects and child entities nest. A
read schema lists every field, required, and gets ``from_domain``; a write
schema leaves out ``id``, ``version``, and ``deleted_at``, takes the
domain's defaults, forbids unknown fields, accepts only aware datetimes,
and bounds integers to 64 bits. ``partial=True`` makes a ``PatchModel``.

``PatchModel`` is a partial change (RFC 7396's merge patch): every field
defaults to ``UNSET``, and ``changes()`` holds only those the request
sent, so ``{"note": null}`` clears a note and ``{}`` changes nothing.
"""

import dataclasses
from collections.abc import Callable, Collection, Mapping
from datetime import datetime
from decimal import Decimal
from enum import Enum
from functools import cache
from typing import Annotated, Any, ClassVar, Final, Self, get_type_hints

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, create_model

from domainkit.ddd.fields import (
    Digits,
    FieldInfo,
    MaxLength,
    fields_of,
    primitive,
    type_arguments,
    unaliased,
    wrapped_field,
)
from domainkit.ddd.messages import UNSET, Unset
from domainkit.ddd.model import ValueObject
from domainkit.ddd.repository import DELETED_AT

GENERATED: Final = frozenset({"id", "version", DELETED_AT})
"""Fields a write schema leaves out: the domain and the database set them."""
INT64: Final = 2**63
REFERENCE_DESCRIPTION: Final = "The id of another aggregate."

type Present = Callable[[object], object]
"""Turns a field's value into what a schema holds."""


def _unset() -> Unset:
    """The default of every ``PatchModel`` field."""
    return UNSET


class PatchModel(BaseModel):
    """A partial change: each field defaults to ``UNSET``; unknown ones are refused.

    Declare fields as usual (``name: str = Field(max_length=200)``), with no
    default; a field may take ``null`` only if its type allows None.
    ``changes()`` are the fields the request sent, as a command's keywords.
    """

    model_config = ConfigDict(extra="forbid")

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:  # noqa: ANN401 - pydantic's keywords.
        """Default every field to ``UNSET``.

        Raises:
            TypeError: A field declares a default of its own.
        """
        super().__pydantic_init_subclass__(**kwargs)
        changed = False
        for name, field in cls.__pydantic_fields__.items():
            if field.default_factory is _unset:
                continue
            if not field.is_required():
                raise TypeError(
                    f"{cls.__name__}.{name}: a PatchModel field has no default; "
                    "one the request leaves out is UNSET."
                )
            field.default_factory = _unset
            changed = True
        if changed:
            cls.model_rebuild(force=True)

    def changes(self) -> dict[str, Any]:
        """The fields the request set, by name, in declaration order."""
        return {
            name: getattr(self, name)
            for name in type(self).model_fields
            if name in self.model_fields_set
        }


class DomainSchema(BaseModel):
    """A read schema ``schema_for`` made; ``from_domain`` fills it from an object."""

    domain_class: ClassVar[type | None] = None
    """The class the schema was derived from."""
    domain_fields: ClassVar[tuple[tuple[str, Present], ...]] = ()
    """Each field, with how its value is shown."""

    @classmethod
    def from_domain(cls, obj: object) -> Self:
        """The schema of ``obj``: value objects unwrapped, child entities nested."""
        return cls.model_validate(
            {name: present(getattr(obj, name)) for name, present in cls.domain_fields}
        )


def schema_for(
    model: type,
    *,
    include: Collection[str] | None = None,
    exclude: Collection[str] = (),
    name: str | None = None,
    write: bool = False,
    partial: bool = False,
    descriptions: Mapping[str, str] | None = None,
) -> type[BaseModel]:
    """A pydantic schema of ``model``'s fields (see the module).

    ``include`` lists the fields to keep, in that order (default: all);
    ``exclude`` drops some. ``write`` makes a request body, ``partial`` a
    ``PatchModel``. ``name`` defaults to ``<Model>Out``, ``<Model>In``, or
    ``<Model>Patch``. ``descriptions`` document fields in OpenAPI.

    Raises:
        TypeError: A field named is not one of the model's, or its type
            has no schema (exclude it, or write the schema yourself).
    """
    fields = fields_of(model)
    writing = write or partial
    names = list(fields) if include is None else list(include)
    unknown = sorted({*names, *exclude} - set(fields))
    if unknown:
        raise TypeError(f"{model.__name__} has no field {', '.join(unknown)}.")
    chosen = [
        each
        for each in names
        if each not in exclude and not (writing and each in GENERATED)
    ]
    suffix = "Patch" if partial else "In" if write else "Out"
    title = name or f"{model.__name__}{suffix}"
    hints = _hints(model)
    defaults = {each.name: each for each in dataclasses.fields(model)}
    definitions: dict[str, Any] = {}
    presenters: list[tuple[str, Present]] = []
    for each in chosen:
        info = fields[each]
        annotation, present = _annotation(info, hints[each], write=writing)
        options: dict[str, Any] = {}
        if info.reference:
            options["description"] = REFERENCE_DESCRIPTION
        if descriptions and each in descriptions:
            options["description"] = descriptions[each]
        if write and not partial:
            options.update(_default(defaults[each]))
        definitions[each] = (annotation, Field(**options))
        presenters.append((each, present))
    if partial:
        return create_model(title, __base__=PatchModel, **definitions)
    if write:
        return create_model(title, __config__=ConfigDict(extra="forbid"), **definitions)
    schema = create_model(title, __base__=DomainSchema, **definitions)
    schema.domain_class = model
    schema.domain_fields = tuple(presenters)
    return schema


def _hints(model: type) -> dict[str, object]:
    """``model``'s type hints, type parameters bound, aliases expanded."""
    bound = type_arguments(model)
    return {
        name: unaliased(bound.get(hint, hint))
        for name, hint in get_type_hints(model, include_extras=True).items()
    }


def _annotation(field: FieldInfo, hint: object, *, write: bool) -> tuple[Any, Present]:
    """The schema type of ``field``, and how to show its value."""
    annotation, present = _shape(field, hint, write=write)
    if field.nullable:
        return annotation | None, _or_none(present)
    return annotation, present


def _shape(field: FieldInfo, hint: object, *, write: bool) -> tuple[Any, Present]:
    """The schema type of a present value of ``field``."""
    kind = field.kind
    entity = field.entity
    if entity is not None:
        nested = _nested(entity, write)
        if field.many:
            return list[nested], _each(_shown(nested))  # type: ignore[valid-type]
        return nested, _shown(nested)
    if kind is None:
        return _plain(hint), _same
    if issubclass(kind, Enum):
        return kind, _same
    if (inner := wrapped_field(kind)) is not None:
        merged = dataclasses.replace(inner, metadata=(*field.metadata, *inner.metadata))
        annotation, _ = _shape(merged, inner.kind, write=write)
        return annotation, primitive
    if issubclass(kind, ValueObject):
        nested = _nested(kind, write)
        return nested, _shown(nested)
    if kind in {set, frozenset}:
        if write:
            return _plain(hint), _same
        return _as_list(hint), _sorted
    return _scalar(field, kind, hint, write=write), _same


def _scalar(field: FieldInfo, kind: type, hint: object, *, write: bool) -> Any:  # noqa: ANN401 - a type.
    """Text, amounts, integers, and times with their limits; other types as hinted."""
    if kind is str and (limit := field.marker(MaxLength)) is not None:
        return Annotated[str, Field(max_length=limit.length)]
    if kind is Decimal and (digits := field.marker(Digits)) is not None:
        return Annotated[
            Decimal,
            Field(max_digits=digits.precision, decimal_places=digits.scale),
        ]
    if write and kind is int:
        return Annotated[int, Field(ge=-INT64, lt=INT64)]
    if write and kind is datetime:
        return AwareDatetime
    if kind in {list, tuple, dict}:
        return _plain(hint)
    return kind


def _plain(hint: object) -> Any:  # noqa: ANN401 - a type.
    """``hint`` without the domain's ``Annotated`` metadata."""
    return getattr(hint, "__origin__", hint) if _annotated(hint) else hint


def _annotated(hint: object) -> bool:
    """Whether ``hint`` is ``Annotated[...]``."""
    return hasattr(hint, "__metadata__")


def _as_list(hint: object) -> Any:  # noqa: ANN401 - a type.
    """``list[X]`` for ``set[X]``: sets are shown as sorted lists."""
    (item,) = getattr(_plain(hint), "__args__", (Any,))
    return list[item]  # type: ignore[valid-type]


@cache
def _nested(cls: type, write: bool) -> type[BaseModel]:
    """The schema of a child entity or a value object, made once."""
    return schema_for(
        cls, write=write, name=f"{cls.__name__}{'In' if write else 'Out'}"
    )


def _shown(schema: type[BaseModel]) -> Present:
    """Show a nested object with its schema's ``from_domain``."""

    def show(value: object) -> object:
        return schema.from_domain(value)  # type: ignore[attr-defined]

    return show


def _each(present: Present) -> Present:
    """Show each item of a collection; a set's in the order of their ids."""

    def show(values: object) -> object:
        items = list(values)  # type: ignore[call-overload]
        if isinstance(values, set | frozenset):
            items.sort(key=lambda item: str(getattr(item, "id", item)))
        return [present(item) for item in items]

    return show


def _or_none(present: Present) -> Present:
    """Show None as None, and anything else with ``present``."""

    def show(value: object) -> object:
        return None if value is None else present(value)

    return show


def _same(value: object) -> object:
    """Show the value as it is."""
    return value


def _sorted(values: object) -> object:
    """A set as a sorted list, so the representation (and its ETag) is stable."""
    return sorted(values, key=lambda item: (type(item).__name__, item))  # type: ignore[call-overload]


def _default(field: dataclasses.Field[Any]) -> dict[str, Any]:
    """A write schema field's default: the domain's, as the request gives it."""
    if field.default is not dataclasses.MISSING:
        return {"default": _wire(field.default)}
    if field.default_factory is not dataclasses.MISSING:
        factory = field.default_factory
        return {"default_factory": lambda: _wire(factory())}
    return {}


def _wire(value: object) -> object:
    """A default as a request gives it: a value object as what it wraps."""
    return primitive(value) if isinstance(value, ValueObject) else value
