"""The W3C trace context, carried through the outbox from commit to worker."""

import uuid

import pytest
from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, TraceState

from domainkit import tracing
from domainkit.contrib.messaging import Worker, chain, enqueue, task
from domainkit.contrib.messaging.outbox import message_row
from domainkit.contrib.messaging.tables import messages
from domainkit.db import connections
from domainkit_tests.test_db import Note, NoteId, NotesUnitOfWork
from domainkit_tests.test_messaging import (  # noqa: F401 - fixtures.
    immediate_retries,
    messaging,
    outbox,
    ran,
    remember,
    tables,
)
from domainkit_tests.test_observability import (  # noqa: F401 - fixtures.
    EXPORTER,
    fresh_spans,
    tracer_provider,
)

pytestmark = pytest.mark.anyio

TRACE_ID = 0x4BF92F3577B34DA6A3CE929D0E0E4736
SPAN_ID = 0x00F067AA0BA902B7
TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


@task(name="tests.traced_first")
async def traced_first() -> None:
    """The first step of a traced chain."""
    ran.append("first")


@task(name="tests.traced_second")
async def traced_second() -> None:
    """The second step."""
    ran.append("second")


def test_no_span_no_headers() -> None:
    """Without an active span there is nothing to carry."""
    assert tracing.trace_headers() == {}


def test_the_active_span_becomes_headers() -> None:
    """The traceparent names the span; the tracestate comes along when set."""
    with tracing.span("outer"):
        ids = tracing.current_ids()
        flags = trace.get_current_span().get_span_context().trace_flags
        headers = tracing.trace_headers()
    assert ids is not None
    trace_id, span_id = ids
    assert flags & TraceFlags.SAMPLED
    assert headers == {"traceparent": f"00-{trace_id}-{span_id}-{flags:02x}"}

    state = TraceState([("vendor", "value"), ("other", "x")])
    parent = SpanContext(TRACE_ID, SPAN_ID, True, TraceFlags(0), state)
    with trace.use_span(NonRecordingSpan(parent)):
        assert tracing.trace_headers() == {
            "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-00",
            "tracestate": "vendor=value,other=x",
        }


def test_headers_are_optional_without_opentelemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the extra, nothing is written and nothing is restored."""
    monkeypatch.setattr(tracing, "trace", None)

    assert tracing.trace_headers() == {}
    with tracing.remote_parent({"traceparent": TRACEPARENT}):
        assert tracing.current_ids() is None


@pytest.mark.parametrize(
    ("value", "parsed"),
    [
        (TRACEPARENT, (TRACE_ID, SPAN_ID, 1)),
        (
            "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-ff",
            (TRACE_ID, SPAN_ID, 255),
        ),
        ("00-4BF92F3577B34DA6A3CE929D0E0E4736-00F067AA0BA902B7-01", None),
        ("01-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01", None),
        ("ff-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01", None),
        ("00-00000000000000000000000000000000-00f067aa0ba902b7-01", None),
        ("00-4bf92f3577b34da6a3ce929d0e0e4736-0000000000000000-01", None),
        ("00-4bf92f3577b34da6a3ce929d0e0e473-00f067aa0ba902b7-01", None),
        ("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01-extra", None),
        ("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-1", None),
        (" " + TRACEPARENT, None),
        (TRACEPARENT + "\n", None),
        ("", None),
        (None, None),
        (42, None),
    ],
)
def test_traceparent_must_follow_the_w3c_format(value: object, parsed: object) -> None:
    """Version 00, lowercase hex, no all-zero ids, nothing around it."""
    assert tracing.parse_traceparent(value) == parsed


@pytest.mark.parametrize(
    ("value", "parsed"),
    [
        ("vendor=value", [("vendor", "value")]),
        (" a=1 ,\tb=2,, ", [("a", "1"), ("b", "2")]),
        ("tenant@system=x", [("tenant@system", "x")]),
        ("k_-*/0=v v", [("k_-*/0", "v v")]),
        ("", []),
        ("Upper=1", None),
        ("1key=1", None),
        ("key", None),
        ("key=", None),
        ("key=trailing \t", [("key", "trailing")]),  # Spaces around are allowed.
        ("key=a,b", None),
        ("key=a=b", None),
        ("a=1,a=2", None),
        ("k=é", None),
        (",".join(f"k{i}=v" for i in range(33)), None),
        (",".join(f"k{i}=v" for i in range(32)), [(f"k{i}", "v") for i in range(32)]),
        ("k=" + "v" * 256, [("k", "v" * 256)]),
        ("k=" + "v" * 257, None),
        ("a" * 257 + "=v", None),
        ("a" * 256 + "=v", [("a" * 256, "v")]),
        (None, None),
    ],
)
def test_tracestate_must_follow_the_w3c_format(value: object, parsed: object) -> None:
    """Keys, values, member counts, and length are checked; a bad one goes whole."""
    assert tracing.parse_tracestate(value) == parsed


def test_tracestate_is_bounded_in_length() -> None:
    """512 characters at most."""
    longest = "a=" + "v" * 254 + ",b=" + "v" * 253

    assert len(longest) == 512
    assert tracing.parse_tracestate(longest) == [("a", "v" * 254), ("b", "v" * 253)]
    assert tracing.parse_tracestate(longest + "v") is None


def test_a_remote_parent_parents_the_spans_inside() -> None:
    """Spans in the block join the remote trace, under the remote span."""
    with (
        tracing.remote_parent(
            {"traceparent": TRACEPARENT, "tracestate": "vendor=value"}
        ),
        tracing.span("child"),
    ):
        state = trace.get_current_span().get_span_context().trace_state
    (child,) = EXPORTER.get_finished_spans()
    assert child.context is not None
    assert child.parent is not None
    assert (child.context.trace_id, child.parent.span_id) == (TRACE_ID, SPAN_ID)
    assert child.parent.is_remote
    assert dict(state) == {"vendor": "value"}
    assert tracing.current_ids() is None  # Detached after the block.


def test_invalid_trace_context_is_dropped() -> None:
    """A bad traceparent starts a new trace; a bad tracestate is left out."""
    with (
        tracing.remote_parent({"traceparent": "00-bad", "tracestate": "a=1"}),
        tracing.span("fresh"),
    ):
        pass
    with (
        tracing.remote_parent({"traceparent": TRACEPARENT, "tracestate": "A=1"}),
        tracing.span("kept"),
    ):
        state = trace.get_current_span().get_span_context().trace_state
    fresh, kept = EXPORTER.get_finished_spans()
    assert fresh.parent is None
    assert fresh.context is not None
    assert fresh.context.trace_id != TRACE_ID
    assert kept.context is not None
    assert kept.context.trace_id == TRACE_ID
    assert dict(state) == {}


@pytest.mark.usefixtures("tables")
async def test_workers_continue_the_trace_of_the_commit() -> None:
    """The commit's span is the parent of the message's span, across processes."""
    async with NotesUnitOfWork() as uow:
        await uow.notes.add(Note(id=NoteId(uuid.uuid4()), text="t"))
        uow.defer(remember, value=uuid.uuid4())
        await uow.commit()
    (row,) = await outbox()
    headers = row["headers"]
    assert isinstance(headers, dict)

    await Worker().run_once()

    commit, process = EXPORTER.get_finished_spans()
    assert (commit.name, process.name) == ("uow.commit", "message.process")
    assert commit.context is not None
    assert process.context is not None
    assert process.parent is not None
    assert headers["traceparent"] == (
        f"00-{commit.context.trace_id:032x}-{commit.context.span_id:016x}-"
        f"{commit.context.trace_flags:02x}"
    )
    assert process.context.trace_id == commit.context.trace_id
    assert process.parent.span_id == commit.context.span_id


@pytest.mark.usefixtures("tables")
async def test_chains_and_bad_headers_in_the_worker() -> None:
    """A chain's next step is a child of the step before; bad headers start anew."""
    with tracing.span("request"):
        await enqueue(chain(traced_first, traced_second))
    row = message_row(
        kind="task",
        target="tests.traced_first",
        payload={},
        headers={"traceparent": "00-zz", "tracestate": "x"},
        max_attempts=1,
    )
    async with connections.engine().begin() as connection:
        await connection.execute(messages.insert().values(row))

    await Worker().run_once()

    spans = EXPORTER.get_finished_spans()
    request = next(span for span in spans if span.name == "request")
    processed = [span for span in spans if span.name == "message.process"]
    assert request.context is not None
    trace_id = request.context.trace_id
    in_trace = [s for s in processed if s.context and s.context.trace_id == trace_id]
    assert len(in_trace) == 2
    first, second = sorted(in_trace, key=lambda span: span.start_time or 0)
    assert first.parent is not None
    assert second.parent is not None
    assert first.context is not None
    assert first.parent.span_id == request.context.span_id
    assert second.parent.span_id == first.context.span_id
    (stray,) = [s for s in processed if s not in in_trace]
    assert stray.parent is None
    assert sorted(ran) == ["first", "first", "second"]
