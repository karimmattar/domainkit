"""OpenTelemetry spans and log correlation (``domainkit[tracing]``).

domainkit creates spans for its own work (commits, worker messages) and
stamps log records with the active trace and span ids. Exporting spans is
the deployment's choice: configure the OpenTelemetry SDK, or run under
``opentelemetry-instrument``, which also traces FastAPI and SQLAlchemy.
Without ``opentelemetry-api`` installed, every helper here does nothing.

Work that crosses processes carries the W3C trace context
(``traceparent`` and ``tracestate``): ``trace_headers()`` writes it, and
``remote_parent(headers)`` makes it the parent of the spans opened inside,
so a worker's span continues the trace of the request that queued the
work. Headers that do not follow the W3C format are ignored.
"""

import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Final

try:
    from opentelemetry import context as otel_context
    from opentelemetry import trace
except ImportError:  # pragma: no cover - only without the [tracing] extra.
    trace = None  # type: ignore[assignment]

TRACER: Final = "domainkit"
TRACEPARENT: Final = "traceparent"
TRACESTATE: Final = "tracestate"
MAX_TRACESTATE: Final = 512
"""Longest ``tracestate`` kept; longer ones are dropped whole."""
MAX_MEMBERS: Final = 32
"""Most ``tracestate`` members, as the W3C specification allows."""

_TRACEPARENT: Final = re.compile(r"00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})")
_KEY: Final = re.compile(
    r"[a-z][_0-9a-z\-*/]{0,255}|[a-z0-9][_0-9a-z\-*/]{0,240}@[a-z][_0-9a-z\-*/]{0,13}"
)
_VALUE: Final = re.compile(
    r"[\x20-\x2b\x2d-\x3c\x3e-\x7e]{0,255}[\x21-\x2b\x2d-\x3c\x3e-\x7e]"
)

type Attribute = str | int | float | bool


@contextmanager
def span(name: str, **attributes: Attribute) -> Iterator[None]:
    """Run the block inside a span named ``name`` (a no-op without OpenTelemetry)."""
    if trace is None:
        yield
        return
    tracer = trace.get_tracer(TRACER)
    with tracer.start_as_current_span(name, attributes=attributes):
        yield


def current_ids() -> tuple[str, str] | None:
    """Return the active ``(trace id, span id)`` in hex, or None."""
    if trace is None:
        return None
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return None
    return f"{context.trace_id:032x}", f"{context.span_id:016x}"


def trace_headers() -> dict[str, str]:
    """The active span's W3C trace context, as headers; empty without one."""
    if trace is None:
        return {}
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return {}
    headers = {
        TRACEPARENT: (
            f"00-{context.trace_id:032x}-{context.span_id:016x}-"
            f"{context.trace_flags:02x}"
        )
    }
    if state := context.trace_state.to_header():
        headers[TRACESTATE] = state
    return headers


def parse_traceparent(value: object) -> tuple[int, int, int] | None:
    """Return ``(trace id, span id, flags)`` from a version-00 ``traceparent``.

    Anything else, including an all-zero trace or span id, gives None.
    """
    if not isinstance(value, str):
        return None
    match = _TRACEPARENT.fullmatch(value)
    if match is None:
        return None
    trace_id, span_id, flags = (int(part, 16) for part in match.groups())
    if not (trace_id and span_id):
        return None
    return trace_id, span_id, flags


def parse_tracestate(value: object) -> list[tuple[str, str]] | None:
    """Return the ``(key, value)`` members of a ``tracestate``, or None if invalid.

    Empty members are skipped. A header that is too long, has too many
    members, repeats a key, or breaks the W3C grammar anywhere is refused
    whole.
    """
    if not isinstance(value, str) or len(value) > MAX_TRACESTATE:
        return None
    members: list[tuple[str, str]] = []
    for member in value.split(","):
        member = member.strip(" \t")
        if not member:
            continue
        key, equals, item = member.partition("=")
        if not (equals and _KEY.fullmatch(key) and _VALUE.fullmatch(item)):
            return None
        members.append((key, item))
    keys = [key for key, _ in members]
    if len(members) > MAX_MEMBERS or len(set(keys)) != len(keys):
        return None
    return members


@contextmanager
def remote_parent(headers: Mapping[str, object]) -> Iterator[None]:
    """Make the trace context in ``headers`` the parent of spans in the block.

    Without OpenTelemetry, or without a valid ``traceparent``, nothing
    changes. An invalid ``tracestate`` is dropped; the parent still holds.
    """
    parsed = parse_traceparent(headers.get(TRACEPARENT))
    if trace is None or parsed is None:
        yield
        return
    trace_id, span_id, flags = parsed
    parent = trace.SpanContext(
        trace_id=trace_id,
        span_id=span_id,
        is_remote=True,
        trace_flags=trace.TraceFlags(flags),
        trace_state=trace.TraceState(parse_tracestate(headers.get(TRACESTATE)) or []),
    )
    token = otel_context.attach(
        trace.set_span_in_context(trace.NonRecordingSpan(parent))
    )
    try:
        yield
    finally:
        otel_context.detach(token)
