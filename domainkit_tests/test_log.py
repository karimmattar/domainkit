"""Structured logging and the request context."""

import json
import logging
import logging.handlers
import queue
import subprocess
import sys
import threading
import weakref
from collections.abc import Iterator
from typing import Any

import pytest

from domainkit import log
from domainkit.context import RequestContext, bind_request, current_request
from domainkit.exceptions import ImproperlyConfigured
from domainkit.lifecycle import close_resources
from domainkit.log import (
    DEFAULT_QUEUE_SIZE,
    BackgroundHandler,
    BackgroundListener,
    ContextFilter,
    JsonFormatter,
    TextFormatter,
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
