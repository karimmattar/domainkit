"""Opaque cursors for keyset pagination."""

import base64

import pytest

from domainkit.http import InvalidCursor, decode_cursor, encode_cursor


def test_round_trip() -> None:
    """Cursors are URL-safe and decode to the same values."""
    cursor = encode_cursor(["Ünïcode name", "0f1c"])

    assert "=" not in cursor
    assert decode_cursor(cursor, 2) == ["Ünïcode name", "0f1c"]


@pytest.mark.parametrize(
    "cursor",
    [
        "not base64 !",
        base64.urlsafe_b64encode(b"{not json").decode(),
        encode_cursor(["one"]),
        base64.urlsafe_b64encode(b'{"a": 1}').decode(),
        base64.urlsafe_b64encode(b'[1, "x"]').decode(),
        "A" * 3000,
    ],
)
def test_forged_cursors_are_rejected(cursor: str) -> None:
    """Anything but a well-formed cursor of the right size is a 422."""
    with pytest.raises(InvalidCursor) as caught:
        decode_cursor(cursor, 2)

    assert (caught.value.status_code, caught.value.code) == (422, "invalid_cursor")
