"""From a request's values to the domain's: fields converted, commands built.

What the route kinds share about the messages they take: which fields a
command needs, how a body's fields become a command's keywords, how a
query string's become the domain's values, and the summaries routes take
from the messages' docstrings.
"""

import dataclasses
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypeGuard, get_origin, get_type_hints

from pydantic import BaseModel

from domainkit.db.models import model_key
from domainkit.ddd.fields import FieldInfo, fields_of, wrapped_field
from domainkit.ddd.model import ValueObject

from ..schemas import PatchModel


def to_domain(field: FieldInfo, value: object) -> object:
    """A request's ``value`` as ``field`` holds it: value objects built, enums found.

    A value object of several fields is built from its parts (a dict), and
    so is each item of a tuple of them.

    Raises:
        InvalidQuery: The value does not fit the field.
    """
    if value is None:
        return None
    kind = field.kind
    if _composite(kind) and isinstance(value, Mapping):
        inner = fields_of(kind)
        return kind(
            **{name: to_domain(inner[name], item) for name, item in value.items()}
        )
    if _composite(field.item) and isinstance(value, list | tuple):
        each = FieldInfo(field.name, field.owner, field.item)
        return tuple(to_domain(each, item) for item in value)
    return field.coerce(value)


def value_fields(message: type) -> dict[str, FieldInfo]:
    """The fields of ``message`` that hold value objects of several fields.

    Alone or in a tuple (``tuple[Address, ...]``). A body gives each as its
    parts, which ``domain_values`` makes into the value object. A message whose
    hints do not resolve has none.
    """
    try:
        fields = fields_of(message)
    except NameError:
        return {}
    return {
        name: field
        for name, field in fields.items()
        if _composite(field.kind) or _composite(field.item)
    }


def domain_values(
    fields: Mapping[str, FieldInfo], given: dict[str, Any]
) -> dict[str, Any]:
    """``given``, with the value objects ``fields`` hold built from their parts.

    Only parts are built (a dict, or a tuple of dicts); a value given
    otherwise is left as it is.
    """
    if not fields:
        return given
    return {
        name: _domain_value(fields[name], value) if name in fields else value
        for name, value in given.items()
    }


def _domain_value(field: FieldInfo, value: object) -> object:
    """``value`` as ``field`` holds it, if it is its value objects' parts."""
    if isinstance(value, Mapping) or (
        isinstance(value, list | tuple)
        and all(isinstance(item, Mapping) for item in value)
    ):
        return to_domain(field, value)
    return value


def _composite(kind: object) -> TypeGuard[type]:
    """Whether ``kind`` is a value object of several fields."""
    return (
        isinstance(kind, type)
        and issubclass(kind, ValueObject)
        and wrapped_field(kind) is None
    )


def changes(payload: BaseModel | None) -> dict[str, Any]:
    """A body's fields as a command's keywords; a ``PatchModel``'s set ones only.

    Nested models become dicts, as ``model_dump`` gives them; dataclasses
    the body validated (value objects among them) stay as they are.
    """
    if payload is None:
        return {}
    given = (
        payload.changes()
        if isinstance(payload, PatchModel)
        else {name: getattr(payload, name) for name in type(payload).model_fields}
    )
    return {name: plain(value) for name, value in given.items()}


def plain(value: object) -> object:
    """Nested models (in lists, tuples, and dicts too) as dicts; others as they are."""
    if isinstance(value, BaseModel):
        return value.model_dump()
    if isinstance(value, list):
        return [plain(item) for item in value]
    if type(value) is tuple:
        return tuple(plain(item) for item in value)
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
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
