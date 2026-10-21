"""Fast JSON responses: the same bytes as Starlette's, from pydantic's encoder."""

import enum
import math
from collections import OrderedDict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import example, given
from hypothesis import strategies as st
from pydantic import BaseModel
from pydantic_core import to_json
from starlette import responses as starlette

from domainkit.http import errors, get_asgi_application
from domainkit.http import responses as fast
from domainkit.http.responses import (
    JSONResponse,
    encode_json,
    may_differ,
    reference_json,
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


def fast_body(content: object) -> bytes | type[BaseException]:
    """``encode_json``'s bytes for ``content``, or the type of what it raises."""
    try:
        return encode_json(content)
    except (ValueError, TypeError) as error:
        return type(error)


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
    content: object, written: bytes
) -> None:
    """Small floats and None keys: pydantic's form is caught, Python's written."""
    assert may_differ(to_json(content))
    assert encode_json(content) == written


@pytest.mark.parametrize(
    "content", [float("nan"), [float("inf")], {"a": float("-inf")}, [1, float("nan")]]
)
def test_nan_and_infinity_are_refused(content: object) -> None:
    """As Starlette refuses them: a ValueError, never ``NaN`` or ``null``."""
    with pytest.raises(ValueError, match="Out of range float values"):
        encode_json(content)
    with pytest.raises(ValueError, match="Out of range float values"):
        JSONResponse(content)


@pytest.mark.parametrize(
    "content",
    [
        {"id": "9e3e-7a1b-4c2d", "at": "2026-10-21T09:30:15+00:00"},
        {"rate": "0.00005", "values": [1.00001, 10.00001, 0.0001, 1e-10]},
        {"text": "Nadia, Inge", "name": "INFO"},
        [1, 2.5, "e-7"],
        {"kebab-e-7": "x-e-7"},
    ],
)
def test_ordinary_content_takes_the_fast_path(
    content: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Identifiers, dates, decimal strings, and ordinary floats need no second pass."""

    def no_reference(content: object) -> bytes:
        raise AssertionError("encoded twice")

    expected = reference_json(content)
    monkeypatch.setattr(fast, "reference_json", no_reference)

    assert not may_differ(to_json(content))
    assert encode_json(content) == expected


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
    ],
)
def test_forms_json_dumps_writes_otherwise_are_caught(encoded: bytes) -> None:
    """Each form pydantic writes and Python does not sends the content to json.dumps."""
    assert may_differ(encoded)


def test_beyond_json_values_pydantic_encodes_what_starlette_refuses() -> None:
    """The documented differences: values JSON has no type for, odd keys, long ints.

    Money stays exact either way: a Decimal is a string, never a float.
    """
    moved = OrderedDict([("a", 1), ("b", 2)])
    moved.move_to_end("a")
    backwards = Reversed(a=1, b=2)
    stamp = datetime(2026, 10, 21, 9, 30, 15, 123456, tzinfo=UTC)
    uuid = UUID("12345678-1234-5678-1234-567812345678")

    assert starlette_body({"amount": Decimal("1.10")}) is TypeError
    assert encode_json({"amount": Decimal("1.10")}) == b'{"amount":"1.10"}'
    assert starlette_body([stamp, uuid]) is TypeError
    assert encode_json([stamp, uuid]) == (
        b'["2026-10-21T09:30:15.123456Z","12345678-1234-5678-1234-567812345678"]'
    )
    assert starlette_body({float("nan"): 1}) is ValueError
    assert encode_json({float("nan"): 1}) == b'{"nan":1}'
    assert starlette_body(10**4300) is ValueError
    assert encode_json(10**4300) == b"1" + b"0" * 4300
    assert starlette_body({1, 2}) is TypeError
    assert encode_json({1, 2}) == b"[1,2]"
    assert starlette_body(b"ab") is TypeError
    assert encode_json(b"ab") == b'"ab"'
    assert starlette_body({(1, 2): "a"}) is TypeError
    assert encode_json({(1, 2): "a"}) == b'{"1,2":"a"}'
    assert starlette_body(backwards) == b'{"b":2,"a":1}'
    assert encode_json(backwards) == b'{"a":1,"b":2}'
    # An OrderedDict is written in its own order by both.
    assert encode_json(moved) == starlette_body(moved) == b'{"b":2,"a":1}'


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
    """For any JSON content: the same bytes, or the same error."""
    assert fast_body(content) == starlette_body(content)


@given(st.floats())
@example(5.787418015910799e-05)
@example(-2.0428733438587214e-9)
def test_any_float_encodes_as_python_writes_it(number: float) -> None:
    """Every float, alone and in a list: Python's repr, or refused if not finite."""
    if math.isfinite(number):
        assert encode_json(number) == repr(number).encode()
        assert encode_json([number, number]) == f"[{number!r},{number!r}]".encode()
    else:
        assert fast_body(number) is ValueError


@given(st.text())
def test_any_text_encodes_as_starlette_encodes_it(text: str) -> None:
    """Escapes, non-ASCII, and line separators, as keys and as values."""
    assert encode_json({text: [text]}) == starlette_body({text: [text]})


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

    assert isinstance(response, JSONResponse)
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
    """``/ready`` is a domainkit ``JSONResponse``, with Starlette's bytes."""
    rendered: list[object] = []

    def recording(content: object) -> bytes:
        rendered.append(content)
        return encode_json(content)

    monkeypatch.setattr(fast, "encode_json", recording)
    with TestClient(get_asgi_application()) as client:
        response = client.get("/ready")

    assert rendered == [{"status": "ready"}]
    assert response.content == bytes(starlette.JSONResponse(response.json()).body)
