"""From a request's values to the domain's: fields converted, commands built.

What the route kinds share about the messages they take: which fields a
command needs, how a body's fields become a command's keywords, how a
query string's become the domain's values, and the summaries routes take
from the messages' docstrings.
"""

import dataclasses
from collections.abc import Callable, Mapping, Sequence
from types import NoneType, UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

from pydantic import BaseModel

from domainkit.db.models import model_key
from domainkit.ddd.fields import FieldInfo, fields_of, wrapped_field
from domainkit.ddd.model import Entity, ValueObject

from ..schemas import PatchModel


def to_domain(field: FieldInfo, value: object) -> object:
    """A request's ``value`` as ``field`` holds it: value objects built, enums found.

    Raises:
        InvalidQuery: The value does not fit the field.
    """
    if value is None:
        return None
    kind = field.kind
    if (
        isinstance(kind, type)
        and issubclass(kind, ValueObject)
        and wrapped_field(kind) is None
        and isinstance(value, Mapping)
    ):
        inner = fields_of(kind)
        return kind(
            **{name: to_domain(inner[name], item) for name, item in value.items()}
        )
    return field.coerce(value)


def instances(message: type, body: type[BaseModel] | None) -> frozenset[str]:
    """The fields of ``body`` that give ``message`` the instances it declares.

    A body field typed as a dataclass (a value object among them), or a
    tuple of one, holds the instances pydantic validated: where ``message``
    declares that same class, or a tuple of it, they are its value, as
    they are. Other fields, and every field of a message whose hints do
    not resolve, come as ``model_dump()`` gives them.
    """
    if body is None:
        return frozenset()
    try:
        fields = fields_of(message)
    except NameError:
        return frozenset()
    return frozenset(
        name
        for name, info in body.model_fields.items()
        if name in fields and _declared(fields[name], _bare(info.annotation))
    )


def _declared(field: FieldInfo, annotation: object) -> bool:
    """Whether ``annotation`` is the dataclass ``field`` holds, or a tuple of it."""
    held, given = field.kind, annotation
    if held is tuple:
        args = get_args(annotation)
        held = field.item
        given = (
            args[0] if get_origin(annotation) is tuple and args[1:] == (...,) else None
        )
    return (
        given is held
        and isinstance(held, type)
        and dataclasses.is_dataclass(held)
        and not issubclass(held, Entity)
    )


def _bare(annotation: object) -> object:
    """``annotation`` without ``| None``."""
    members = [each for each in get_args(annotation) if each is not NoneType]
    if get_origin(annotation) in {Union, UnionType} and len(members) == 1:
        return members[0]
    return annotation


def changes(
    payload: BaseModel | None, kept: frozenset[str] = frozenset()
) -> dict[str, Any]:
    """A body's fields as a command's keywords; a ``PatchModel``'s set ones only.

    Nested models become dicts, as ``model_dump`` gives them, but in the
    fields ``kept`` names (``instances``): the instances pydantic validated.
    """
    if payload is None:
        return {}
    if isinstance(payload, PatchModel):  # Gives the instances already.
        return {name: plain(value) for name, value in payload.changes().items()}
    if not kept:
        return payload.model_dump()
    given = payload.model_dump(exclude=set(kept))
    return given | {name: getattr(payload, name) for name in kept}


def plain(value: object) -> object:
    """A nested model (or a list of them) as dicts; other values as they are."""
    if isinstance(value, BaseModel):
        return value.model_dump()
    if isinstance(value, list):
        return [plain(item) for item in value]
    return value


def required(message: type) -> set[str]:
    """The fields of a dataclass message that have no default."""
    return {
        field.name
        for field in dataclasses.fields(message)
        if field.default is dataclasses.MISSING
        and field.default_factory is dataclasses.MISSING
    }


def check_given(
    message: type, given: set[str], provided: Mapping[str, object], other: set[str]
) -> None:
    """Refuse a body or ``provide`` that names fields ``message`` lacks, or misses.

    ``given`` are the body's fields; ``other`` those the path gives.

    Raises:
        TypeError: A field named is not the message's, or a field it needs
            is given by nothing.
    """
    names = {field.name for field in dataclasses.fields(message)}
    if extra := sorted((given | set(provided)) - names):
        raise TypeError(f"{message.__name__} has no field {', '.join(extra)}.")
    if missing := sorted(required(message) - given - set(provided) - other):
        raise TypeError(f"Nothing gives {message.__name__}'s {', '.join(missing)}.")


def returns_many(handler: Callable[..., object]) -> bool:
    """Whether ``handler`` returns a sequence (of aggregates), as annotated.

    Raises:
        TypeError: It does not annotate what it returns.
    """
    returned = get_type_hints(handler).get("return")
    if returned is None:
        raise TypeError(
            f"Annotate what {label_of(handler)} returns: an aggregate, or a list."
        )
    origin = get_origin(returned)
    return isinstance(origin, type) and issubclass(origin, Sequence)


def label_of(function: Callable[..., object]) -> str:
    """A function's name, for messages."""
    return getattr(function, "__qualname__", repr(function))


def summary_of(message: type) -> str | None:
    """A message's summary: its docstring's first line, not the one dataclass writes."""
    text = message.__doc__
    if text is not None and text.startswith(f"{message.__name__}("):
        return None
    return first_line(text)


def first_line(text: str | None) -> str | None:
    """The first line of a docstring, as a route's summary."""
    return text.strip().splitlines()[0] if text and text.strip() else None


def model_label(model: type) -> str:
    """The key of ``model`` (``catalog.category``), or its class name."""
    try:
        return ".".join(model_key(model))
    except LookupError:
        return model.__qualname__
