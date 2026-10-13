"""OpenTelemetry spans and log correlation (``domainkit[tracing]``).

domainkit creates spans for its own work (commits, worker messages) and
stamps log records with the active trace and span ids. Exporting spans is
the deployment's choice: configure the OpenTelemetry SDK, or run under
``opentelemetry-instrument``, which also traces FastAPI and SQLAlchemy.
Without ``opentelemetry-api`` installed, every helper here does nothing.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Final

try:
    from opentelemetry import trace
except ImportError:  # pragma: no cover - only without the [tracing] extra.
    trace = None  # type: ignore[assignment]

TRACER: Final = "domainkit"

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
