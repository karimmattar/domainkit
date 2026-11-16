"""From a request's values to the domain's: fields converted, commands built.

What the route kinds share about the messages they take: which fields a
command needs, how a body's fields become a command's keywords, how a
query string's become the domain's values, and the summaries routes take
from the messages' docstrings.
"""

import dataclasses
from collections.abc import Callable, Mapping, Sequence
from functools import cache, partial
from types import NoneType, UnionType
from typing import Any, TypeGuard, Union, get_args, get_origin, get_type_hints

from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel

from domainkit.db.models import model_key
from domainkit.ddd.exceptions import InvalidField
from domainkit.ddd.fields import FieldInfo, fields_of, wrapped_field
from domainkit.ddd.model import Entity, ValueObject
from domainkit.money import MoneyError

from ..schemas import PatchModel, derived_from


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


type Loc = tuple[str | int, ...]
"""Where a value is in the request, as validation errors name it."""
type Build = Callable[[Any, Loc], object]
"""Makes a body's validated value, found at a ``Loc``, into what a field holds."""


@cache
def builders(message: type, body: type[BaseModel] | None) -> dict[str, Build]:
    """How ``message``'s value objects are made from ``body``'s fields, by field.

    Only where the body's field is what ``schema_for`` derives for the
    value object ``message`` declares, decided here, once:

    - a value object of several fields, as its write schema (``PlaceIn``),
      alone or in a tuple: built from the parts, so its checks run;
    - one of a single field, in a schema ``schema_for`` derived from a
      field of the same type: built around its value, itself made so;
    - a value object or another dataclass, or a tuple of them, as that
      class, where ``message`` declares that same class (or a tuple of
      it): the value pydantic validated, each value object in it built
      again, inside out, so its checks run.

    Other fields, a field ``message`` declares as ``dict``, ``Any``, or
    anything else, a ``list``, and every field of a message whose hints do
    not resolve, are given as ``model_dump()`` gives them.
    """
    if body is None:
        return {}
    try:
        fields = fields_of(message)
    except NameError:
        return {}
    derived = derived_from(body)
    sources = derived[1] if derived is not None else {}
    return {
        name: build
        for name, info in body.model_fields.items()
        if name in fields
        and (build := _builder(fields[name], _bare(info.annotation), sources.get(name)))
        is not None
    }


def _builder(
    field: FieldInfo, annotation: object, source: FieldInfo | None
) -> Build | None:
    """How ``field``'s value is made from a body's of ``annotation``, if it is."""
    kind, item = field.kind, field.item
    if _composite(kind) and _parts_of(annotation, kind):
        return partial(_from_parts, kind, annotation)
    items = _items_of(annotation)
    if _composite(item) and _parts_of(items, item):
        return partial(_each, partial(_from_parts, item, items))
    if _plain_class(kind) and annotation is kind:
        return _checked
    if _plain_class(item) and items is item:
        return partial(_each, _checked)
    if (
        isinstance(kind, type)
        and (inner := wrapped_field(kind)) is not None
        and source is not None
        and source.kind is kind
    ):  # The body gives what it wraps, as a field of that type is derived.
        return partial(_around, kind, inner.name, _builder(inner, annotation, inner))
    return None


def _parts_of(annotation: object, kind: type) -> TypeGuard[type[BaseModel]]:
    """Whether ``annotation`` is a write schema of ``kind``, with all it needs."""
    if not isinstance(annotation, type) or issubclass(annotation, PatchModel):
        return False
    derived = derived_from(annotation)
    return (
        derived is not None
        and derived[0] is kind
        and required(kind) <= set(derived[1]) <= _initial(kind)
    )


def _initial(kind: type) -> set[str]:
    """The fields ``kind``'s ``__init__`` takes."""
    return {each.name for each in dataclasses.fields(kind) if each.init}


def _items_of(annotation: object) -> object:
    """``X`` of ``tuple[X, ...]``; None for another annotation."""
    args = get_args(annotation)
    return args[0] if get_origin(annotation) is tuple and args[1:] == (...,) else None


def _from_parts(
    kind: type, schema: type[BaseModel], parts: BaseModel | None, loc: Loc
) -> object:
    """The value object ``kind`` of the write schema's ``parts``.

    Its own value objects are made as a body's are; other values stay as
    validated, a nested model as a dict.
    """
    if parts is None:
        return None
    inner = builders(kind, schema)
    arguments = {}
    for name in schema.model_fields:
        value = getattr(parts, name)
        arguments[name] = (
            inner[name](value, (*loc, name)) if name in inner else plain(value)
        )
    return _made(kind, arguments, loc)


def _each(build: Build, values: tuple[object, ...] | None, loc: Loc) -> object:
    """Each of ``values`` made by ``build``, as a tuple."""
    if values is None:
        return None
    return tuple(build(value, (*loc, at)) for at, value in enumerate(values))


def _around(
    kind: type, name: str, build: Build | None, value: object, loc: Loc
) -> object:
    """The value object of one field ``kind`` around ``value``, made by ``build``."""
    if value is None:
        return None
    return _made(kind, {name: value if build is None else build(value, loc)}, loc)


def _checked(value: object, loc: Loc) -> object:
    """``value`` as validated, each value object in it built again, inside out.

    So the checks of each run, which pydantic skips; a dataclass is built
    again only around a value object built again.
    """
    if type(value) is tuple:
        return tuple(_checked(each, (*loc, at)) for at, each in enumerate(value))
    if not dataclasses.is_dataclass(value) or isinstance(value, type):
        return value
    changed = {
        field.name: _checked(getattr(value, field.name), (*loc, field.name))
        for field in dataclasses.fields(value)
        if field.init
    }
    if not isinstance(value, ValueObject) and all(
        each is getattr(value, name) for name, each in changed.items()
    ):
        return value
    return _made(partial(dataclasses.replace, value), changed, loc)


def _made(make: Callable[..., object], arguments: dict[str, Any], loc: Loc) -> object:
    """``make(**arguments)``: a value object at ``loc``, its refusal a 422 there.

    Raises:
        RequestValidationError: It refused a part (``InvalidField``), or
            raised ``ValueError``, ``TypeError``, ``AssertionError``, or
            ``ArithmeticError``, as pydantic answers a validator's.
    """
    try:
        return make(**arguments)
    except InvalidField as exc:
        error = {"type": exc.code, "loc": (*loc, exc.field), "msg": exc.message}
    except AssertionError as exc:
        error = _refused(loc, "assertion_error", "Assertion failed", exc)
    except (TypeError, ValueError, ArithmeticError) as exc:
        if isinstance(exc, MoneyError):
            raise  # Answered as money errors are.
        error = _refused(loc, "value_error", "Value error", exc)
    raise RequestValidationError([error])


def _refused(loc: Loc, kind: str, title: str, exc: BaseException) -> dict[str, Any]:
    """A validation error at ``loc`` for ``exc``, as pydantic writes a validator's."""
    return {
        "type": kind,
        "loc": loc,
        "msg": f"{title}, {exc}",
        "ctx": {"error": str(exc)},
    }


def _bare(annotation: object) -> object:
    """``annotation`` without ``| None``."""
    members = [each for each in get_args(annotation) if each is not NoneType]
    if get_origin(annotation) in {Union, UnionType} and len(members) == 1:
        return members[0]
    return annotation


def _plain_class(kind: object) -> TypeGuard[type]:
    """Whether ``kind`` is a dataclass a field holds by value: not an entity."""
    return (
        isinstance(kind, type)
        and dataclasses.is_dataclass(kind)
        and not issubclass(kind, Entity)
    )


def _composite(kind: object) -> TypeGuard[type]:
    """Whether ``kind`` is a value object of several fields."""
    return (
        isinstance(kind, type)
        and issubclass(kind, ValueObject)
        and wrapped_field(kind) is None
    )


def changes(
    payload: BaseModel | None, build: Mapping[str, Build] | None = None
) -> dict[str, Any]:
    """A body's fields as a command's keywords; a ``PatchModel``'s set ones only.

    Nested models become dicts, as ``model_dump`` gives them, but the
    fields ``build`` makes (``builders``), from the values validated.

    Raises:
        RequestValidationError: A value object refused its parts: 422,
            where in the body they are.
    """
    if payload is None:
        return {}
    build = build or {}
    if isinstance(payload, PatchModel):
        return {
            name: build[name](value, ("body", name)) if name in build else plain(value)
            for name, value in payload.changes().items()
        }
    given = payload.model_dump(exclude=set(build))
    for name, each in build.items():
        given[name] = each(getattr(payload, name), ("body", name))
    return given


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
