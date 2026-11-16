"""A server span per HTTP request (``domainkit[tracing]``).

List ``TracingMiddleware`` in ``MIDDLEWARE`` right after
``ProxyHeadersMiddleware``. Each HTTP request then runs inside a span of
kind ``SERVER``, named as OpenTelemetry's conventions for HTTP name them:
the method and the route's template (``GET /accounts/{account_id}``), the
method alone when no route matched, ``HTTP`` for a method it does not
know. What the request does inside, its commits and the messages it
queues among them, runs in that span's trace, and so does the worker that
runs those messages later (``domainkit.tracing``).

The span records the method, the scheme, the protocol version, the
route's template (``http.route``), and the status; never the query string,
a header, or a body. ``url.path``, the path as sent, is recorded only with
``ACCESS_LOG_PATH = "raw"``, as for the logs: paths carry ids, tokens, and
emails. An answer from 500 up, or an error that escaped, sets the span's
status to error, with the status or the error's type (``error.type``),
never its message.

A request continues the trace of its ``traceparent`` (and ``tracestate``)
as ``ACCEPT_TRACE_CONTEXT`` says: a trusted proxy's only (the default, as
``ProxyHeadersMiddleware`` judged the connection), any caller's, or
nobody's. A request that arrives inside a span already, one an enclosing
instrumentation opened (``opentelemetry-instrument``), gets none of its
own. Spans are exported as the deployment configures OpenTelemetry's SDK.
Until one is set, no span is opened, since none would be kept; a trace
context the request continues still passes on to the work it does (its
outgoing calls, the messages it queues). WebSocket connections get no
span.
"""

from typing import Final

from opentelemetry import trace
from opentelemetry.trace import Span, SpanKind, StatusCode
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from domainkit.conf import settings
from domainkit.context import UNMATCHED_PATH, logged_path, route_template
from domainkit.http.middleware import PEER
from domainkit.tracing import TRACEPARENT, TRACER, TRACESTATE, remote_parent

type _Attributes = dict[str, str | int]

__all__ = [
    "TracingMiddleware",
]

KNOWN_METHODS: Final = frozenset(
    {
        "CONNECT",
        "DELETE",
        "GET",
        "HEAD",
        "OPTIONS",
        "PATCH",
        "POST",
        "PUT",
        "QUERY",
        "TRACE",
    }
)
"""The methods OpenTelemetry's conventions name; any other is ``_OTHER``."""
OTHER_METHOD: Final = "_OTHER"
SERVER_ERROR: Final = 500
"""Answers from this status up set the span's status to error."""
UNCONFIGURED: Final = (trace.ProxyTracerProvider, trace.NoOpTracerProvider)
"""The providers the OpenTelemetry API has until an SDK is set: they keep no span."""
_TRACEPARENT: Final = TRACEPARENT.encode("latin-1")
_TRACESTATE: Final = TRACESTATE.encode("latin-1")


class TracingMiddleware:
    """Run each HTTP request in a server span named by its route.

    List it right after ``ProxyHeadersMiddleware``, which says whether the
    connection is a trusted proxy's. See the module for what a span holds.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app``, with ``ACCEPT_TRACE_CONTEXT`` and ``ACCESS_LOG_PATH``."""
        self.app = app
        accept = settings.ACCEPT_TRACE_CONTEXT
        self.from_anyone = accept == "anyone"
        """Whether any caller's ``traceparent`` is continued."""
        self.from_proxies = accept == "proxies"
        """Whether a trusted proxy's is (``PEER`` in the scope)."""
        self.raw_path = settings.ACCESS_LOG_PATH == "raw"
        """Whether spans record the path as sent (``url.path``)."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve one request inside its span."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        carried = self.carried(scope)
        if isinstance(trace.get_tracer_provider(), UNCONFIGURED):
            if not carried:
                await self.app(scope, receive, send)
                return
            with remote_parent(carried):  # No span; the trace context passes on.
                await self.app(scope, receive, send)
            return
        if trace.get_current_span().is_recording():  # Traced from above.
            await self.app(scope, receive, send)
            return
        method = scope["method"]
        known = method if method in KNOWN_METHODS else OTHER_METHOD
        name = "HTTP" if known == OTHER_METHOD else known
        attributes: _Attributes = {
            "http.request.method": known,
            "url.scheme": scope.get("scheme", "http"),
        }
        if known != method:
            attributes["http.request.method_original"] = method
        if version := scope.get("http_version"):
            attributes["network.protocol.version"] = version
        if self.raw_path:  # The path as sent, as the logs show it (SEC-10c-1).
            attributes["url.path"] = logged_path(scope)
        status_code: int | None = None

        async def send_with_status(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        tracer = trace.get_tracer(TRACER)
        with (
            remote_parent(carried),
            tracer.start_as_current_span(
                name,
                kind=SpanKind.SERVER,
                attributes=attributes,
                record_exception=False,
                set_status_on_exception=False,
            ) as span,
        ):
            try:
                await self.app(scope, receive, send_with_status)
            except BaseException as exc:
                _finish(span, name, scope, status_code, exc)
                raise
            _finish(span, name, scope, status_code, None)

    def carried(self, scope: Scope) -> dict[str, str]:
        """The trace context the request continues: none unless it is believed.

        One ``traceparent`` line is read (several are no trace context), and
        every ``tracestate`` line, in order.
        """
        if not (self.from_anyone or (self.from_proxies and PEER in scope)):
            return {}
        parents: list[str] = []
        states: list[str] = []
        for key, value in scope["headers"]:
            if key == _TRACEPARENT:
                parents.append(value.decode("latin-1"))
            elif key == _TRACESTATE:
                states.append(value.decode("latin-1"))
        if len(parents) != 1:
            return {}
        carried = {TRACEPARENT: parents[0]}
        if states:
            carried[TRACESTATE] = ",".join(states)
        return carried


def _finish(
    span: Span,
    name: str,
    scope: Scope,
    status_code: int | None,
    error: BaseException | None,
) -> None:
    """Name ``span`` after the route, and record how the request ended."""
    attributes: _Attributes = {}
    route = route_template(scope)
    if route != UNMATCHED_PATH:
        span.update_name(f"{name} {route}")
        attributes["http.route"] = route
    if status_code is not None:
        attributes["http.response.status_code"] = status_code
    if error is not None:
        failure = type(error)
        attributes["error.type"] = (
            failure.__qualname__
            if failure.__module__ == "builtins"
            else f"{failure.__module__}.{failure.__qualname__}"
        )
    elif status_code is None or status_code >= SERVER_ERROR:
        attributes["error.type"] = str(status_code or "incomplete_response")
    span.set_attributes(attributes)
    if "error.type" in attributes:
        span.set_status(StatusCode.ERROR)
