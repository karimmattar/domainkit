"""JSON responses encoded in Rust, byte for byte as Starlette encodes them.

``JSONResponse`` is Starlette's (and FastAPI's) ``JSONResponse`` with a faster
``render``, and ``encode_json`` is what it renders with. They take the fast
path, ``pydantic_core.to_json``, only for content checked to be JSON's own
values as ``json.dumps`` takes them (``json_native``):

- dicts, lists, and tuples, of exactly those classes;
- strings, integers, floats, booleans, and None; floats from 1e-4 up (or
  zero), and integers Python may write (``sys.get_int_max_str_digits()``);
- instances of str, int, and float subclasses, enum members among them,
  which ``json.dumps`` writes as their data; an enum member only when its
  ``value``, which ``to_json`` writes, is that data;
- keys that are strings, integers, finite floats, or booleans.

On such content ``to_json`` writes the bytes ``json.dumps`` writes, with
Starlette's options: compact separators, text in UTF-8 as it is. Anything
else goes to ``json.dumps``, as in Starlette, which refuses it with the same
error: a pydantic model, a dataclass, bytes, a datetime, a ``Decimal``, a
set, another object, NaN or Infinity, an integer too long to write (in no
more than linear time), a NaN key or one of another type. A mistake that
passes a model where its ``model_dump(mode="json")`` belongs still fails
closed, with a 500, and writes none of its fields. What ``json.dumps``
writes otherwise than ``to_json`` it writes itself: a float under 1e-4, a
``None`` key, a subclass key, a dict subclass (in its ``items()`` order),
content nested deeper than ``MAX_DEPTH`` or holding a cycle (which it
refuses), a lone surrogate (which it refuses).

The check is Python, and costs about what ``to_json`` costs again: a page
of 50 resources encodes in about as much as ``json.dumps`` takes, a small
body in a quarter of it. Bodies built JSON-native
(``model_dump(mode="json")``: resources, error and readiness bodies) skip
it: ``BuiltJSONResponse`` writes them with ``to_json`` and looks in the
bytes for the forms ``json.dumps`` writes otherwise (``may_differ``). Use
it only for such content; content from elsewhere goes through
``JSONResponse``.

Not a default for the application: FastAPI serializes routes that declare a
response model with pydantic already, and any response class set for them
turns that off (ADR 0063).
"""

import functools
import json
import math
import re
import sys
from enum import Enum
from typing import Any, Final

from pydantic_core import PydanticSerializationError, to_json
from starlette import responses

__all__ = [
    "BuiltJSONResponse",
    "JSONResponse",
    "json_native",
]

MAX_DEPTH: Final = 64
"""Containers nested deeper than this are encoded by ``json.dumps``.

JSON a service answers with nests a few levels; a cycle nests forever,
and the check stops there instead of following it.
"""
SMALLEST: Final = 1e-4
"""The smallest float ``to_json`` writes as Python does; zero aside."""
_INFINITY: Final = math.inf
_MINUS_INFINITY: Final = -math.inf
SHORT_EXPONENT: Final = re.compile(rb"e-[0-9](?:[,\]}]|\Z)")
"""``to_json``'s exponent of a float under 1e-5: one digit, where Python writes two.

Numbers end at a comma, a bracket, a brace, or the end; a string goes on
to its closing quote, so the dashes of identifiers and dates never match.
"""
TINY: Final = b".0000"
"""What ``to_json`` writes for a float from 1e-5 up to 1e-4: ``0.00005``."""
NUMBER_STARTS: Final = frozenset(b":,[")
"""What may come right before a number in compact JSON (or nothing)."""
TINY_LOOKS: Final = 16
"""``0.0000`` found inside strings that are looked at before giving up.

Past them the content is encoded by ``json.dumps``: that costs less than
looking on, so text full of them cannot make the check the dear part.
"""
_ZERO: Final = ord("0")
_MINUS: Final = ord("-")


def encode_json(content: Any) -> bytes:  # noqa: ANN401 - any content.
    """``content`` as Starlette's ``JSONResponse`` renders it, sooner if it is JSON.

    Raises:
        ValueError: ``content`` holds NaN or Infinity, a cycle, or an
            integer too long to write.
        TypeError: ``content`` holds a value or key JSON has no type for.
        UnicodeEncodeError: A string holds a lone surrogate.
    """
    if json_native(content):
        try:
            return to_json(content)
        except PydanticSerializationError:  # A lone surrogate, say.
            pass
    return reference_json(content)


def json_native(content: object) -> bool:
    """Whether ``to_json`` writes ``content`` as ``json.dumps`` does (see above)."""
    low, high = _integer_bounds(sys.get_int_max_str_digits())
    return _native([content], low, high, MAX_DEPTH)


@functools.lru_cache(maxsize=4)
def _integer_bounds(digits: int) -> tuple[float, float]:
    """What integers of at most ``digits`` digits lie between; 0: no limit.

    ``2 ** (3 * digits)`` is under ``10 ** digits``, so an integer between
    its negative and it is written without reaching the limit; the few from
    it up to the limit go to ``json.dumps``, which writes them as well.
    Both are made once: negating a bound of thousands of bits for each
    integer would cost more than the check.
    """
    bound = 1 << (3 * digits) if digits else math.inf
    return -bound, bound


def _native(container: Any, low: float, high: float, depth: int) -> bool:  # noqa: ANN401 - a dict, list, or tuple.
    """Whether a dict, list, or tuple holds only what ``json_native`` takes.

    Integers must lie between ``low`` and ``high``. The checks are written
    out in the loop, which saves a call for each value.
    """
    if type(container) is dict:
        for key in container:
            kind = type(key)
            if kind is str:
                continue
            if kind is int:
                if not low < key < high:
                    return False
            elif kind is float:
                if not _MINUS_INFINITY < key < _INFINITY:  # NaN or infinite.
                    return False
            elif kind is not bool:
                return False
        items = container.values()
    else:
        items = container
    for item in items:
        kind = type(item)
        if kind is str:
            continue
        if kind is int:
            if not low < item < high:
                return False
        elif kind is dict or kind is list or kind is tuple:
            if not depth or not _native(item, low, high, depth - 1):
                return False
        elif kind is float:
            # _written_alike, written out: 1e-4 is SMALLEST.
            if not (
                1e-4 <= item < _INFINITY
                or _MINUS_INFINITY < item <= -1e-4
                or item == 0.0
            ):
                return False
        elif item is not None and kind is not bool and not _data(item, low, high):
            return False
    return True


def _written_alike(number: float) -> bool:
    """Whether ``to_json`` writes ``number`` as ``repr`` does: finite, not tiny."""
    return (
        SMALLEST <= number < _INFINITY
        or _MINUS_INFINITY < number <= -SMALLEST
        or number == 0.0
    )


def _data(item: object, low: float, high: float) -> bool:
    """Whether ``item``, of a str, int, or float subclass, is written as its data.

    ``json.dumps`` writes a subclass's data, whatever its methods say;
    ``to_json`` too, but an enum member's ``value``. The data is read with
    the base class's own methods.
    """
    kind = type(item)
    data: str | int | float
    if issubclass(kind, str):
        data = str.__str__(item)
    elif issubclass(kind, int):
        data = int.__index__(item)  # type: ignore[arg-type]
        if not low < data < high:
            return False
    elif issubclass(kind, float):
        data = float.__float__(item)  # type: ignore[arg-type]
        if not _written_alike(data):
            return False
    else:
        return False
    if not isinstance(item, Enum):
        return True
    try:
        value = item.value
    except Exception:  # noqa: BLE001 - json.dumps reads the data, never the value.
        return False
    return type(value) is type(data) and value == data and repr(value) == repr(data)


def reference_json(content: Any) -> bytes:  # noqa: ANN401 - any JSON content.
    """``content`` encoded exactly as Starlette's ``JSONResponse.render`` does."""
    return json.dumps(
        content,
        ensure_ascii=False,
        allow_nan=False,
        indent=None,
        separators=(",", ":"),
    ).encode("utf-8")


def may_differ(encoded: bytes) -> bool:
    """Whether ``json.dumps`` may write ``to_json``'s ``encoded`` otherwise.

    True for NaN and Infinity, which it refuses; for a ``None`` key, which
    it writes ``null``; and for floats under 1e-4 that ``to_json`` writes
    in a shorter form. A string that merely looks like one of them also
    answers True, which costs a second encoding, never a wrong one.
    Single-byte searches first: they are the cheap ones.
    """
    if b"N" in encoded and (b"NaN" in encoded or b'"None":' in encoded):
        return True
    if b"I" in encoded and b"Infinity" in encoded:
        return True
    if SHORT_EXPONENT.search(encoded) is not None:
        return True
    return _has_tiny_decimal(encoded)


def _has_tiny_decimal(encoded: bytes) -> bool:
    """Whether a number in ``encoded`` starts with ``0.0000`` (or ``-0.0000``).

    Python writes those floats with an exponent; ``to_json`` without one.
    After ``TINY_LOOKS`` lookalikes it answers True without looking on.
    """
    found = encoded.find(TINY)
    looked = 0
    while found != -1:
        if looked == TINY_LOOKS:
            return True
        looked += 1
        start = found - 1  # The digit before the point.
        if found and encoded[start] == _ZERO:
            if start and encoded[start - 1] == _MINUS:
                start -= 1
            if not start or encoded[start - 1] in NUMBER_STARTS:
                return True
        found = encoded.find(TINY, found + len(TINY))
    return False


class JSONResponse(responses.JSONResponse):
    """Starlette's ``JSONResponse``, rendered by ``encode_json``: the same bytes.

    The same errors too: content ``json.dumps`` refuses (a model, a
    ``Decimal``, a ``datetime``, ...) raises as in Starlette. Use it where a
    response is built by hand, or as the ``response_class`` of a route
    without a response model, whose content FastAPI passes through
    ``jsonable_encoder`` first. Routes with a response model are best left
    to FastAPI, which serializes them with pydantic already.
    """

    def render(self, content: Any) -> bytes:  # noqa: ANN401 - any content.
        """``content`` as JSON bytes."""
        return encode_json(content)


def _encode_built(content: Any) -> bytes:  # noqa: ANN401 - JSON content.
    """``content``, built JSON-native, as Starlette renders it, without the check.

    For domainkit's own bodies only (``BuiltJSONResponse``): ``to_json``
    writes values JSON has no type for instead of refusing them, so the
    content must be JSON's values already, as ``model_dump(mode="json")``
    gives them. Where ``json.dumps`` writes such content otherwise (a small
    float, NaN, a ``None`` key), it writes it itself.
    """
    try:
        encoded = to_json(content, inf_nan_mode="constants")
    except PydanticSerializationError:
        return reference_json(content)
    if may_differ(encoded):
        return reference_json(content)
    return encoded


class BuiltJSONResponse(JSONResponse):
    """JSON bodies built of JSON's own values (``model_dump(mode="json")``): no check.

    Resource items and pages, batch answers, error bodies, and readiness
    answers. They are rendered by ``_encode_built``, in about half of
    ``JSONResponse``'s time on a page of resources. Content built otherwise
    (a model, a ``Decimal``, a ``datetime``) must go through
    ``JSONResponse``: here ``to_json`` would write what ``json.dumps``
    refuses.
    """

    def render(self, content: Any) -> bytes:  # noqa: ANN401 - JSON content.
        """``content`` as JSON bytes."""
        return _encode_built(content)
