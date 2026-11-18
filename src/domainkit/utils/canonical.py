"""One text per JSON value, for digests and signatures: RFC 8785's (JCS).

``canonical_json(value)`` writes a value as the JSON Canonicalization
Scheme does, as UTF-8 bytes: object members sorted by their names' UTF-16
code units, no white space, strings escaped as ECMAScript's
``JSON.stringify`` escapes them, and numbers written as ECMAScript writes
them (``1`` for ``1.0``, ``1e+21``, ``1e-7``). Any implementation of RFC
8785, in any language, gives the same bytes for the same value, so a
client can work out a digest from what it shows.

New in 2.9, for new digests (ADR 0188). The digests the framework already
stores keep their own encoders (an audit row's, an evidence record's, a
command's fingerprint), so what was stored still verifies.
"""

import json
import math
from collections.abc import Mapping
from typing import Final

__all__ = ["canonical_json"]

MAX_SAFE_INTEGER: Final = 2**53 - 1
"""The largest integer every JSON implementation reads exactly (RFC 8785, App. B)."""
_ENCODER: Final = json.JSONEncoder(
    ensure_ascii=False, allow_nan=False, separators=(",", ":")
)


def canonical_json(value: object) -> bytes:
    """``value`` as RFC 8785 canonical JSON, in UTF-8.

    ``value`` holds dicts with string keys, lists and tuples, strings,
    integers, floats, booleans, and None. Write a ``Decimal`` or a large
    integer as a string (``decimal_text``), a time as ISO 8601 text.

    Raises:
        TypeError: A value or a key of a type JSON has no form for.
        ValueError: NaN or an infinity; an integer past 2**53 - 1, which
            JSON readers may round; or a string with a lone surrogate,
            which is no Unicode text.
    """
    return _text(value).encode("utf-8")


def _text(value: object) -> str:
    """The canonical text of ``value``."""
    if value is None or isinstance(value, bool | str):
        return _ENCODER.encode(value)
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise ValueError(
                f"{value} is past 2**53 - 1, which JSON readers may round: "
                "write it as a string."
            )
        return int.__repr__(value)
    if isinstance(value, float):
        return _number(float(value))
    if isinstance(value, list | tuple):
        return "[" + ",".join(_text(each) for each in value) + "]"
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("Canonical JSON takes objects with string keys only.")
        names = sorted(value, key=lambda name: name.encode("utf-16-be"))
        return (
            "{"
            + ",".join(
                f"{_ENCODER.encode(name)}:{_text(value[name])}" for name in names
            )
            + "}"
        )
    raise TypeError(f"Canonical JSON has no form for a {type(value).__name__}.")


def _number(value: float) -> str:
    """``value`` as ECMAScript's ``Number.prototype.toString`` writes it.

    The digits are the shortest that read back as ``value`` (``repr``'s);
    where they go follows ECMA-262's Number::toString, as RFC 8785 asks.
    """
    if not math.isfinite(value):
        raise ValueError("Canonical JSON has no form for NaN or an infinity.")
    if value == 0:
        return "0"
    if value < 0:
        return "-" + _number(-value)
    mantissa, _, exponent = repr(value).partition("e")
    whole, _, fraction = mantissa.partition(".")
    written = whole + fraction
    digits = written.lstrip("0")
    # n: where the point goes, counted from the first digit (ECMA-262's n).
    n = len(whole) + int(exponent or 0) - (len(written) - len(digits))
    digits = digits.rstrip("0")
    k = len(digits)
    if k <= n <= 21:
        return digits + "0" * (n - k)
    if 0 < n <= 21:
        return f"{digits[:n]}.{digits[n:]}"
    if -6 < n <= 0:
        return "0." + "0" * -n + digits
    sign = "+" if n - 1 >= 0 else "-"
    head = digits[0] if k == 1 else f"{digits[0]}.{digits[1:]}"
    return f"{head}e{sign}{abs(n - 1)}"
