"""Opaque cursors for keyset pagination.

A cursor encodes the sort key of the last item a client received, for example
``(name, id)``. It is base64url JSON: opaque to clients, readable in logs.
Decoding validates the shape, so a tampered cursor is a 422, never a 500.
"""

import base64
import json
from collections.abc import Sequence
from typing import Final

from fastapi import status

from domainkit.http.errors import APIError

CURSOR_PATTERN: Final = r"^[A-Za-z0-9_-]+$"
"""Cursors are base64url; use as ``Query(pattern=CURSOR_PATTERN)``."""
MAX_CURSOR_LENGTH: Final = 2048
"""Longest cursor accepted; use as ``Query(max_length=MAX_CURSOR_LENGTH)``.

Enough for a sort key of about 1,500 UTF-8 bytes (a 200-character name in any
script plus an id)."""


class InvalidCursor(APIError):
    """The cursor was not issued by this API, or was altered."""

    def __init__(self) -> None:
        """Answer with 422 and the ``invalid_cursor`` code."""
        super().__init__(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "The cursor is invalid; start again without one.",
            code="invalid_cursor",
        )


def encode_cursor(values: Sequence[str]) -> str:
    """Encode a sort key as an opaque, URL-safe cursor.

    Raises:
        ValueError: The cursor would be longer than ``decode_cursor`` accepts
            (sort keys must stay short).
    """
    raw = json.dumps(list(values), separators=(",", ":"), ensure_ascii=False)
    cursor = base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode()
    if len(cursor) > MAX_CURSOR_LENGTH:
        raise ValueError(f"Sort key too long for a cursor ({len(cursor)} chars).")
    return cursor


def decode_cursor(cursor: str, size: int) -> list[str]:
    """Decode a cursor made by ``encode_cursor`` with ``size`` values.

    Raises:
        InvalidCursor: The cursor is malformed or has the wrong shape.
    """
    if len(cursor) > MAX_CURSOR_LENGTH:
        raise InvalidCursor
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        values = json.loads(base64.urlsafe_b64decode(padded))
    except ValueError as exc:
        raise InvalidCursor from exc
    if (
        not isinstance(values, list)
        or len(values) != size
        or not all(isinstance(value, str) for value in values)
    ):
        raise InvalidCursor
    return values
