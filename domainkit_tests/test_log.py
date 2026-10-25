"""Structured logging and the request context."""

import asyncio
import json
import logging
import logging.handlers
import queue
import random
import subprocess
import sys
import threading
import time
import weakref
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from hypothesis import example, given
from hypothesis import strategies as st
from starlette.types import Message, Receive, Scope, Send

from domainkit import log
from domainkit.clock import freeze_time
from domainkit.conf import override_settings
from domainkit.context import RequestContext, bind_request, current_request
from domainkit.ddd.exceptions import NotFound
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import get_asgi_application
from domainkit.http.middleware import RequestContextMiddleware
from domainkit.lifecycle import close_resources
from domainkit.log import (
    DEFAULT_QUEUE_SIZE,
    AccessSampler,
    BackgroundHandler,
    BackgroundListener,
    ContextFilter,
    JsonFormatter,
    TextFormatter,
    ThrottledLog,
    build_logging_config,
    configure_logging,
    flush_logs,
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
    """The generated configuration writes formatted lines to stdout, from a thread."""
    configure_logging(build_logging_config(log_format="json", level="debug"))

    with bind_request(CONTEXT):
        logging.getLogger("domainkit.test").warning("configured")
    assert flush_logs()

    entry = json.loads(capsys.readouterr().out)
    assert (entry["message"], entry["request_id"]) == ("configured", "req-1")
    (handler,) = logging.getLogger().handlers
    assert isinstance(handler, BackgroundHandler)
    assert handler.queue.maxsize == DEFAULT_QUEUE_SIZE


@pytest.mark.usefixtures("restore_logging")
def test_logging_without_a_queue_writes_on_the_callers_thread(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``queue_size=None`` keeps the plain handler, with the request context."""
    configure_logging(
        build_logging_config(log_format="text", level="info", queue_size=None)
    )

    with bind_request(CONTEXT):
        logging.getLogger("domainkit.test").info("direct")

    assert "[req-1] direct" in capsys.readouterr().out
    (handler,) = logging.getLogger().handlers
    assert type(handler) is logging.StreamHandler


@pytest.mark.parametrize("size", [0, -1])
def test_the_logging_queue_needs_room(size: int) -> None:
    """A queue size below 1 is refused; 1 is the least."""
    with pytest.raises(ImproperlyConfigured, match="queue_size must be 1 or more"):
        build_logging_config(log_format="json", level="INFO", queue_size=size)

    least = build_logging_config(log_format="json", level="INFO", queue_size=1)

    assert least["handlers"]["background"]["queue"]["maxsize"] == 1


@pytest.mark.parametrize("records", [queue.Queue(), queue.SimpleQueue()])
def test_a_handler_needs_a_bounded_queue(records: Any) -> None:  # noqa: ANN401 - any queue.
    """An unbounded queue, or one with no size at all, is refused."""
    with pytest.raises(ImproperlyConfigured) as refused:
        BackgroundHandler(records)

    assert str(refused.value) == (
        "BackgroundHandler needs a bounded queue, such as "
        '{"()": "queue.Queue", "maxsize": 10000}.'
    )


class Collect(logging.Handler):
    """Keeps what it writes, and the thread that wrote it.

    ``block`` makes it wait at its first record until ``resume`` is set,
    after setting ``entered``: the logging thread is then held there.
    """

    def __init__(self, *, block: bool = False, level: int = logging.NOTSET) -> None:
        """Start empty."""
        super().__init__(level)
        self.lines: list[str] = []
        self.threads: set[str] = set()
        self.records: list[logging.LogRecord] = []
        self.block = block
        self.entered = threading.Event()
        self.resume = threading.Event()

    def emit(self, record: logging.LogRecord) -> None:
        """Keep the message and the writing thread."""
        if self.block and not self.entered.is_set():
            self.entered.set()
            self.resume.wait(30)
        self.lines.append(record.getMessage())
        self.records.append(record)
        self.threads.add(threading.current_thread().name)


@pytest.fixture
def collect() -> Collect:
    """A handler that keeps what it writes."""
    return Collect()


@pytest.fixture
def held() -> Iterator[Collect]:
    """A handler that holds the logging thread at its first record."""
    handler = Collect(block=True)
    yield handler
    handler.resume.set()


def record(message: str, *args: object, level: int = logging.INFO) -> logging.LogRecord:
    """A record for the ``app`` logger, at ``level``."""
    return logging.LogRecord("app", level, __file__, 1, message, args, None)


def emit_from_another_thread(handler: logging.Handler, *messages: str) -> None:
    """Emit ``messages``, failing if that takes a blocked caller's time."""
    caller = threading.Thread(
        target=lambda: [handler.handle(record(message)) for message in messages]
    )
    caller.start()
    caller.join(10)
    assert not caller.is_alive(), "the caller waited for the logging thread"


def test_records_are_written_in_order_by_the_logging_thread(collect: Collect) -> None:
    """The caller only queues; one thread writes, in order."""
    handler = BackgroundHandler.writing_to(collect)

    for number in range(50):
        handler.handle(record("line %d", number))
    assert handler.listener is not None
    handler.listener.start()  # Running already: no second thread.
    assert handler.wait_written(10)
    threads = [t for t in threading.enumerate() if t.name == "domainkit-logging"]
    handler.close()

    assert collect.lines == [f"line {number}" for number in range(50)]
    assert collect.threads == {"domainkit-logging"}
    assert threads
    assert all(thread.daemon for thread in threads)  # Never holds up an exit.


def test_a_full_queue_drops_and_counts_without_waiting(held: Collect) -> None:
    """Past the queue's size records are dropped; the count is logged later."""
    handler = BackgroundHandler.writing_to(held, maxsize=2)
    handler.handle(record("first"))
    assert held.entered.wait(10)  # The thread holds "first"; the queue is empty.

    emit_from_another_thread(handler, "queued 1", "queued 2", "lost 1", "lost 2")
    assert handler.dropped == 2
    held.resume.set()
    assert handler.wait_written(10)  # Reports the drops, now there is room.
    handler.handle(record("after"))
    assert handler.wait_written(10)
    handler.close()

    assert held.lines == [
        "first",
        "queued 1",
        "queued 2",
        "Dropped 2 log records (2 INFO): the logging queue was full",
        "after",
    ]
    notice = held.records[3]
    assert (notice.name, notice.levelname, notice.__dict__["request_id"]) == (
        "domainkit.log",
        "WARNING",
        "-",
    )


def test_drops_are_reported_by_the_next_record_that_fits(held: Collect) -> None:
    """The next record with room carries the notice; a notice without room waits."""
    handler = BackgroundHandler.writing_to(held, maxsize=1)
    handler.handle(record("first"))
    assert held.entered.wait(10)
    emit_from_another_thread(handler, "queued", "lost 1", "lost 2")

    # No room for the notice of "lost 1": "lost 2" is dropped with it, and
    # waiting for room again gives up while the thread is held.
    assert handler.dropped == 2
    assert not handler.wait_written(0.01)
    assert handler.dropped == 2
    held.resume.set()
    assert handler.wait_written(10)
    handler.handle(record("after"))  # Room for both now.
    handler.close()

    assert held.lines == [
        "first",
        "queued",
        "Dropped 2 log records (2 INFO): the logging queue was full",
        "after",
    ]


def test_warnings_have_room_that_records_below_them_cannot_fill(
    held: Collect,
) -> None:
    """A security event during a flood of access lines is kept, not dropped.

    Lines below WARNING fill 9 of 10 places; the tenth takes the warning.
    The drops are then reported by level, once there is room.
    """
    handler = BackgroundHandler.writing_to(held, maxsize=10)
    handler.handle(record("first"))
    assert held.entered.wait(10)  # The thread holds "first"; the queue is empty.
    lines = [f"GET /items {number}" for number in range(12)]

    emit_from_another_thread(handler, *lines)
    handler.handle(record("Refused credentials", level=logging.WARNING))
    handler.handle(record("No room left", level=logging.WARNING))
    handler.handle(record("Nor here", level=logging.ERROR))
    assert handler.dropped == 5
    held.resume.set()
    assert handler.wait_written(10)
    handler.close()

    assert held.lines == [
        "first",
        *lines[:9],
        "Refused credentials",
        (
            "Dropped 5 log records (3 INFO, 1 WARNING, 1 ERROR): the logging queue "
            "was full"
        ),
    ]


def test_a_drop_notice_goes_ahead_of_a_record_only_if_both_fit(held: Collect) -> None:
    """With room for one, the record goes; with room for two, the notice first."""
    handler = BackgroundHandler.writing_to(held, maxsize=20)  # 18 below WARNING.
    handler.handle(record("first"))
    assert held.entered.wait(10)
    lines = [f"line {number}" for number in range(19)]

    emit_from_another_thread(handler, *lines)  # 18 fit: "line 18" is dropped.
    handler.handle(record("Refused credentials", level=logging.WARNING))
    held.resume.set()
    assert handler.wait_written(10)
    handler.close()

    assert held.lines == [
        "first",
        *lines[:18],
        "Dropped 1 log records (1 INFO): the logging queue was full",
        "Refused credentials",
    ]
    assert len(held.lines) == 21


class LaggingQueue(queue.Queue[Any]):
    """A queue whose size is read before another thread takes the last place."""

    def qsize(self) -> int:
        """Empty, as it was just before."""
        return 0


def test_a_record_that_loses_the_last_place_to_another_thread_is_dropped(
    held: Collect,
) -> None:
    """There was room when it looked; there is none when it puts: dropped, counted."""
    records = LaggingQueue(1)
    handler = BackgroundHandler(records)
    handler.listener = BackgroundListener(records, held)
    handler.handle(record("first"))
    assert held.entered.wait(10)
    handler.handle(record("queued"))  # Takes the one place.

    handler.handle(record("lost"))

    assert handler.dropped == 1
    held.resume.set()
    handler.close()
    assert held.lines == [
        "first",
        "queued",
        "Dropped 1 log records (1 INFO): the logging queue was full",
    ]


def test_small_queues_keep_no_room_for_warnings(collect: Collect) -> None:
    """Below ten places, records of any level may fill the whole queue."""
    for size, below in [(1, 1), (9, 9), (10, 9), (10_000, 9_000)]:
        handler = BackgroundHandler(queue.Queue(size))
        assert handler._below_warning == below
        handler.close()


def test_a_dropped_count_rides_ahead_of_the_next_record(held: Collect) -> None:
    """With room again, the next record queues the notice first."""
    handler = BackgroundHandler.writing_to(held, maxsize=3)
    handler.handle(record("first"))
    assert held.entered.wait(10)
    emit_from_another_thread(handler, "a", "b", "c", "lost")
    held.resume.set()
    assert handler.listener is not None
    assert handler.listener.flush(10)  # Written, but the drop not yet reported.

    handler.handle(record("after"))
    handler.close()

    assert held.lines[-2:] == [
        "Dropped 1 log records (1 INFO): the logging queue was full",
        "after",
    ]


def test_closing_writes_the_queue_then_writes_on_the_caller(collect: Collect) -> None:
    """After close, records are written by the caller, as a plain handler would."""
    handler = BackgroundHandler.writing_to(collect)
    handler.handle(record("queued"))
    handler.close()
    handler.close()  # Twice is harmless.

    handler.handle(record("direct"))

    assert collect.lines == ["queued", "direct"]
    assert collect.threads == {"domainkit-logging", threading.current_thread().name}
    assert handler.listener is not None
    assert not handler.listener.running
    assert handler.listener.stop()  # Already stopped.


def test_handlers_without_a_listener_write_nothing() -> None:
    """A handler made without ``handlers`` drops records, and waits for none."""
    handler = BackgroundHandler(queue.Queue(5))
    assert handler.listener is None

    handler.handle(record("nowhere"))

    assert handler.wait_written(0.01)
    assert handler.queue.empty()
    handler.close()


def test_stopping_gives_up_on_a_stuck_handler(held: Collect) -> None:
    """A thread held by a blocked stream does not hold shutdown past its time."""
    full = BackgroundHandler.writing_to(held, maxsize=1)
    full.handle(record("held"))
    assert held.entered.wait(10)
    full.handle(record("queued"))
    assert full.listener is not None

    assert not full.listener.flush(0.01)  # No room for the marker.
    assert not full.listener.stop(0.01)  # No room to ask the thread to end.

    other = Collect(block=True)
    roomy = BackgroundHandler.writing_to(other, maxsize=5)
    roomy.handle(record("held"))
    assert other.entered.wait(10)
    assert roomy.listener is not None
    assert not roomy.listener.flush(0.01)  # The thread never reaches the marker.
    assert not roomy.listener.stop(0.01)  # Asked to end, but still busy.
    other.resume.set()


def test_flushing_and_closing_give_up_on_a_stuck_handler(
    held: Collect, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``flush`` and ``close`` wait ``FLUSH_TIMEOUT`` each, not for the stream."""
    monkeypatch.setattr(log, "FLUSH_TIMEOUT", 0.01)
    handler = BackgroundHandler.writing_to(held, maxsize=5)
    handler.handle(record("held"))
    assert held.entered.wait(10)
    handler.handle(record("queued"))

    handler.flush()
    handler.close()

    assert held.lines == []  # Both returned while the stream was still stuck.
    held.resume.set()


class Seconds:
    """The seconds the logging waits read, moved by the test."""

    def __init__(self) -> None:
        """Start at 0."""
        self.now = 0.0

    def __call__(self) -> float:
        """The time now."""
        return self.now


@pytest.fixture
def seconds(monkeypatch: pytest.MonkeyPatch) -> Seconds:
    """The clock of the logging waits' deadlines, under the test's hand."""
    clock = Seconds()
    monkeypatch.setattr(log, "_seconds", clock)
    return clock


class FullQueue(queue.Queue[Any]):
    """A queue whose waits for room use up their timeout on ``clock``, in vain."""

    def __init__(self, clock: Seconds, maxsize: int) -> None:
        """Bounded at ``maxsize``; full to every wait while ``refusing``."""
        super().__init__(maxsize)
        self.clock = clock
        self.refusing = True
        self.waits: list[float | None] = []

    def put(self, item: Any, block: bool = True, timeout: float | None = None) -> None:  # noqa: ANN401
        """Wait out ``timeout`` and find no room; ``put_nowait`` works as ever."""
        if self.refusing and block:
            self.waits.append(timeout)
            self.clock.now += timeout or 0.0
            raise queue.Full
        super().put(item, block, timeout)


def test_the_waits_of_one_flush_share_its_time(held: Collect, seconds: Seconds) -> None:
    """Waiting for room for the drop notice leaves only the rest for the marker."""
    records = FullQueue(seconds, maxsize=5)
    handler = BackgroundHandler(records)
    handler.listener = BackgroundListener(records, held)
    handler.handle(record("held"))
    assert held.entered.wait(10)
    handler._count_dropped(logging.INFO)

    assert not handler.wait_written(5.0)

    assert records.waits == [5.0, 0.0]
    records.refusing = False
    held.resume.set()
    handler.close()


def test_stopping_shares_its_time_between_room_and_the_thread(
    held: Collect, seconds: Seconds, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Room for the stop marker after 2 of 5 seconds: the thread gets 3."""
    records: queue.Queue[Any] = queue.Queue(5)
    listener = BackgroundListener(records, held)
    listener.start()
    worker = listener._worker
    assert worker is not None
    joins: list[float | None] = []
    put = records.put

    def put_later(item: Any, block: bool = True, timeout: float | None = None) -> None:  # noqa: ANN401
        seconds.now += 2.0
        put(item, block, timeout)

    monkeypatch.setattr(records, "put", put_later)
    monkeypatch.setattr(worker, "join", joins.append)

    listener.stop(5.0)

    assert joins == [3.0]
    threading.Thread.join(worker, 10)
    assert not worker.is_alive()


class TimedFlush(log._Flush):
    """A flush marker that records how long it is waited for, and is never reached."""

    waits: list[float | None]

    def __init__(self) -> None:
        """Unreached, its waits recorded on the class."""
        super().__init__()
        self.done = self  # type: ignore[assignment]

    def wait(self, timeout: float | None = None) -> bool:
        """Record ``timeout``; the thread never gets here."""
        self.waits.append(timeout)
        return False

    def set(self) -> None:
        """Ignore the thread reaching it, as if it never did."""


def test_a_listener_flush_shares_its_time_between_room_and_the_thread(
    collect: Collect, seconds: Seconds, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Room for the marker after 2 of 5 seconds: the thread has 3 to reach it."""
    records: queue.Queue[Any] = queue.Queue(5)
    listener = BackgroundListener(records, collect)
    listener.start()
    put = records.put

    def put_later(item: Any, block: bool = True, timeout: float | None = None) -> None:  # noqa: ANN401
        seconds.now += 2.0
        put(item, block, timeout)

    TimedFlush.waits = []
    monkeypatch.setattr(log, "_Flush", TimedFlush)
    monkeypatch.setattr(records, "put", put_later)

    assert not listener.flush(5.0)

    assert TimedFlush.waits == [3.0]
    monkeypatch.undo()
    assert listener.stop(10)


def test_closing_after_a_flush_gave_up_does_not_wait(
    held: Collect, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At exit, a stream stuck through ``flush()`` costs ``close()`` no wait.

    The thread writes on once the stream is free, though nothing waits.
    """
    handler = BackgroundHandler.writing_to(held, maxsize=5)
    handler.handle(record("held"))
    assert held.entered.wait(10)
    handler.handle(record("queued"))
    assert handler.listener is not None
    worker = handler.listener._worker
    assert worker is not None
    monkeypatch.setattr(log, "FLUSH_TIMEOUT", 0.01)
    handler.flush()  # Gives up: the stream is stuck.
    monkeypatch.setattr(log, "FLUSH_TIMEOUT", 3600.0)

    closing = threading.Thread(target=handler.close)
    closing.start()
    closing.join(10)  # A bound for a broken test, not a timing.
    waited = closing.is_alive()
    held.resume.set()
    closing.join()
    worker.join(10)

    assert not waited
    assert held.lines == ["held", "queued"]
    assert not worker.is_alive()


def test_closing_right_after_a_flush_waits_only_for_what_it_left(
    held: Collect, seconds: Seconds, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``flush()`` then ``close()``, as at exit, share one ``FLUSH_TIMEOUT``."""
    monkeypatch.setattr(log, "FLUSH_TIMEOUT", 3600.0)
    handler = BackgroundHandler.writing_to(held, maxsize=5)
    handler.flush()  # Nothing queued: done at 0, of its 3600 seconds.
    handler.handle(record("held"))
    assert held.entered.wait(10)  # The stream is stuck now.
    seconds.now = 3600.0 - 0.01

    closing = threading.Thread(target=handler.close)
    closing.start()
    closing.join(10)  # A bound for a broken test, not a timing.
    waited = closing.is_alive()
    held.resume.set()
    closing.join()

    assert not waited


def test_a_close_long_after_a_flush_has_all_its_time(
    collect: Collect, seconds: Seconds
) -> None:
    """What a flush leaves is shared until its deadline; then it is FLUSH_TIMEOUT."""
    handler = BackgroundHandler.writing_to(collect)
    assert handler._time_left() == log.FLUSH_TIMEOUT  # No flush yet.

    handler.flush()  # At 0: until FLUSH_TIMEOUT.
    seconds.now = 2.0
    shared = handler._time_left()
    seconds.now = log.FLUSH_TIMEOUT
    later = handler._time_left()
    handler.close()

    assert shared == log.FLUSH_TIMEOUT - 2.0
    assert later == log.FLUSH_TIMEOUT


def test_a_failing_handler_does_not_stop_the_thread(collect: Collect) -> None:
    """A handler that raises is reported by its handleError; the rest still write."""
    failures: list[str] = []

    class Broken(logging.Handler):
        def handle(self, record: logging.LogRecord) -> bool:
            raise RuntimeError("broken")

        def handleError(self, record: logging.LogRecord) -> None:
            failures.append(record.getMessage())

    handler = BackgroundHandler.writing_to(Broken(), collect)
    handler.handle(record("one"))
    handler.handle(record("two"))
    handler.close()

    assert failures == ["one", "two"]
    assert collect.lines == ["one", "two"]


@pytest.mark.parametrize(
    ("respect", "written"),
    [
        ({}, ["warned", "loud"]),
        ({"respect_handler_level": True}, ["warned", "loud"]),
        ({"respect_handler_level": False}, ["quiet", "warned", "loud"]),
    ],
)
def test_handler_levels_are_respected(
    respect: dict[str, bool], written: list[str]
) -> None:
    """A handler's level filters what the thread writes to it, unless told not to.

    A record at the level itself is written, and a handler that skips a
    record leaves it to the others.
    """
    warnings = Collect(level=logging.WARNING)
    everything = Collect()
    records: queue.Queue[Any] = queue.Queue(5)
    handler = BackgroundHandler(records)
    handler.listener = BackgroundListener(records, warnings, everything, **respect)

    handler.handle(record("quiet"))
    warned, loud = record("warned"), record("loud")
    warned.levelno, loud.levelno = logging.WARNING, logging.ERROR
    handler.handle(warned)
    handler.handle(loud)
    handler.close()

    assert warnings.lines == written
    assert everything.lines == ["quiet", "warned", "loud"]


def test_records_carry_what_the_caller_saw(collect: Collect) -> None:
    """Context and message are taken on the caller; exceptions format later."""
    handler = BackgroundHandler.writing_to(collect)
    handler.addFilter(ContextFilter())
    logger = logging.getLogger("domainkit.test.background")
    logger.addHandler(handler)
    logger.propagate = False
    items = ["a"]
    try:
        with bind_request(CONTEXT):
            logger.warning("items: %s", items)
            try:
                raise ValueError("bad")
            except ValueError:
                logger.exception("failed")
        items.append("b")
        handler.close()
    finally:
        logger.removeHandler(handler)
        logger.propagate = True

    written, failed = collect.records
    assert written.getMessage() == "items: ['a']"
    assert written.args is None
    assert written.__dict__["request_id"] == "req-1"
    assert failed.exc_info is not None
    entry = json.loads(JsonFormatter().format(failed))
    assert "ValueError: bad" in entry["exception"]


def test_dictconfig_listeners_become_background_listeners(collect: Collect) -> None:
    """Without a ``listener`` entry, dictConfig's QueueListener is replaced."""
    records: queue.Queue[Any] = queue.Queue(5)
    handler = BackgroundHandler(records)
    handler.listener = logging.handlers.QueueListener(
        records, collect, respect_handler_level=False
    )

    assert isinstance(handler.listener, BackgroundListener)
    assert handler.listener.handlers == (collect,)
    assert handler.listener.respect_handler_level is False
    handler.listener = None
    assert handler.listener is None


def test_forked_children_start_over(
    collect: Collect, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child gets a new queue and no thread; its first record starts one."""
    handler = BackgroundHandler.writing_to(collect, maxsize=7)
    handler.handle(record("parent"))
    assert handler.wait_written(10)
    parents = handler.queue
    unwired = BackgroundHandler(queue.Queue(3))
    monkeypatch.setattr(log, "_live_handlers", lambda: [handler, unwired])
    monkeypatch.setattr(log, "_handlers_lock", log._handlers_lock)  # Put back after.

    log._after_fork_in_child()

    assert handler.queue is not parents
    assert handler.queue.maxsize == 7
    assert handler.listener is not None
    assert handler.listener.queue is handler.queue
    assert not handler.listener.running
    handler._count_dropped(logging.INFO)  # As if the child's own queue was full.
    handler._count_dropped(logging.INFO)
    handler.handle(record("child"))
    handler.close()
    parents.put(log._STOP)  # End the parent's thread, which this test left.
    assert collect.lines == [
        "parent",
        "Dropped 2 log records (2 INFO): the logging queue was full",
        "child",
    ]
    assert unwired.queue.maxsize == 3
    assert unwired.listener is None


def test_a_forked_child_never_waits_for_the_parents_lock(
    collect: Collect, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child's copy of the lock stays held: it makes a new one first.

    The lock is held at every fork (``_before_fork``), and may be held by
    any thread of the parent; that thread does not exist in the child.
    """
    handler = BackgroundHandler.writing_to(collect, maxsize=3)
    parents = handler.queue
    held = threading.RLock()
    monkeypatch.setattr(log, "_handlers_lock", held)
    monkeypatch.setattr(log, "_handlers", weakref.WeakSet([handler]))
    held.acquire()  # As at the fork.

    child = threading.Thread(target=log._after_fork_in_child)
    child.start()
    child.join(10)  # A bound for a broken test, not a timing.
    stuck = child.is_alive()
    held.release()  # Lets a stuck child go, so the test ends.
    child.join()

    assert not stuck
    assert log._handlers_lock is not held
    assert free_to_others(log._handlers_lock)
    assert reentrant(log._handlers_lock)
    assert handler.queue is not parents
    assert handler.queue.maxsize == 3
    handler.close()


def free_to_others(lock: Any) -> bool:  # noqa: ANN401 - a Lock or an RLock.
    """Whether another thread could take ``lock`` now."""
    taken: list[bool] = []

    def take() -> None:
        if lock.acquire(blocking=False):
            taken.append(True)
            lock.release()

    other = threading.Thread(target=take)
    other.start()
    other.join()
    return bool(taken)


def reentrant(lock: Any) -> bool:  # noqa: ANN401 - a Lock or an RLock.
    """Whether the thread that holds ``lock`` may take it again."""
    with lock:
        again = lock.acquire(blocking=False)
        if again:
            lock.release()
    return bool(again)


def test_the_handlers_lock_is_reentrant() -> None:
    """A fork from a signal handler, on the thread that holds it, takes it again.

    As ``logging``'s own lock: the hook before a fork must not wait for the
    thread it runs on.
    """
    assert reentrant(log._handlers_lock)
    assert free_to_others(log._handlers_lock)


def test_the_lock_is_held_across_a_fork(monkeypatch: pytest.MonkeyPatch) -> None:
    """Taken before a fork, so no thread changes the handlers then; given back after."""
    lock = threading.Lock()
    monkeypatch.setattr(log, "_handlers_lock", lock)

    log._before_fork()
    taken = lock.locked()
    log._after_fork_in_parent()

    assert taken
    assert not lock.locked()


FORK_WHILE_HELD = """
import os, threading, warnings
from domainkit import log

held, release = threading.Event(), threading.Event()

def free():
    taken = []
    def take():
        if log._handlers_lock.acquire(blocking=False):
            taken.append(True)
            log._handlers_lock.release()
    other = threading.Thread(target=take)
    other.start()
    other.join()
    return bool(taken)

def hold():
    with log._handlers_lock:
        held.set()
        release.wait()

holder = threading.Thread(target=hold)
holder.start()
held.wait()
# Hooks run newest first: the holder lets go as the fork starts, before
# domainkit's own hook takes the lock.
os.register_at_fork(before=release.set)
with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    pid = os.fork()
if pid == 0:
    os._exit(7)
statuses = []
waiter = threading.Thread(target=lambda: statuses.append(os.waitpid(pid, 0)[1]))
waiter.start()
waiter.join(60)
if waiter.is_alive():
    os.kill(pid, 9)
    waiter.join()
    print("child=stuck")
else:
    print(f"child={os.waitstatus_to_exitcode(statuses[0])}")
holder.join()
print(f"parent-lock-free={free()}")
with log._handlers_lock:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        pid = os.fork()
    if pid == 0:
        os._exit(8)
print(f"own-child={os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])}")
print(f"lock-free-after={free()}")
"""


def test_a_fork_while_a_thread_holds_the_lock_leaves_a_working_child() -> None:
    """A real fork, in a process of its own: the child runs, the parent goes on.

    Then the thread that holds the lock forks, as a signal handler that
    interrupted it would: the lock is reentrant, so it does not wait for
    itself.
    """
    child = subprocess.run(  # noqa: S603 - this interpreter, fixed code.
        [sys.executable, "-c", FORK_WHILE_HELD],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert child.returncode == 0, child.stderr
    assert child.stdout.split() == [
        "child=7",
        "parent-lock-free=True",
        "own-child=8",
        "lock-free-after=True",
    ]
    assert child.stderr == ""  # No at-fork hook failed (they are only printed).


def test_flush_logs_waits_for_every_handler(
    collect: Collect, held: Collect, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every handler is waited for; a stuck one makes the answer False."""
    good = BackgroundHandler.writing_to(collect)
    stuck = BackgroundHandler.writing_to(held)
    good.handle(record("written"))
    stuck.handle(record("held"))
    assert held.entered.wait(10)
    monkeypatch.setattr(log, "_live_handlers", lambda: [good, stuck])

    assert not flush_logs(0.01)
    assert collect.lines == ["written"]
    held.resume.set()
    assert flush_logs(10)
    good.close()
    stuck.close()


@pytest.mark.anyio
async def test_shutdown_writes_queued_records(
    held: Collect, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``close_resources`` waits for the logging thread; nothing to wait, no thread."""
    handler = BackgroundHandler.writing_to(held)
    handler.handle(record("held"))
    assert held.entered.wait(10)
    handler.handle(record("before shutdown"))
    waiting = handler.wait_written

    def released_by_flush(timeout: float) -> bool:
        held.resume.set()  # Only a flush lets the thread go on.
        return waiting(timeout)

    monkeypatch.setattr(handler, "wait_written", released_by_flush)
    monkeypatch.setattr(log, "_live_handlers", lambda: [handler])

    await close_resources()

    assert held.lines == ["held", "before shutdown"]
    handler.close()
    monkeypatch.setattr(log, "_live_handlers", list)
    monkeypatch.setattr(log, "flush_logs", lambda timeout: pytest.fail("no wait"))
    await log.aflush_logs()


@pytest.mark.anyio
async def test_shutdown_waits_for_logs_as_long_as_asked(
    collect: Collect, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``aflush_logs`` waits the seconds it is given, ``FLUSH_TIMEOUT`` by default."""
    waited: list[float] = []
    handler = BackgroundHandler.writing_to(collect)
    monkeypatch.setattr(log, "_live_handlers", lambda: [handler])
    monkeypatch.setattr(log, "flush_logs", waited.append)

    await log.aflush_logs(1.5)
    await log.aflush_logs()

    assert waited == [1.5, log.FLUSH_TIMEOUT]
    handler.close()


def reference_line(formatter: logging.Formatter, record: logging.LogRecord) -> str:
    """The line domainkit 0.8.0's ``JsonFormatter`` wrote: ``json.dumps`` of a dict."""
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
    if "access" in record.__dict__:
        entry["access"] = record.__dict__["access"]
    if record.exc_info and not record.exc_text:
        record.exc_text = formatter.formatException(record.exc_info)
    if record.exc_text:
        entry["exception"] = record.exc_text
    if record.stack_info:
        entry["stack"] = formatter.formatStack(record.stack_info)
    return json.dumps(entry, default=str, ensure_ascii=False)


class Named:
    """A value JSON has no type for: the line shows its ``str``."""

    def __str__(self) -> str:
        """Its name."""
        return "named é"


class Text(str):
    """A string subclass."""


ODD_VALUES: list[object] = [
    "",
    "-",
    'é\n\r\t"\\\x00\x1f\u2028😀',
    Text("subclassed"),
    0,
    7,
    -1.5,
    float("nan"),
    float("inf"),
    None,
    True,
    ["a", 1, None],
    {"k": [1, {"x": None}], "n": 2.5},
    Named(),
]


def access_record(**fields: object) -> logging.LogRecord:
    """An access line, merged as the background handler merges it, with ``fields``."""
    line = logging.LogRecord(
        "domainkit.access",
        logging.INFO,
        __file__,
        1,
        "%s %s %s %.1fms",
        ("GET", "/hello", 200, 0.4),
        None,
    )
    line.__dict__.update(
        request_id="9f2c1d7e8a6b4c3d9e0f1a2b3c4d5e6f",
        client_ip="203.0.113.7",
        trace_id="-",
        span_id="-",
        access={"method": "GET", "path": "/hello", "status": 200, "duration_ms": 0.4},
    )
    line.__dict__.update(fields)
    return line


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"request_id": "-", "client_ip": "-"},
        {"trace_id": "4bf92f3577b34da6a3ce929d0e0e4736", "span_id": "00f067aa0ba902b7"},
        {
            "access": {
                "method": "WS",
                "path": "/ws",
                "status": None,
                "duration_ms": 1e-7,
            }
        },
        {"name": "é.\n", "levelname": "LOUD\x00"},
        {"msg": "line\r\nforged", "args": None},
        {"stack_info": "Stack (most recent call last):\n  é"},
        {"created": 1792575015.9999996},
        {"created": 0.0},
        {"created": -1.5},
        *({field: value} for field in ("request_id", "access") for value in ODD_VALUES),
        {"levelname": 20, "name": Named()},
    ],
)
def test_json_lines_are_byte_for_byte_what_json_dumps_wrote(
    fields: dict[str, object],
) -> None:
    """Log pipelines parse these lines: every byte stays as it was."""
    formatter = JsonFormatter()
    line = access_record(**fields)
    twin = access_record(**{"created": line.created, **fields})
    expected = reference_line(formatter, twin)

    assert formatter.format(line) == expected


def test_json_lines_with_exceptions_are_what_json_dumps_wrote() -> None:
    """The traceback, formatted once, is the ``exception`` field."""
    formatter = JsonFormatter()
    try:
        raise ValueError("bad é")
    except ValueError:
        failed = access_record()
        again = access_record(created=failed.created)
        failed.exc_info = again.exc_info = sys.exc_info()

    assert formatter.format(failed) == reference_line(formatter, again)
    assert failed.exc_text is not None
    assert formatter.format(failed) == reference_line(formatter, again)  # Cached.


@given(
    st.dictionaries(
        st.sampled_from(["request_id", "client_ip", "trace_id", "span_id", "access"]),
        st.recursive(
            st.none() | st.booleans() | st.integers() | st.floats() | st.text(),
            lambda children: (
                st.lists(children, max_size=4)
                | st.dictionaries(st.text(max_size=5), children, max_size=4)
            ),
            max_leaves=12,
        ),
    ),
    st.text(),
    st.floats(min_value=-2e9, max_value=2.5e11),
)
def test_any_record_formats_as_json_dumps_wrote_it(
    fields: dict[str, object], message: str, created: float
) -> None:
    """Whatever the extras, message, and time: the same line."""
    formatter = JsonFormatter()
    line = access_record(msg=message, args=None, created=created, **fields)

    assert formatter.format(line) == reference_line(formatter, line)


@given(st.floats(min_value=-2e9, max_value=253402300799.0))
@example(1792575015.9995)
@example(1792575015.99949999)
@example(1792575015.9999995)
@example(1792575015.0000005)
@example(1792575015.9999993)  # A microsecond short of the next second.
@example(0.9999994)
@example(-0.0000004)
@example(0.0)
def test_timestamps_are_what_datetime_writes(created: float) -> None:
    """Rounded and cut as ``datetime.fromtimestamp(...).isoformat`` does."""
    expected = datetime.fromtimestamp(created, UTC).isoformat(timespec="milliseconds")

    assert JsonFormatter().timestamp(created) == expected


def test_the_date_and_time_are_worked_out_once_a_second(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lines within one second reuse its date and time; the next works out its own."""
    worked_out: list[int] = []
    utc_second = log.utc_second

    def counted(second: int) -> str:
        worked_out.append(second)
        return utc_second(second)

    formatter = JsonFormatter()
    monkeypatch.setattr(log, "utc_second", counted)

    stamps = [formatter.timestamp(t) for t in (100.1, 100.5, 100.9999999, 101.2)]

    assert stamps == [
        "1970-01-01T00:01:40.100+00:00",
        "1970-01-01T00:01:40.500+00:00",
        "1970-01-01T00:01:41.000+00:00",  # Rounds up into the next second.
        "1970-01-01T00:01:41.200+00:00",
    ]
    assert worked_out == [100, 101]


class CopiedOwnWay(logging.LogRecord):
    """A record kind with its own way of being copied."""

    copies = 0

    def __copy__(self) -> "CopiedOwnWay":
        """Count the copy, then copy the fields."""
        CopiedOwnWay.copies += 1
        twin = CopiedOwnWay.__new__(CopiedOwnWay)
        twin.__dict__.update(self.__dict__)
        return twin


def test_prepare_copies_plain_records_field_by_field() -> None:
    """The copy holds the caller's fields and the merged message; the original stays."""
    handler = BackgroundHandler(queue.Queue(5))
    original = access_record()
    fields = dict(original.__dict__)

    prepared = handler.prepare(original)
    handler.close()

    assert type(prepared) is logging.LogRecord
    assert prepared is not original
    assert original.__dict__ == fields  # Untouched.
    assert prepared.__dict__ == {
        **fields,
        "message": "GET /hello 200 0.4ms",
        "msg": "GET /hello 200 0.4ms",
        "args": None,
    }
    assert prepared.__dict__["access"] is fields["access"]  # Shallow, as copy.copy.


def test_prepare_copies_other_record_kinds_their_own_way() -> None:
    """A record class of its own is copied by ``copy.copy``, so by its ``__copy__``."""
    handler = BackgroundHandler(queue.Queue(5))
    original = CopiedOwnWay("app", logging.INFO, __file__, 1, "hi %s", ("you",), None)
    CopiedOwnWay.copies = 0

    prepared = handler.prepare(original)
    handler.close()

    assert type(prepared) is CopiedOwnWay
    assert CopiedOwnWay.copies == 1
    assert (prepared.msg, prepared.args, original.args) == ("hi you", None, ("you",))


class Draws:
    """Numbers ``AccessSampler`` draws, in order, and how many it drew."""

    def __init__(self, *numbers: float) -> None:
        """Return ``numbers``, one per draw."""
        self.numbers = list(numbers)
        self.drawn = 0

    def __call__(self) -> float:
        """The next number."""
        self.drawn += 1
        return self.numbers.pop(0)


def test_everything_is_logged_at_the_default_rate() -> None:
    """At 1.0 nothing is drawn, and every line is written."""
    draws = Draws()
    sampler = AccessSampler(draw=draws)

    assert all(
        sampler.keep(status, seconds)
        for status in (200, 201, 204, 302, 404, 422, None)
        for seconds in (0.0, 0.5, 30.0)
    )
    assert draws.drawn == 0


@pytest.mark.parametrize(
    "status",
    [None, 500, 502, 503, 599, 400, 401, 403, 404, 405, 409, 413, 422, 429, 451, 499],
)
def test_errors_and_refusals_are_always_logged(status: int | None) -> None:
    """Whatever the rate, without a draw: every status from 400 up, or none.

    A scoped resource answers 404 for what the caller may not reach, so
    probing another tenant's ids leaves a line for every probe.
    """
    draws = Draws()

    assert AccessSampler(0.0, 1.0, draw=draws).keep(status, 0.0)
    assert draws.drawn == 0


@pytest.mark.parametrize("status", [100, 101, 200, 201, 204, 206, 301, 302, 304, 399])
def test_successes_and_redirects_are_sampled(status: int) -> None:
    """Below 400 the draw decides: at rate 0, no line."""
    draws = Draws(0.0)

    assert not AccessSampler(0.0, 1.0, draw=draws).keep(status, 0.0)
    assert draws.drawn == 1


def test_slow_requests_are_always_logged() -> None:
    """From ``slow`` seconds on, a line is written without a draw."""
    draws = Draws(0.99)
    sampler = AccessSampler(0.0, 2.5, draw=draws)

    assert sampler.keep(200, 2.5)
    assert sampler.keep(302, 60.0)
    assert not sampler.keep(200, 2.4999)
    assert draws.drawn == 1


@pytest.mark.parametrize(
    ("rate", "draw", "kept"),
    [(0.25, 0.0, True), (0.25, 0.2499, True), (0.25, 0.25, False), (0.0, 0.0, False)],
)
def test_other_lines_are_kept_below_the_rate(
    rate: float, draw: float, kept: bool
) -> None:
    """A line is written when its draw falls under the rate: never at 0."""
    assert AccessSampler(rate, 1.0, draw=Draws(draw)).keep(200, 0.0) is kept


def test_the_sample_is_fair() -> None:
    """A rate of a quarter keeps a quarter of 40,000 lines, of each status."""
    numbers = random.Random(20261021)  # noqa: S311 - a fixed seed, a repeatable sample.
    sampler = AccessSampler(0.25, 1.0, draw=numbers.random)
    kept = {
        status: sum(sampler.keep(status, 0.0) for _ in range(10_000))
        for status in (200, 201, 204, 302)
    }

    assert all(2_400 <= count <= 2_600 for count in kept.values()), kept


@pytest.mark.parametrize(
    ("rate", "slow", "refused"),
    [
        (-0.1, 1.0, "sample rate must be from 0 to 1, not -0.1"),
        (1.5, 1.0, "sample rate must be from 0 to 1, not 1.5"),
        (float("nan"), 1.0, "sample rate must be from 0 to 1, not nan"),
        (0.5, 0.0, "slow duration must be positive, not 0.0"),
        (0.5, float("nan"), "slow duration must be positive, not nan"),
    ],
)
def test_sampler_arguments_are_checked(rate: float, slow: float, refused: str) -> None:
    """A rate outside 0 to 1, or a duration that is not positive, is refused."""
    with pytest.raises(ValueError, match=refused):
        AccessSampler(rate, slow)


@pytest.mark.parametrize(
    ("setting", "value", "message"),
    [
        ("ACCESS_LOG_SAMPLE_RATE", 1.01, "less than or equal to 1"),
        ("ACCESS_LOG_SAMPLE_RATE", -0.01, "greater than or equal to 0"),
        ("ACCESS_LOG_SLOW_DURATION", 0.0, "greater than 0"),
        ("ACCESS_LOG_SLOW_DURATION", 3600.5, "less than or equal to 3600"),
        ("ACCESS_LOG_SLOW_DURATION", float("inf"), "finite number"),
    ],
)
def test_access_log_settings_are_bounded(
    setting: str, value: float, message: str
) -> None:
    """A sample rate from 0 to 1; a slow duration up to an hour."""
    with pytest.raises(ImproperlyConfigured, match=message):
        override_settings(**{setting: value}).__enter__()


async def answered(scope: Scope, receive: Receive, send: Send) -> None:
    """Answer the status the path names: ``/status/404`` answers 404."""
    code = int(scope["path"].rsplit("/", 1)[1])
    await send({"type": "http.response.start", "status": code, "headers": []})
    await send({"type": "http.response.body", "body": b""})


def statuses_logged(
    middleware: RequestContextMiddleware,
    codes: list[int],
    caplog: pytest.LogCaptureFixture,
) -> list[tuple[str, int]]:
    """The access lines ``middleware`` writes for requests answered ``codes``."""
    caplog.set_level(logging.DEBUG, logger="domainkit.access")

    async def requests() -> None:
        async def receive() -> Message:
            return {"type": "http.request", "body": b""}

        async def send(message: Message) -> None:
            pass

        for code in codes:
            scope = {"type": "http", "method": "GET", "path": f"/status/{code}"}
            await middleware({**scope, "headers": []}, receive, send)

    asyncio.run(requests())
    return [
        (line.getMessage().split()[1], line.levelno)
        for line in caplog.records
        if line.name == "domainkit.access"
    ]


def test_a_sampled_access_log_keeps_errors_and_refusals(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """At rate 0, successful and redirected requests alone leave no line."""
    with override_settings(ACCESS_LOG_SAMPLE_RATE=0.0):
        middleware = RequestContextMiddleware(answered)
    codes = [200, 204, 302, 400, 404, 401, 403, 409, 422, 429, 500]

    logged = statuses_logged(middleware, codes, caplog)

    assert logged == [
        ("/status/400", logging.WARNING),
        ("/status/404", logging.WARNING),
        ("/status/401", logging.WARNING),
        ("/status/403", logging.WARNING),
        ("/status/409", logging.WARNING),
        ("/status/422", logging.WARNING),
        ("/status/429", logging.WARNING),
        ("/status/500", logging.ERROR),
    ]


scoped = APIRouter()
"""Accounts as a scoped resource answers them: 404 for those of other tenants."""


@scoped.get("/accounts/{number}")
async def account(number: int) -> dict[str, int]:
    """Account 1 is the caller's; any other is out of their reach."""
    if number != 1:
        raise NotFound(f"Account {number} not found.")
    return {"account": number}


def test_probing_a_sampled_application_leaves_a_line_per_probe(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """At rate 0, every id tried that is not the caller's is in the access log.

    So are the requests the application refuses before a route runs (405,
    422); the caller's own reads are left out.
    """
    caplog.set_level(logging.INFO, logger="domainkit.access")
    with (
        override_settings(
            ROOT_ROUTER=f"{__name__}.scoped",
            MIDDLEWARE=["domainkit.http.middleware.RequestContextMiddleware"],
            ACCESS_LOG_SAMPLE_RATE=0.0,
            ACCESS_LOG_SLOW_DURATION=3600.0,  # No request is slow enough to count.
        ),
        TestClient(get_asgi_application()) as client,
    ):
        answers = [client.get(f"/accounts/{n}").status_code for n in range(1, 41)]
        answers += [
            client.delete("/accounts/2").status_code,
            client.get("/accounts/two").status_code,
        ]

    lines = [r.getMessage() for r in caplog.records if r.name == "domainkit.access"]
    assert answers == [200] + [404] * 39 + [405, 422]
    assert [line.rsplit(" ", 1)[0] for line in lines] == [
        *(f"GET /accounts/{n} 404" for n in range(2, 41)),
        "DELETE /accounts/2 405",
        "GET /accounts/two 422",
    ]


def test_the_full_access_log_is_the_default(caplog: pytest.LogCaptureFixture) -> None:
    """At the default rate every request has its line, as before."""
    middleware = RequestContextMiddleware(answered)

    logged = statuses_logged(middleware, [200, 404, 500], caplog)

    assert [path for path, _ in logged] == ["/status/200", "/status/404", "/status/500"]
    assert middleware.sampler.rate == 1.0


def test_a_slow_request_is_logged_whatever_the_rate(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Past ``ACCESS_LOG_SLOW_DURATION`` the line is written, with its duration."""
    caplog.set_level(logging.INFO, logger="domainkit.access")
    with override_settings(ACCESS_LOG_SAMPLE_RATE=0.0, ACCESS_LOG_SLOW_DURATION=2.0):
        middleware = RequestContextMiddleware(answered)
    context = RequestContext("req-1", None, "GET", "/slow")
    now = time.perf_counter()

    middleware._log_access(context, 200, now - 1.0)  # Fast enough: sampled out.
    middleware._log_access(context, 200, now - 5.0)  # Started 5 seconds ago.

    (line,) = [r for r in caplog.records if r.name == "domainkit.access"]
    assert line.getMessage().startswith("GET /slow 200 5")
    assert line.__dict__["access"]["duration_ms"] >= 5000


class Untouchable(AccessSampler):
    """A sampler that fails the test if it is asked."""

    def keep(self, status: int | None, seconds: float) -> bool:
        """Never to be called."""
        raise AssertionError("asked about a line the level drops")


def test_lines_the_level_drops_are_not_built(monkeypatch: pytest.MonkeyPatch) -> None:
    """No sampling, no record: the level is checked first."""
    access = logging.getLogger("domainkit.access")
    built: list[object] = []
    monkeypatch.setattr(access, "_log", lambda *args, **kwargs: built.append(args))
    middleware = RequestContextMiddleware(answered)
    middleware.sampler = Untouchable()
    context = RequestContext("req-1", None, "GET", "/")
    level = access.level
    access.setLevel(logging.ERROR)
    try:
        middleware._log_access(context, 200, time.perf_counter())
        middleware._log_access(context, 404, time.perf_counter())
    finally:
        access.setLevel(level)

    assert built == []


def test_the_slow_duration_is_a_second_unless_set() -> None:
    """``AccessSampler(rate)`` writes every request of a second or more."""
    sampler = AccessSampler(0.0, draw=Draws(0.99))

    assert sampler.keep(200, 1.0)
    assert not sampler.keep(200, 0.999)


def test_json_formatter_takes_logging_formatters_arguments() -> None:
    """Positional and keyword arguments reach ``logging.Formatter``, as before."""
    positional = JsonFormatter(None, "%H:%M")
    keyword = JsonFormatter(datefmt="%M")
    line = access_record()

    assert (positional.datefmt, keyword.datefmt) == ("%H:%M", "%M")
    assert positional.format(line) == reference_line(positional, line)


def test_dates_are_utc_whatever_the_local_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cached date and time are UTC's, not the machine's."""
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    time.tzset()
    try:
        stamp = JsonFormatter().timestamp(0.5)
        local = datetime.fromtimestamp(0).isoformat()  # noqa: DTZ006 - the zone's.
    finally:
        monkeypatch.undo()
        time.tzset()

    assert local == "1970-01-01T09:00:00"  # The zone took effect.
    assert stamp == "1970-01-01T00:00:00.500+00:00"


def test_access_lines_carry_the_request_and_its_rounded_duration(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Method, path, status, and milliseconds to one place; 400 and up warn."""
    caplog.set_level(logging.DEBUG, logger="domainkit.access")
    middleware = RequestContextMiddleware(answered)
    context = RequestContext("req-1", None, "POST", "/orders")
    monkeypatch.setattr(time, "perf_counter", lambda: 100.0)

    middleware._log_access(context, 400, 98.76544)  # 1,234.56 ms ago.
    middleware._log_access(context, 399, 99.5)
    monkeypatch.undo()

    lines = [r for r in caplog.records if r.name == "domainkit.access"]
    assert [(r.getMessage(), r.levelno) for r in lines] == [
        ("POST /orders 400 1234.6ms", logging.WARNING),
        ("POST /orders 399 500.0ms", logging.INFO),
    ]
    assert lines[0].__dict__["access"] == {
        "method": "POST",
        "path": "/orders",
        "status": 400,
        "duration_ms": 1234.6,
    }


def test_flush_waits_and_a_close_after_giving_up_waits_no_more(
    collect: Collect, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``flush()`` waits ``FLUSH_TIMEOUT``; when it gave up, ``close()`` waits 0."""
    handler = BackgroundHandler.writing_to(collect)
    waits: list[float] = []

    def stuck(timeout: float) -> bool:
        waits.append(timeout)
        return False

    monkeypatch.setattr(handler, "wait_written", stuck)

    handler.flush()
    handler.close()

    assert waits == [log.FLUSH_TIMEOUT, 0.0]


def test_stopping_waits_for_room_no_longer_than_it_may(
    collect: Collect, seconds: Seconds
) -> None:
    """On a full queue, ``stop`` waits its timeout for room, never without a bound.

    Room is what it needs to ask the thread to end.
    """
    records = FullQueue(seconds, maxsize=5)
    listener = BackgroundListener(records, collect)
    listener.start()
    worker = listener._worker
    assert worker is not None

    stopped = listener.stop(5.0)

    records.refusing = False
    records.put(log._STOP)  # End the thread, which was never asked.
    worker.join(10)
    assert not stopped
    assert records.waits == [5.0]
    assert not worker.is_alive()


def throttled_lines(caplog: pytest.LogCaptureFixture, name: str) -> list[str]:
    """The lines ``name`` wrote, in order."""
    return [record.getMessage() for record in caplog.records if record.name == name]


def test_a_throttled_log_writes_the_first_line_and_counts_the_rest(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Repeats of an event from a key are held back, then counted on the next line.

    Other keys and other events are written at once.
    """
    throttled = ThrottledLog(logging.getLogger("test.throttled"), interval=10.0)
    with freeze_time("2026-01-01") as clock:
        for _ in range(3):
            throttled.warning("host", "ip:203.0.113.9", "Rejected %r", "evil")
        throttled.warning("host", "ip:198.51.100.7", "Rejected %r", "evil")
        throttled.warning("body", "ip:203.0.113.9", "Too large")
        clock.tick(9.9)
        throttled.warning("host", "ip:203.0.113.9", "Rejected %r", "evil")
        clock.tick(0.1)
        throttled.warning("host", "ip:203.0.113.9", "Rejected %r", "worse")
        throttled.warning("host", "ip:203.0.113.9", "Rejected %r", "worse")
        clock.tick(10)
        throttled.warning("host", "ip:203.0.113.9", "Rejected %r", "%s")
        throttled.warning("body", "ip:203.0.113.9", "Too %s", "large")
        for _ in range(2):  # No arguments: the message is not a format.
            throttled.warning("rate", "ip:203.0.113.9", "Over 100%")
        clock.tick(10)
        throttled.warning("rate", "ip:203.0.113.9", "Over 100%")

    assert throttled_lines(caplog, "test.throttled") == [
        "Rejected 'evil'",
        "Rejected 'evil'",
        "Too large",
        "Rejected 'worse' (and 3 more like it since the last line)",
        "Rejected '%s' (and 1 more like it since the last line)",
        "Too large",
        "Over 100%",
        "Over 100% (and 1 more like it since the last line)",
    ]


def test_a_throttled_log_forgets_the_oldest_keys_but_not_their_counts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Past ``keys``, the oldest is let go: with a line for what it held back."""
    throttled = ThrottledLog(logging.getLogger("test.throttled"), keys=2)
    with freeze_time("2026-01-01"):
        throttled.warning("host", "a", "From a")
        throttled.warning("host", "a", "From a")
        throttled.warning("host", "b", "From b")
        throttled.warning("host", "c", "From c")  # Lets a go, which held one.
        throttled.warning("host", "d", "From d")  # Lets b go, which held none.
        throttled.warning("host", "a", "From a")  # New again: written.

    assert throttled_lines(caplog, "test.throttled") == [
        "From a",
        "From b",
        "From c",
        "1 more host line(s) for a were held back",
        "From d",
        "From a",
    ]
    assert list(throttled._seen) == [("host", "d"), ("host", "a")]


def test_a_throttled_log_does_nothing_where_warnings_are_off(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Nothing is counted or kept for a logger that would drop the line."""
    logger = logging.getLogger("test.throttled.quiet")
    throttled = ThrottledLog(logger)
    with caplog.at_level(logging.ERROR, "test.throttled.quiet"):
        throttled.warning("host", "a", "From a")

    assert throttled._seen == {}
    assert throttled_lines(caplog, "test.throttled.quiet") == []
