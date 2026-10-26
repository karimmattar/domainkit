"""Fast JSON responses: the same bytes as Starlette's, from pydantic's encoder.

And the same errors: what Starlette refuses, ``JSONResponse`` refuses too.
"""

import collections
import dataclasses
import enum
import math
import sys
from collections import OrderedDict
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import PurePosixPath
from typing import Any, NamedTuple
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import example, given
from hypothesis import strategies as st
from pydantic import BaseModel
from pydantic_core import to_json
from starlette import responses as starlette

import domainkit.http
from domainkit.http import errors, get_asgi_application
from domainkit.http import responses as fast
from domainkit.http.access import Public
from domainkit.http.responses import (
    MAX_DEPTH,
    JSONResponse,
    _BuiltJSONResponse,
    _encode_built,
    encode_json,
    json_native,
    may_differ,
    reference_json,
)
from domainkit.resources import ResourceRouter
from domainkit_tests.gadgets import (
    FakeGadgetsUnitOfWork,
    Gadget,
    InMemoryGadgets,
    make_gadget,
)


class Color(enum.StrEnum):
    """A string enum: JSON sees its value."""

    RED = "red"


class Rank(enum.IntEnum):
    """An integer enum: JSON sees its value."""

    FIRST = 1


class Text(str):
    """A string subclass."""


class Number(float):
    """A float subclass."""


class Reversed(dict[str, int]):
    """A dict that lists its items backwards."""

    def items(self) -> Any:  # noqa: ANN401 - dict's own items view type.
        """The items, last first."""
        return reversed(list(super().items()))


def starlette_body(content: object) -> bytes | type[BaseException]:
    """Starlette's bytes for ``content``, or the type of what it raises."""
    try:
        return bytes(starlette.JSONResponse(content).body)
    except (ValueError, TypeError) as error:
        return type(error)


def fast_body(
    content: object, encode: Callable[[Any], bytes] = encode_json
) -> bytes | type[BaseException]:
    """``encode``'s bytes for ``content``, or the type of what it raises."""
    try:
        return encode(content)
    except (ValueError, TypeError) as error:
        return type(error)


def outcome(render: Callable[[], object]) -> object:
    """What ``render`` gives: bytes, or what it raised, with its message."""
    try:
        return bytes(render())  # type: ignore[call-overload]
    except (ValueError, TypeError, RecursionError) as error:
        return type(error), str(error)


def starlette_outcome(content: object) -> object:
    """Starlette's ``JSONResponse`` body for ``content``, or its error."""
    return outcome(lambda: starlette.JSONResponse(content).body)


def fast_outcome(content: object) -> object:
    """The body of domainkit's ``JSONResponse`` for ``content``, or its error."""
    return outcome(lambda: JSONResponse(content).body)


ENCODERS = [encode_json, _encode_built]
"""The checked encoder, and the one for content built JSON-native."""


NESTED: list[Any] = []
for _ in range(400):  # Deeper than pydantic goes; json.dumps writes it.
    NESTED = [NESTED]
CYCLE: list[Any] = []
CYCLE.append(CYCLE)

CORPUS: dict[str, object] = {
    "small": {"status": "ok"},
    "empty containers": {"a": {}, "b": [], "c": "", "d": ()},
    "nested": {"a": [1, 2.5, {"b": None, "c": "é", "d": [True, False]}]},
    "key order": {"z": 1, "a": 2, "m": 3},
    "none values": {"a": None, "b": [None]},
    "integers": [0, -1, 2**63, -(2**63), 2**100, 10**4299],
    "floats": [0.0, -0.0, 0.1, 1.0, 1e15, 1e16, 1e22, -1.5e300, 5e-324, 1e-4],
    "floats from 1e-9 to 1e-4": [1e-5, 3.4e-5, -9.99e-5, 1e-6, 1.5e-7, -2e-8, 1e-9],
    "floats under 1e-9": [1e-10, 2.5e-300],
    "float alone": 1e-7,
    "tiny float alone": -3.4e-5,
    "control characters": "".join(chr(code) for code in range(32)),
    "quotes and slashes": '"\\/',
    "non-ascii": "é ü ñ مرحبا 😀 \u2028\u2029 \x7f \ufffe",
    "integer keys": {1: "a", -2: "b", 2**70: "c"},
    "float keys": {1.5: "a", 1e16: "b", 1e-7: "c", 3.4e-5: "d"},
    "boolean keys": {True: "a", False: "b"},
    "none key": {None: "a"},
    "the string None as a key": {"None": "a"},
    "duplicate keys": {1: "a", "1": "b"},
    "subclasses": {Text("k"): [Color.RED, Rank.FIRST, Text("x"), Number(1e16)]},
    "strings that look like refusals": ["NaN", "Infinity", "-Infinity", "x,1e-7]"],
    "uuids and dates": ["9e3e-7a1b", "2026-10-21T09:30:15.123+00:00", "e-7"],
    "decimal strings": {"rate": "0.00005", "fee": "-0.00001"},
    "nan": float("nan"),
    "infinity": [float("inf")],
    "minus infinity": {"a": float("-inf")},
    "lone surrogate": "\ud800",
    "cycle": CYCLE,
    "deep": NESTED,
    "object": object(),
    "unhashable key type": {frozenset(): "a"},
}


@pytest.mark.parametrize("name", list(CORPUS))
def test_the_same_bytes_or_the_same_error_as_starlette(name: str) -> None:
    """Every payload of the corpus: identical bytes, or the same exception type."""
    content = CORPUS[name]

    expected = starlette_body(content)

    assert fast_body(content) == expected
    if isinstance(expected, bytes):
        assert bytes(JSONResponse(content).body) == expected


@pytest.mark.parametrize("name", list(CORPUS))
def test_content_built_json_native_encodes_alike(name: str) -> None:
    """The path for domainkit's own bodies, on the corpus's JSON: Starlette's bytes.

    Or Starlette's error, with its message.
    """
    content = CORPUS[name]
    expected = starlette_outcome(content)

    if json_native(content) or name in BUILT:
        assert outcome(lambda: _encode_built(content)) == expected
        assert outcome(lambda: _BuiltJSONResponse(content).body) == expected


BUILT = {
    "floats from 1e-9 to 1e-4",
    "floats under 1e-9",
    "float alone",
    "tiny float alone",
    "integers",
    "float keys",
    "none key",
    "subclasses",
    "nan",
    "infinity",
    "minus infinity",
}
"""JSON content the check leaves to json.dumps; domainkit's own path takes it."""


@pytest.mark.parametrize("encode", ENCODERS)
@pytest.mark.parametrize(
    ("content", "written"),
    [
        (1e-7, b"1e-07"),
        ([1.5e-7], b"[1.5e-07]"),
        ({"a": -2e-9}, b'{"a":-2e-09}'),
        (3.4e-5, b"3.4e-05"),
        ([-3.4e-5], b"[-3.4e-05]"),
        ({"a": 1e-5}, b'{"a":1e-05}'),
        ({None: 1}, b'{"null":1}'),
    ],
)
def test_where_pydantic_writes_otherwise_json_dumps_writes(
    content: object, written: bytes, encode: Callable[[Any], bytes]
) -> None:
    """Small floats and None keys: Python's form is written, never pydantic's.

    The check leaves them to json.dumps; domainkit's own path finds
    pydantic's form in the bytes.
    """
    assert may_differ(to_json(content))
    assert not json_native(content)
    assert encode(content) == written


@pytest.mark.parametrize("encode", ENCODERS)
@pytest.mark.parametrize(
    "content", [float("nan"), [float("inf")], {"a": float("-inf")}, [1, float("nan")]]
)
def test_nan_and_infinity_are_refused(
    content: object, encode: Callable[[Any], bytes]
) -> None:
    """As Starlette refuses them: a ValueError, never ``NaN`` or ``null``."""
    with pytest.raises(ValueError, match="Out of range float values"):
        encode(content)
    with pytest.raises(ValueError, match="Out of range float values"):
        JSONResponse(content)
    with pytest.raises(ValueError, match="Out of range float values"):
        _BuiltJSONResponse(content)


@pytest.mark.parametrize("encode", ENCODERS)
@pytest.mark.parametrize(
    "content",
    [
        {"id": "9e3e-7a1b-4c2d", "at": "2026-10-21T09:30:15+00:00"},
        {"rate": "0.00005", "values": [1.00001, 10.00001, 0.0001, -0.0001, 0.0]},
        {"text": "Nadia, Inge", "name": "INFO"},
        [1, 2.5, "e-7", None, True, (False, -(2**64))],
        {"kebab-e-7": "x-e-7", 1: "one", 2.5: "two", False: "no"},
        {"color": Color.RED, "rank": Rank.FIRST, "text": Text("x"), "n": Number(2.5)},
    ],
)
def test_ordinary_content_takes_the_fast_path(
    content: object, encode: Callable[[Any], bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Identifiers, dates, decimal strings, and ordinary numbers: no second pass."""

    def no_reference(content: object) -> bytes:
        raise AssertionError("encoded twice")

    expected = reference_json(content)
    monkeypatch.setattr(fast, "reference_json", no_reference)

    assert json_native(content)
    assert not may_differ(to_json(content))
    assert encode(content) == expected


def test_floats_under_1e_minus_4_are_left_to_json_dumps() -> None:
    """Even where pydantic writes them as Python does: the check is by range."""
    content = [1e-10, -2.5e-300, 5e-324]

    assert to_json(content) == reference_json(content)
    assert not json_native(content)
    assert encode_json(content) == _encode_built(content) == reference_json(content)


@pytest.mark.parametrize(
    "encoded",
    [
        b'"0.00005"',
        b'["x0.00005"]',
        b'{"a":"-0.00005"}',
        b"10.00001",
        b"[1.00005]",
        b'"a-0.00001"',
        b'"x:1e-70"',
        b"[1e-10]",
        b'"9e-7a"',
    ],
)
def test_lookalikes_inside_strings_and_other_numbers_pass(encoded: bytes) -> None:
    """Only a number that starts with ``0.0000``, or ends in a one-digit exponent."""
    assert not may_differ(encoded)


@pytest.mark.parametrize(
    "encoded",
    [
        b"0.00005",
        b"-0.00005",
        b"[0.00001]",
        b"[1,-0.00009]",
        b'{"a":0.00003}',
        b"1e-7",
        b"[2.5e-9]",
        b'{"a":1e-6}',
        b'{"a":"x,1e-7}"}',
        b"NaN",
        b"[-Infinity]",
        b'{"None":1}',
        b'[0.00001,"0.00005"]',
        b'["0.00005",0.00001]',
        b'["0.00005",0.00001,"0.00005"]',
    ],
)
def test_forms_json_dumps_writes_otherwise_are_caught(encoded: bytes) -> None:
    """Each form pydantic writes and Python does not sends the content to json.dumps."""
    assert may_differ(encoded)


class Row(BaseModel):
    """A row with a secret, passed by mistake where its JSON dump belongs."""

    username: str
    password_hash: str


@dataclasses.dataclass
class Card:
    """A dataclass with a card number."""

    pan: str
    cvv: str


class Slots:
    """An object with attributes: nothing JSON knows."""

    def __init__(self) -> None:
        """A secret attribute."""
        self.secret = "s3cret"


class Plain(enum.Enum):
    """An enum of no JSON type: json.dumps refuses its members."""

    A = "a"


class Odd(str, enum.Enum):  # noqa: UP042 - a mixin enum, as older code writes it.
    """A string enum whose value is not its data: json.dumps writes the data."""

    X = "data"

    def __new__(cls, data: str) -> "Odd":
        """The member's string is ``data``; its value, something else."""
        member = str.__new__(cls, data)
        member._value_ = "value"
        return member


class OddNumber(int, enum.Enum):
    """An integer enum whose value is not its data."""

    X = 1

    def __new__(cls, data: int) -> "OddNumber":
        """The member's integer is ``data``; its value is ``data + 1``."""
        member = int.__new__(cls, data)
        member._value_ = data + 1
        return member


class Renamed(enum.StrEnum):
    """A string enum whose ``value`` says something else."""

    A = "a"

    @property
    def value(self) -> str:
        """Not the member's string."""
        return "renamed"


class Unreadable(enum.IntEnum):
    """An integer enum whose ``value`` cannot be read."""

    A = 1

    @property
    def value(self) -> int:
        """Raises."""
        raise RuntimeError("no value")


class FloatKind(float, enum.Enum):
    """A float enum."""

    HALF = 0.5
    NEGATIVE_ZERO = -0.0


class SignedZero(float, enum.Enum):
    """A float enum whose data is -0.0 and whose value is 0.0: equal, not alike."""

    ZERO = -0.0

    def __new__(cls, data: float) -> "SignedZero":
        """The member's float is ``data``; its value, zero without a sign."""
        member = float.__new__(cls, data)
        member._value_ = 0.0
        return member


class Flags(enum.IntFlag):
    """Flags, combined into members of their own."""

    READ = 1
    WRITE = 2


class Lying(int):
    """An integer whose comparisons and conversions lie."""

    def __lt__(self, other: object) -> bool:
        """Always smaller."""
        return True

    def __gt__(self, other: object) -> bool:
        """Always greater."""
        return True

    def __index__(self) -> int:
        """Zero."""
        return 0

    def __hash__(self) -> int:
        """As an int."""
        return int.__hash__(self)


class LyingFloat(float):
    """A float whose comparisons and conversion lie."""

    def __ge__(self, other: object) -> bool:
        """Always."""
        return True

    def __le__(self, other: object) -> bool:
        """Always."""
        return True

    def __lt__(self, other: object) -> bool:
        """Always."""
        return True

    def __eq__(self, other: object) -> bool:
        """Always."""
        return True

    def __float__(self) -> float:
        """One."""
        return 1.0

    def __hash__(self) -> int:
        """As a float."""
        return float.__hash__(self)


class Pair(NamedTuple):
    """A named tuple: json.dumps writes it as a list."""

    a: int
    b: int


class NamedNumber(int):
    """An integer with a name of its own: ``to_json`` writes keys by ``str``."""

    def __str__(self) -> str:
        """Not its digits."""
        return "named"


HUGE = (1 << 1_600_000) - 1
"""481,648 digits: refused by json.dumps at once, quadratic to write."""
REFUSED: dict[str, object] = {
    "pydantic model": {"user": Row(username="alice", password_hash="pbkdf2$1$s$h")},
    "dataclass": {"card": Card(pan="4111111111111111", cvv="123")},
    "bytes": {"blob": b"raw"},
    "bytearray": [bytearray(b"raw")],
    "memoryview": [memoryview(b"raw")],
    "datetime": {"at": datetime(2026, 1, 1, tzinfo=UTC)},
    "date": [date(2026, 1, 1)],
    "time": [time(9, 30)],
    "timedelta": [timedelta(seconds=1)],
    "set": {"tags": {"a"}},
    "frozenset": [frozenset({1})],
    "Decimal": {"amount": Decimal("10.10")},
    "UUID": [UUID("12345678-1234-5678-1234-567812345678")],
    "path": [PurePosixPath("/etc/passwd")],
    "object with attributes": {"x": Slots()},
    "complex": [1j],
    "plain enum": {"kind": Plain.A},
    "plain enum key": {Plain.A: 1},
    "huge integer": {"n": HUGE},
    "huge negative integer": [-HUGE],
    "huge integer key": {HUGE: 1},
    "huge integer subclass": [Lying(HUGE)],
    "NaN key": {float("nan"): 1},
    "infinite key": {float("-inf"): 1},
    "positive infinite key": {float("inf"): 1},
    "NaN subclass": [LyingFloat("nan")],
    "tuple key": {(1, 2): "a"},
    "bytes key": {b"k": 1},
    "type": [int],
    "function": [print],
    "cycle": CYCLE,
}


@pytest.mark.parametrize("name", list(REFUSED))
def test_what_starlette_refuses_is_refused_alike(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same exception, with the same message, and nothing written.

    pydantic's encoder is never asked: it would write a model's fields
    (a password hash among them), a dataclass's, a Decimal or a datetime,
    and take quadratic time over a long integer.
    """
    content = REFUSED[name]

    def no_fast_path(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("to_json asked")

    expected = starlette_outcome(content)
    monkeypatch.setattr(fast, "to_json", no_fast_path)

    assert isinstance(expected, tuple)
    assert fast_outcome(content) == expected
    assert outcome(lambda: encode_json(content)) == expected
    assert not json_native(content)


def test_a_model_passed_by_mistake_leaks_none_of_its_fields() -> None:
    """A 500, as before: the handler's error, not a body with the hash."""
    app = FastAPI()

    @app.get("/user", response_model=None)
    async def user() -> JSONResponse:
        return JSONResponse({"user": Row(username="alice", password_hash="pbkdf2$s")})

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/user")

    assert response.status_code == 500
    assert b"pbkdf2" not in response.content


ALIKE: dict[str, object] = {
    "string enum": [Color.RED],
    "integer enum": {"rank": Rank.FIRST},
    "float enum": [FloatKind.HALF, FloatKind.NEGATIVE_ZERO],
    "combined flags": [Flags.READ | Flags.WRITE],
    "enum value that is not its data": [Odd.X, OddNumber.X],
    "enum whose value says otherwise": [Renamed.A],
    "enum whose value cannot be read": [Unreadable.A],
    "enum whose value is zero, and data minus zero": [SignedZero.ZERO],
    "enum keys": {Color.RED: 1, Rank.FIRST: 2, Odd.X: 3},
    "lying integer": [Lying(5)],
    "lying float": [LyingFloat(2.5), LyingFloat(1e-7)],
    "integer named otherwise, as a key": {NamedNumber(3): 1},
    "float subclass key": {Number(2.5): 1},
    "string subclass key": {Text("k"): 1},
    "None key": {None: 1, "None": 2},
    "dict subclass": Reversed(a=1, b=2),
    "OrderedDict moved": OrderedDict([("b", 2), ("a", 1)]),
    "defaultdict": collections.defaultdict(int, a=1),
    "named tuple": [Pair(1, 2)],
    "list subclass": [type("Items", (list,), {})([1, 2])],
    "integers at the limit": [10**4299, -(10**4299)],
    "deep": NESTED,
    "lone surrogate": ["\ud800"],
}


@pytest.mark.parametrize("name", list(ALIKE))
def test_what_starlette_writes_is_written_alike(name: str) -> None:
    """Enums, subclasses, odd keys, and dict subclasses: Starlette's bytes.

    ``json.dumps`` writes a subclass's data and a dict subclass's
    ``items()``; where pydantic would write something else (an enum's
    value, a key's ``str``, the dict's own order), json.dumps writes it.
    """
    content = ALIKE[name]
    expected = starlette_outcome(content)

    assert fast_outcome(content) == expected
    assert outcome(lambda: encode_json(content)) == expected


def test_reordered_ordered_dicts_keep_their_order() -> None:
    """``move_to_end`` reorders an OrderedDict, not the dict below it."""
    moved = OrderedDict([("a", 1), ("b", 2)])
    moved.move_to_end("a")

    assert encode_json(moved) == starlette_body(moved) == b'{"b":2,"a":1}'


@pytest.fixture
def digits() -> Iterator[Callable[[int], None]]:
    """Set Python's limit on integer digits for the test, then restore it."""
    before = sys.get_int_max_str_digits()
    yield sys.set_int_max_str_digits
    sys.set_int_max_str_digits(before)


def test_integers_are_checked_against_pythons_digit_limit(
    digits: Callable[[int], None],
) -> None:
    """What Python may write is written; one digit more is refused, as Starlette."""
    digits(640)
    longest, too_long = 10**639, 10**640

    assert encode_json([longest]) == starlette_body([longest])
    assert starlette_body([too_long]) is ValueError
    assert fast_body([too_long]) is ValueError
    assert fast_body({-too_long: 1}) is ValueError
    assert json_native([(1 << (3 * 640)) - 1])
    assert not json_native([1 << (3 * 640)])  # Left to json.dumps, which writes it.
    assert encode_json([1 << (3 * 640)]) == starlette_body([1 << (3 * 640)])


def test_integers_of_every_kind_are_checked_against_the_same_bound(
    digits: Callable[[int], None],
) -> None:
    """Values, keys, and subclasses: under ``2 ** (3 * limit)`` either way."""
    digits(640)
    bound = 1 << (3 * 640)

    for inside in (bound - 1, -bound + 1):
        assert json_native([inside])
        assert json_native({inside: 1})
        assert json_native([Lying(inside)])
    for outside in (bound, -bound):
        assert not json_native([outside])
        assert not json_native({outside: 1})
        assert not json_native([Lying(outside)])


def test_float_subclasses_are_checked_as_floats_are() -> None:
    """From 1e-4 up, or zero, and finite; their data, whatever they say."""
    assert json_native([Number(1e-4), Number(-1e-4), Number(0.0), Number(-0.0)])
    assert json_native([LyingFloat(2.5)])
    for number in (9.9e-5, -9.9e-5, math.inf, -math.inf, math.nan):
        assert not json_native([Number(number)])
    assert not json_native([LyingFloat(1e-7)])
    assert not json_native([LyingFloat(math.nan)])


def test_enum_members_take_the_fast_path_only_when_their_value_is_their_data() -> None:
    """``to_json`` writes the value, ``json.dumps`` the data: they must agree."""
    assert json_native([Color.RED, Rank.FIRST, FloatKind.HALF, Flags.READ])
    assert json_native([FloatKind.NEGATIVE_ZERO])
    for member in (Odd.X, OddNumber.X, Renamed.A, Unreadable.A, SignedZero.ZERO):
        assert not json_native([member]), member
    assert not json_native([Plain.A])


def test_without_a_digit_limit_every_integer_is_written(
    digits: Callable[[int], None],
) -> None:
    """``sys.set_int_max_str_digits(0)`` lifts the limit, for both encoders."""
    digits(0)
    long = 10**5000

    assert json_native([long, {long: -long}])
    assert encode_json([long]) == starlette_body([long])


def test_nesting_past_the_depth_is_left_to_json_dumps() -> None:
    """Content as deep as ``MAX_DEPTH`` is checked; deeper, json.dumps writes it."""
    deep: list[object] = []
    for _ in range(MAX_DEPTH - 1):  # MAX_DEPTH lists, one in the other.
        deep = [deep]

    assert json_native(deep)
    assert not json_native([deep])
    assert encode_json([deep]) == starlette_body([deep])


def test_domainkits_own_path_is_not_exported() -> None:
    """Only the checked class is the API; domainkit's own is a kind of it."""
    assert domainkit.http.JSONResponse is JSONResponse
    assert "_BuiltJSONResponse" not in domainkit.http.__all__
    assert issubclass(_BuiltJSONResponse, JSONResponse)


JSON_KEYS = (
    st.text()
    | st.integers()
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.booleans()
    | st.none()
)
JSON_VALUES = st.recursive(
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats()
    | st.floats(min_value=1e-10, max_value=1e-3)
    | st.text(),
    lambda children: (
        st.lists(children, max_size=6)
        | st.tuples(children, children)
        | st.dictionaries(JSON_KEYS, children, max_size=6)
    ),
    max_leaves=40,
)


@given(JSON_VALUES)
@example([1e-7, 3.4e-5, {None: float("inf")}])
@example({"a": [0.00001, -0.00009999]})
def test_any_json_value_encodes_as_starlette_encodes_it(content: object) -> None:
    """For any JSON content, by either path: the same bytes, or the same error."""
    expected = starlette_body(content)

    assert fast_body(content) == expected
    assert fast_body(content, _encode_built) == expected


def ones(bits: int) -> int:
    """An integer of ``bits`` ones: from 4,215 digits up, past Python's limit.

    Built by the strategy, as Python cannot write it in a strategy's repr.
    """
    return (1 << bits) - 1


def negative(number: int) -> int:
    """``-number``."""
    return -number


OTHER_VALUES = (
    st.decimals(allow_nan=False)
    | st.datetimes()
    | st.binary(max_size=4)
    | st.uuids()
    | st.sets(st.integers(), max_size=2)
    | st.builds(Row, username=st.text(), password_hash=st.text())
    | st.builds(Card, pan=st.text(), cvv=st.text())
    | st.sampled_from([Color.RED, Rank.FIRST, Plain.A, Odd.X, OddNumber.X, Renamed.A])
    | st.sampled_from([Unreadable.A, FloatKind.HALF, Flags.READ | Flags.WRITE])
    | st.builds(Text, st.text())
    | st.builds(Number, st.floats())
    | st.builds(Lying, st.integers())
    | st.builds(LyingFloat, st.floats())
    | st.integers(min_value=14_000, max_value=2_000_000).map(ones)
    | st.integers(min_value=14_000, max_value=2_000_000).map(ones).map(negative)
)
MIXED_KEYS = (
    JSON_KEYS
    | st.floats()
    | st.sampled_from(
        [
            (1, 2),
            Plain.A,
            Color.RED,
            Odd.X,
            Text("k"),
            NamedNumber(3),
            Number(0.5),
        ]
    )
    | st.integers(min_value=14_000, max_value=50_000).map(ones)
)
MIXED_VALUES = st.recursive(
    JSON_VALUES | OTHER_VALUES,
    lambda children: (
        st.lists(children, max_size=4)
        | st.tuples(children, children)
        | st.dictionaries(MIXED_KEYS, children, max_size=4)
        | st.builds(Reversed, st.dictionaries(st.text(), st.integers(), max_size=3))
    ),
    max_leaves=20,
)


@given(MIXED_VALUES)
@example({"user": Row(username="alice", password_hash="h"), "n": 1})
@example([{1: [Color.RED]}, {"x": Decimal("1.5")}])
def test_any_value_encodes_or_fails_as_starlette(content: object) -> None:
    """JSON or not: the same bytes, or the same error with the same message."""
    assert fast_outcome(content) == starlette_outcome(content)


@pytest.mark.parametrize("encode", ENCODERS)
@given(number=st.floats())
@example(number=5.787418015910799e-05)
@example(number=-2.0428733438587214e-9)
def test_any_float_encodes_as_python_writes_it(
    encode: Callable[[Any], bytes], number: float
) -> None:
    """Every float, alone and in a list: Python's repr, or refused if not finite."""
    if math.isfinite(number):
        assert encode(number) == repr(number).encode()
        assert encode([number, number]) == f"[{number!r},{number!r}]".encode()
    else:
        assert fast_body(number, encode) is ValueError


@pytest.mark.parametrize("encode", ENCODERS)
@given(text=st.text())
def test_any_text_encodes_as_starlette_encodes_it(
    encode: Callable[[Any], bytes], text: str
) -> None:
    """Escapes, non-ASCII, and line separators, as keys and as values."""
    assert encode({text: [text]}) == starlette_body({text: [text]})


class Item(BaseModel):
    """A response model."""

    name: str
    price: float


def test_routes_with_a_response_model_are_left_to_fastapi() -> None:
    """FastAPI writes them with pydantic; a response class would change their bytes.

    So the application sets no default response class (ADR 0063). This
    pins FastAPI's behaviour: an upgrade that changes it fails here.
    """
    app = FastAPI()

    @app.get("/model")
    async def model() -> Item:
        return Item(name="é", price=1e-7)

    @app.get("/classed", response_class=JSONResponse)
    async def classed() -> Item:
        return Item(name="é", price=1e-7)

    with TestClient(app) as client:
        by_pydantic, by_class = client.get("/model"), client.get("/classed")

    assert by_pydantic.content == '{"name":"é","price":1e-7}'.encode()
    assert by_class.content == '{"name":"é","price":1e-07}'.encode()


def test_a_route_without_a_response_model_keeps_its_bytes() -> None:
    """``response_class=JSONResponse`` renders what jsonable_encoder gives, the same."""
    app = FastAPI()
    content = {"name": "é", "price": 1e-7, "when": datetime(2026, 1, 1, tzinfo=UTC)}

    @app.get("/default", response_model=None)
    async def default() -> dict[str, object]:
        return content

    @app.get("/fast", response_model=None, response_class=JSONResponse)
    async def quick() -> dict[str, object]:
        return content

    with TestClient(app) as client:
        before, after = client.get("/default"), client.get("/fast")

    assert after.content == before.content
    assert after.content == (
        '{"name":"é","price":1e-07,"when":"2026-01-01T00:00:00+00:00"}'.encode()
    )
    assert after.headers["content-type"] == before.headers["content-type"]


def test_error_bodies_keep_their_bytes() -> None:
    """``error_response`` and readiness answers render as Starlette rendered them."""
    response = errors.error_response(
        422,
        "The request is invalid.",
        code="validation_error",
        errors=[errors.FieldError(loc=["body", "price", 0], msg="é", type="x")],
    )

    assert type(response) is _BuiltJSONResponse
    assert bytes(response.body) == bytes(
        starlette.JSONResponse(
            {
                "detail": "The request is invalid.",
                "code": "validation_error",
                "errors": [{"loc": ["body", "price", 0], "msg": "é", "type": "x"}],
            }
        ).body
    )


def test_readiness_answers_render_through_the_fast_encoder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/ready`` is domainkit's own ``JSONResponse``, with Starlette's bytes."""
    rendered: list[object] = []

    def recording(content: object) -> bytes:
        rendered.append(content)
        return _encode_built(content)

    monkeypatch.setattr(fast, "_encode_built", recording)
    with TestClient(get_asgi_application()) as client:
        response = client.get("/ready")

    assert rendered == [{"status": "ready"}]
    assert response.content == bytes(starlette.JSONResponse(response.json()).body)


def test_resource_bodies_take_domainkits_own_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pages, items, and error bodies are JSON as built: no check, the same bytes."""
    rendered: list[object] = []

    def recording(content: object) -> bytes:
        rendered.append(content)
        return _encode_built(content)

    def never(content: object) -> bool:
        raise AssertionError("checked")

    monkeypatch.setattr(fast, "_encode_built", recording)
    monkeypatch.setattr(fast, "json_native", never)
    gadget = make_gadget("a", price="3.00")
    store = InMemoryGadgets([gadget])
    app = FastAPI()
    errors.install_exception_handlers(app)
    app.include_router(
        ResourceRouter(
            "/gadgets",
            model=Gadget,
            unit_of_work=lambda: FakeGadgetsUnitOfWork(store),
            repository=lambda uow: uow.gadgets,
            dependencies=[Public],
        )
    )
    client = TestClient(app)

    answers = [
        client.get("/gadgets"),
        client.get(f"/gadgets/{gadget.id}"),
        client.get(f"/gadgets/{UUID(int=0)}"),
    ]

    assert [answer.status_code for answer in answers] == [200, 200, 404]
    assert len(rendered) == 3
    for answer in answers:
        assert answer.content == bytes(starlette.JSONResponse(answer.json()).body)


def test_a_few_lookalikes_are_looked_at_and_pass() -> None:
    """Up to ``TINY_LOOKS`` decimal strings like ``"0.00005"``: still the fast path."""
    content = {"rates": ["0.00005"] * fast.TINY_LOOKS}

    assert not may_differ(to_json(content))
    assert _encode_built(content) == reference_json(content)


def test_text_full_of_lookalikes_goes_to_json_dumps_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past ``TINY_LOOKS`` of them the check stops, and json.dumps encodes it.

    So content made of them costs one more encoding, never a search in
    Python through each of them.
    """
    referenced: list[object] = []
    reference = fast.reference_json

    def counted(content: object) -> bytes:
        referenced.append(content)
        return reference(content)

    many = {"rates": ["0.00005"] * (fast.TINY_LOOKS + 1)}
    flood = [".0000" * 200_000]
    monkeypatch.setattr(fast, "reference_json", counted)

    assert may_differ(to_json(many))
    assert _encode_built(many) == reference(many)
    assert _encode_built(flood) == reference(flood)
    assert referenced == [many, flood]
