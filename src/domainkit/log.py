"""Structured logging: JSON for log pipelines, escaped text for terminals.

Every record gets ``request_id`` and ``client_ip`` from the request context, so
the application, access, and security lines of one request can be joined.
Query strings and request bodies are never logged.
"""

import json
import logging
import logging.config
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final

from domainkit.context import current_request
from domainkit.exceptions import ImproperlyConfigured
from domainkit.tracing import current_ids

SECURITY_LOGGER: Final = "domainkit.security"
"""Rejected requests and other security events (``django.security``)."""
ACCESS_LOGGER: Final = "domainkit.access"
"""One line per HTTP request; replaces uvicorn's access log."""

TEXT_FORMAT: Final = "%(asctime)s %(levelname)s [%(name)s] [%(request_id)s] %(message)s"
_STRUCTURED_FIELDS: Final = ("access",)


class ContextFilter(logging.Filter):
    """Attach request and trace correlation fields to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Add request and trace ids and the client address ("-" if unknown)."""
        request = current_request()
        record.__dict__["request_id"] = request.request_id if request else "-"
        record.__dict__["client_ip"] = (
            request.client_ip if request and request.client_ip else "-"
        )
        ids = current_ids()
        record.__dict__["trace_id"], record.__dict__["span_id"] = ids or ("-", "-")
        return True


class JsonFormatter(logging.Formatter):
    """Render one JSON object per line; control characters are always escaped."""

    def format(self, record: logging.LogRecord) -> str:
        """Serialize the record with UTC timestamps and structured extras."""
        entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in ("request_id", "client_ip", "trace_id", "span_id"):
            value = record.__dict__.get(field)
            if value and value != "-":
                entry[field] = value
        for field in _STRUCTURED_FIELDS:
            if field in record.__dict__:
                entry[field] = record.__dict__[field]
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            entry["stack"] = self.formatStack(record.stack_info)
        return json.dumps(entry, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """Human-readable lines; CR/LF in messages are escaped to stop log forging."""

    def formatMessage(self, record: logging.LogRecord) -> str:
        """Escape line breaks that untrusted input (such as paths) may contain."""
        line = super().formatMessage(record)
        return line.replace("\r", "\\r").replace("\n", "\\n")


def build_logging_config(*, log_format: str, level: str) -> dict[str, Any]:
    """Return a ``LOGGING`` mapping for ``text`` or ``json`` output on stdout."""
    if log_format not in {"text", "json"}:
        raise ImproperlyConfigured(
            f"LOG_FORMAT must be 'text' or 'json', got {log_format!r}."
        )
    level = level.upper()
    if level not in logging.getLevelNamesMapping():
        raise ImproperlyConfigured(f"LOG_LEVEL {level!r} is not a logging level.")
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {"context": {"()": ContextFilter}},
        "formatters": {
            "text": {"()": TextFormatter, "fmt": TEXT_FORMAT},
            "json": {"()": JsonFormatter},
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "formatter": log_format,
                "filters": ["context"],
            },
        },
        "root": {"handlers": ["console"], "level": level},
        "loggers": {
            # Route server logs through the same handler and format.
            "uvicorn": {"handlers": [], "propagate": True},
            "uvicorn.error": {"handlers": [], "propagate": True},
            # Replaced by domainkit.access, which omits query strings.
            "uvicorn.access": {"handlers": [], "propagate": False},
            # Autogenerate announces each plugin it loads; keep migrations readable.
            "alembic.runtime.plugins": {"level": "WARNING"},
        },
    }


def configure_logging(config: Mapping[str, Any]) -> None:
    """Apply a ``LOGGING`` mapping with ``logging.config.dictConfig``."""
    logging.config.dictConfig(dict(config))
