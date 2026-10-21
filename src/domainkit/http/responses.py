"""JSON responses encoded in Rust, byte for byte as Starlette encodes them.

``JSONResponse`` is Starlette's (and FastAPI's) ``JSONResponse`` with a faster
``render``. ``encode_json`` writes the content with ``pydantic_core.to_json``,
two to three times faster than ``json.dumps``, and returns the bytes
``json.dumps`` returns with Starlette's options: compact separators, text in
UTF-8 as it is, NaN and Infinity refused. Where the two encoders can differ,
it hands the content to ``json.dumps`` itself:

- a float from 1e-9 up to 1e-4, which ``to_json`` writes as ``1e-7`` or
  ``0.00005`` and ``json.dumps`` as ``1e-07`` or ``5e-05``;
- NaN and Infinity, which ``json.dumps`` refuses with ``ValueError``;
- a ``None`` key, which ``json.dumps`` writes as ``"null"``;
- what ``to_json`` cannot encode: a lone surrogate, a cycle, deep nesting.

Content made of JSON's values (dicts, lists, tuples, strings, integers,
finite floats, booleans, and None, as FastAPI's ``jsonable_encoder`` and
pydantic's ``model_dump(mode="json")`` give them) therefore encodes to the
same bytes, or fails with the same error. Outside them the two differ, as
``to_json`` encodes what ``json.dumps`` refuses with ``TypeError`` or
``ValueError``: values JSON has no type for (a ``Decimal`` becomes a string,
never a float; a ``datetime`` pydantic's ISO 8601; a set a list), a key of
another type (a tuple), a NaN or infinite key (``"nan"``), and an integer of
more than 4,300 digits. A dict subclass that overrides ``items()`` is written
in the order it holds its keys.

Not a default for the application: FastAPI serializes routes that declare a
response model with pydantic already, and any response class set for them
turns that off (ADR 0063).
"""

import json
import re
from typing import Any, Final

from pydantic_core import PydanticSerializationError, to_json
from starlette import responses

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


def encode_json(content: Any) -> bytes:  # noqa: ANN401 - any JSON content.
    """``content`` as Starlette's ``JSONResponse`` renders it, sooner.

    Raises:
        ValueError: ``content`` holds NaN or Infinity, a cycle, or an
            integer too long to write.
        TypeError: ``content`` holds a value or key JSON has no type for.
        UnicodeEncodeError: A string holds a lone surrogate.
    """
    try:
        encoded = to_json(content, inf_nan_mode="constants")
    except PydanticSerializationError:
        return reference_json(content)
    if may_differ(encoded):
        return reference_json(content)
    return encoded


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

    Use it where a response is built by hand, or as the ``response_class``
    of a route without a response model, whose content FastAPI passes
    through ``jsonable_encoder`` first. Routes with a response model are
    best left to FastAPI, which serializes them with pydantic already.
    """

    def render(self, content: Any) -> bytes:  # noqa: ANN401 - any JSON content.
        """``content`` as JSON bytes."""
        return encode_json(content)
