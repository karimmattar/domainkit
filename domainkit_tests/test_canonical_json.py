"""``canonical_json``: RFC 8785's one text per value, for new digests (E7).

The vectors are RFC 8785's own: its sorting example (section 3.2.3) and
numbers from its appendix B, each a double given by its bits.
"""

import struct
from decimal import Decimal
from enum import IntEnum

import pytest

from domainkit.utils.canonical import MAX_SAFE_INTEGER, canonical_json


def double(bits: str) -> float:
    """The double whose IEEE 754 bits are the hex ``bits``."""
    value: float = struct.unpack(">d", bytes.fromhex(bits))[0]
    return value


@pytest.mark.parametrize(
    ("bits", "text"),
    [
        ("0000000000000000", "0"),
        ("8000000000000000", "0"),
        ("0000000000000001", "5e-324"),
        ("8000000000000001", "-5e-324"),
        ("7fefffffffffffff", "1.7976931348623157e+308"),
        ("ffefffffffffffff", "-1.7976931348623157e+308"),
        ("4340000000000000", "9007199254740992"),
        ("c340000000000000", "-9007199254740992"),
        ("4430000000000000", "295147905179352830000"),
        ("44b52d02c7e14af5", "9.999999999999997e+22"),
        ("44b52d02c7e14af6", "1e+23"),
        ("44b52d02c7e14af7", "1.0000000000000001e+23"),
        ("444b1ae4d6e2ef4e", "999999999999999700000"),
        ("444b1ae4d6e2ef4f", "999999999999999900000"),
        ("444b1ae4d6e2ef50", "1e+21"),
        ("3eb0c6f7a0b5ed8c", "9.999999999999997e-7"),
        ("3eb0c6f7a0b5ed8d", "0.000001"),
        ("41b3de4355555553", "333333333.3333332"),
        ("41b3de4355555554", "333333333.33333325"),
        ("41b3de4355555555", "333333333.3333333"),
        ("41b3de4355555556", "333333333.3333334"),
        ("41b3de4355555557", "333333333.33333343"),
        ("becbf647612f3696", "-0.0000033333333333333333"),
        ("43143ff3c1cb0959", "1424953923781206.2"),
    ],
)
def test_numbers_are_written_as_ecmascript_writes_them(bits: str, text: str) -> None:
    """RFC 8785, appendix B: the shortest digits, placed by Number::toString."""
    assert canonical_json(double(bits)) == text.encode()


def test_numbers_that_python_writes_otherwise() -> None:
    """``2.0`` is ``2``, ``1e16`` has no exponent, ``1e-7`` no leading zero."""
    assert canonical_json([2.0, 1e16, 1e-7, 0.5, 123.25, -0.0, 7, -7]) == (
        b"[2,10000000000000000,1e-7,0.5,123.25,0,7,-7]"
    )


def test_members_are_sorted_by_utf16_code_units() -> None:
    """RFC 8785, 3.2.3: an emoji's surrogates sort before U+FB33."""
    value = {
        "€": "Euro Sign",
        "\r": "Carriage Return",
        "דּ": "Hebrew Letter Dalet With Dagesh",
        "1": "One",
        "\U0001f600": "Emoji: Grinning Face",
        "\u0080": "Control",
        "ö": "Latin Small Letter O With Diaeresis",
    }

    assert canonical_json(value).decode() == (
        '{"\\r":"Carriage Return","1":"One","\u0080":"Control",'
        '"ö":"Latin Small Letter O With Diaeresis","€":"Euro Sign",'
        '"\U0001f600":"Emoji: Grinning Face",'
        '"דּ":"Hebrew Letter Dalet With Dagesh"}'
    )


def test_strings_escape_controls_quotes_and_backslashes_only() -> None:
    """Other characters are written as they are, in UTF-8; nesting has no spaces."""
    value = {"b": [True, False, None], "a": 'é"\\\b\f\n\r\t\x01\x1f/', "c": {}}

    assert (
        canonical_json(value)
        == (
            '{"a":"é\\"\\\\\\b\\f\\n\\r\\t\\u0001\\u001f/","b":[true,false,null],"c":{}}'
        ).encode()
    )
    assert canonical_json(("x", [])) == b'["x",[]]'


class Level(IntEnum):
    """An integer of a subclass."""

    HIGH = 3


def test_integers_up_to_the_safe_bound() -> None:
    """Exact up to 2**53 - 1; past it a reader may round, so it is refused."""
    assert canonical_json(MAX_SAFE_INTEGER) == b"9007199254740991"
    assert canonical_json([-MAX_SAFE_INTEGER, Level.HIGH]) == (b"[-9007199254740991,3]")
    with pytest.raises(ValueError, match="past 2\\*\\*53 - 1"):
        canonical_json(MAX_SAFE_INTEGER + 1)


@pytest.mark.parametrize(
    ("value", "error", "match"),
    [
        (float("nan"), ValueError, "NaN or an infinity"),
        ([float("inf")], ValueError, "NaN or an infinity"),
        ("\ud800", ValueError, "surrogate"),
        ({1: "one"}, TypeError, "string keys only"),
        (Decimal("1.10"), TypeError, "no form for a Decimal"),
        ({"a": {1, 2}}, TypeError, "no form for a set"),
    ],
)
def test_values_without_one_text_are_refused(
    value: object, error: type[Exception], match: str
) -> None:
    """No NaN, no lone surrogate, no keys but strings, no types JSON lacks."""
    with pytest.raises(error, match=match):
        canonical_json(value)
