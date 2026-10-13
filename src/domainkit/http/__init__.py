"""FastAPI integration: application factory, middleware, and error mapping."""

from domainkit.http.application import get_asgi_application
from domainkit.http.errors import APIError, ErrorResponse, FieldError, error_response
from domainkit.http.pagination import (
    CURSOR_PATTERN,
    MAX_CURSOR_LENGTH,
    InvalidCursor,
    decode_cursor,
    encode_cursor,
)

__all__ = [
    "CURSOR_PATTERN",
    "MAX_CURSOR_LENGTH",
    "APIError",
    "ErrorResponse",
    "FieldError",
    "InvalidCursor",
    "decode_cursor",
    "encode_cursor",
    "error_response",
    "get_asgi_application",
]
