"""Structured logging and the request context."""

import json
import logging
import sys

import pytest

from domainkit.context import RequestContext, bind_request, current_request
from domainkit.exceptions import ImproperlyConfigured
from domainkit.log import (
    ContextFilter,
    JsonFormatter,
    TextFormatter,
    build_logging_config,
    configure_logging,
)

CONTEXT = RequestContext(
    request_id="req-1", client_ip="10.0.0.1", method="GET", path="/"
)


def make_record(message: str = "hello %s", *args: object) -> logging.LogRecord:
    """Build a record that went through the context filter."""
    record = logging.LogRecord("app", logging.INFO, __file__, 1, message, args, None)
    ContextFilter().filter(record)
    return record


def test_request_context_is_scoped() -> None:
    """The context exists only inside ``bind_request``."""
    assert current_request() is None
    with bind_request(CONTEXT):
        assert current_request() is CONTEXT
    assert current_request() is None


def test_context_filter_adds_correlation_fields() -> None:
    """Records get the request id and client address, or dashes."""
    outside = make_record()
    with bind_request(CONTEXT):
        inside = make_record()
    with bind_request(RequestContext("req-2", None, "GET", "/")):
        anonymous = make_record()

    assert (outside.__dict__["request_id"], outside.__dict__["client_ip"]) == ("-", "-")
    assert (inside.__dict__["request_id"], inside.__dict__["client_ip"]) == (
        "req-1",
        "10.0.0.1",
    )
    assert anonymous.__dict__["client_ip"] == "-"


def test_json_formatter() -> None:
    """One JSON object per record with context, extras, and tracebacks."""
    with bind_request(CONTEXT):
        record = make_record("hello %s", "world")
    record.__dict__["access"] = {"status": 200}
    try:
        raise ValueError("bad")
    except ValueError:
        record.exc_info = sys.exc_info()
    record.stack_info = "stack"

    entry = json.loads(JsonFormatter().format(record))

    assert entry["message"] == "hello world"
    assert entry["request_id"] == "req-1"
    assert entry["access"] == {"status": 200}
    assert "ValueError: bad" in entry["exception"]
    assert entry["stack"] == "stack"
    assert "request_id" not in json.loads(JsonFormatter().format(make_record()))


def test_text_formatter_escapes_line_breaks() -> None:
    """Untrusted input cannot forge extra log lines."""
    line = TextFormatter("%(message)s").format(make_record("a\r\nb"))

    assert line == "a\\r\\nb"


def test_logging_config_validation() -> None:
    """Only known formats and levels are accepted."""
    with pytest.raises(ImproperlyConfigured, match="LOG_FORMAT"):
        build_logging_config(log_format="xml", level="INFO")
    with pytest.raises(ImproperlyConfigured, match="LOG_LEVEL"):
        build_logging_config(log_format="json", level="LOUD")


@pytest.mark.usefixtures("restore_logging")
def test_configure_logging_applies_the_config(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The generated configuration writes formatted lines to stdout."""
    configure_logging(build_logging_config(log_format="json", level="debug"))

    logging.getLogger("domainkit.test").warning("configured")

    assert json.loads(capsys.readouterr().out)["message"] == "configured"
