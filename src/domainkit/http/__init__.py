"""FastAPI integration: application factory, middleware, and error mapping.

The names below are imported on first use, so importing a module of this
package (``domainkit.http.errors``) does not build the application's
imports (PERF-15).
"""

from typing import TYPE_CHECKING

from domainkit.utils.module_loading import lazy_names

if TYPE_CHECKING:
    from domainkit.http.application import get_asgi_application
    from domainkit.http.errors import ErrorResponse, error_response, error_responses
    from domainkit.http.filters import EveryWord, Filter, FilterSet, InvalidFilter
    from domainkit.http.pagination import (
        CURSOR_PATTERN,
        MAX_CURSOR_LENGTH,
        InvalidCursor,
        PageOut,
        PageRequest,
        Pagination,
        cursor_length,
        decode_cursor,
        encode_cursor,
    )
    from domainkit.http.problems import APIError, FieldError
    from domainkit.http.responses import JSONResponse

__all__ = [
    "CURSOR_PATTERN",
    "MAX_CURSOR_LENGTH",
    "APIError",
    "ErrorResponse",
    "EveryWord",
    "FieldError",
    "Filter",
    "FilterSet",
    "InvalidCursor",
    "InvalidFilter",
    "JSONResponse",
    "PageOut",
    "PageRequest",
    "Pagination",
    "cursor_length",
    "decode_cursor",
    "encode_cursor",
    "error_response",
    "error_responses",
    "get_asgi_application",
]

__getattr__ = lazy_names(
    __name__,
    {
        "get_asgi_application": "domainkit.http.application",
        **dict.fromkeys(
            ["ErrorResponse", "error_response", "error_responses"],
            "domainkit.http.errors",
        ),
        **dict.fromkeys(["APIError", "FieldError"], "domainkit.http.problems"),
        **dict.fromkeys(
            ["EveryWord", "Filter", "FilterSet", "InvalidFilter"],
            "domainkit.http.filters",
        ),
        **dict.fromkeys(
            [
                "CURSOR_PATTERN",
                "MAX_CURSOR_LENGTH",
                "InvalidCursor",
                "PageOut",
                "PageRequest",
                "Pagination",
                "cursor_length",
                "decode_cursor",
                "encode_cursor",
            ],
            "domainkit.http.pagination",
        ),
        "JSONResponse": "domainkit.http.responses",
    },
)
