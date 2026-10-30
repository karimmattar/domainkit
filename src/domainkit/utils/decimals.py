"""One text form for a ``Decimal``: fixed point, never an exponent.

pydantic writes a ``Decimal`` as ``str(value)``, which turns
``Decimal("1E+3")`` into ``"1E+3"`` and ``Decimal("0E-8")`` into ``"0E-8"``:
spreadsheets read that as a float, and the same amount gets two texts in two
records. Wherever domainkit writes an amount as text (API responses, outbox
payloads, audit details, evidence, exports, cursors, fixtures), it writes
``decimal_text``: ``"1000"``, ``"0.00000000"``, ``"-12.50"``. Places stay as
the value has them, so ``Decimal("5.00")`` is ``"5.00"``, and zero has no
sign. What JSON writes as a string stays a string, and a number a number.

A value with more than ``MAX_FIXED_DIGITS`` digits before or after the point
is no amount, and its fixed-point text could be a billion characters long
(``1E+999999999``): it is written as ``str(value)``, as are NaN and the
infinities.
"""

import dataclasses
from collections.abc import Mapping
from decimal import Decimal
from functools import cache
from typing import Any, Final

from pydantic import BaseModel, TypeAdapter
from pydantic_core import PydanticSerializationError, to_jsonable_python

__all__ = [
    "decimal_text",
]

MAX_FIXED_DIGITS: Final = 100
"""Digits a value may have before the point, and after it, to be written fixed."""


def decimal_text(value: Decimal) -> str:
    """``value`` in fixed point (``"1000"`` for ``Decimal("1E+3")``), at its places.

    Negative zero is ``"0"`` (``"0.00"`` at two places). Values past
    ``MAX_FIXED_DIGITS`` on either side of the point, NaN, and the
    infinities are ``str(value)``.
    """
    exponent = value.as_tuple().exponent  # Of the last digit; a letter if not finite.
    if not (
        isinstance(exponent, int)
        and exponent >= -MAX_FIXED_DIGITS
        and value.adjusted() < MAX_FIXED_DIGITS
    ):
        return str(value)
    if value.is_zero():
        value = value.copy_abs()
    return format(value, "f")


def jsonable(value: object) -> Any:  # noqa: ANN401 - JSON values.
    """``to_jsonable_python(value)``, each ``Decimal`` in it as ``decimal_text``.

    A value of a class with a pydantic schema of its own (``Money``) is
    written as that schema writes it.
    """
    return with_decimal_text(value, to_jsonable_python(value, fallback=_own_schema))


def _own_schema(value: object) -> Any:  # noqa: ANN401 - JSON values.
    """``value`` as its class's own pydantic schema writes it in JSON.

    Raises:
        PydanticSerializationError: The class has none: pydantic cannot write it.
    """
    kind: Any = type(value)
    if not hasattr(kind, "__get_pydantic_core_schema__"):
        raise PydanticSerializationError(f"Unable to serialize unknown type: {kind}")
    return _adapter(kind).dump_python(value, mode="json")


@cache
def _adapter(kind: Any) -> TypeAdapter[Any]:  # noqa: ANN401 - any class.
    """The adapter of a class with a schema of its own, made once."""
    return TypeAdapter(kind)


def with_decimal_text(value: object, dumped: Any) -> Any:  # noqa: ANN401 - JSON values.
    """``dumped``, pydantic's JSON values of ``value``, with Decimals in fixed point.

    ``value`` and ``dumped`` are walked together: the fields of dataclasses
    and models, the items of mappings, and the members of lists, tuples, and
    sets, which pydantic writes in the order they iterate. Only the text
    pydantic wrote for a ``Decimal`` (``str(value)``) is replaced, so a value
    a serializer of its own wrote, and anything that is not text, stays as it
    is. Unchanged parts are returned as they are, not copied.
    """
    if isinstance(value, Decimal):
        return decimal_text(value) if dumped == str(value) else dumped
    if isinstance(dumped, dict):
        changed = {}
        for key, item in _pairs(value):
            name = key if isinstance(key, str) else str(to_jsonable_python(key))
            if name in dumped:
                new = with_decimal_text(item, dumped[name])
                if new is not dumped[name]:
                    changed[name] = new
        return {**dumped, **changed} if changed else dumped
    if isinstance(dumped, list) and isinstance(value, list | tuple | set | frozenset):
        if len(value) != len(dumped):
            return dumped
        items = [
            with_decimal_text(item, out)
            for item, out in zip(value, dumped, strict=True)
        ]
        if all(new is old for new, old in zip(items, dumped, strict=True)):
            return dumped
        return items
    return dumped


def _pairs(value: object) -> list[tuple[object, object]]:
    """The keys and values a mapping, a dataclass, or a model is written with."""
    if isinstance(value, Mapping):
        return list(value.items())
    if isinstance(value, BaseModel):
        return [(name, getattr(value, name)) for name in type(value).model_fields]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return [
            (field.name, getattr(value, field.name))
            for field in dataclasses.fields(value)
        ]
    return []
