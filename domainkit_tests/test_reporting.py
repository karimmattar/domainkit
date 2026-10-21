"""Error reporters run on their own thread: bounded, in the caller's context."""

import threading
from collections.abc import Iterator, Mapping
from typing import ClassVar

import pytest

from domainkit import reporting
from domainkit.conf import override_settings
from domainkit.context import RequestContext, bind_request, current_request
from domainkit.lifecycle import close_resources
from domainkit.reporting import _ReportingThread, flush_reports, report_error

CONTEXT = RequestContext("req-7", "10.0.0.7", "GET", "/")


class Seen:
    """What the reporter saw, and a gate that can hold it."""

    calls: ClassVar[list[tuple[str, str | None, str]]] = []
    entered = threading.Event()
    resume = threading.Event()
    hold = False


def watch(exc: BaseException, context: Mapping[str, object]) -> None:
    """A reporter that records the thread and the request it ran in."""
    if Seen.hold:
        Seen.entered.set()
        Seen.resume.wait(30)
    request = current_request()
    Seen.calls.append(
        (
            str(exc),
            request.request_id if request else None,
            threading.current_thread().name,
        )
    )


@pytest.fixture(autouse=True)
def fresh_seen() -> Iterator[None]:
    """Start each test with nothing seen and the reporter not held."""
    Seen.calls = []
    Seen.entered = threading.Event()
    Seen.resume = threading.Event()
    Seen.hold = False
    yield
    Seen.resume.set()


@pytest.fixture
def worker() -> Iterator[_ReportingThread]:
    """A reporting thread of its own, with room for two waiting reports."""
    thread = _ReportingThread(2)
    yield thread
    thread._queue.put_nowait(lambda: None)  # Wake it, so nothing is left blocked.


def held(worker: _ReportingThread) -> threading.Event:
    """Hold ``worker`` at a call until the returned event is set."""
    entered, resume = threading.Event(), threading.Event()

    def hold() -> None:
        entered.set()
        resume.wait(30)

    assert worker.submit(hold)
    assert entered.wait(10)
    return resume


def test_reporters_run_on_their_thread_in_the_callers_context() -> None:
    """``report_error`` returns at once; the reporter sees the request."""
    Seen.hold = True
    with (
        override_settings(ERROR_REPORTERS=["domainkit_tests.test_reporting.watch"]),
        bind_request(CONTEXT),
    ):
        report_error(ValueError("first"))  # Held in the reporter...
        assert Seen.entered.wait(10)
        report_error(ValueError("second"))  # ...and this one returned anyway.

    assert Seen.calls == []
    Seen.resume.set()
    assert flush_reports()
    assert Seen.calls == [
        ("first", "req-7", "domainkit-reporting"),
        ("second", "req-7", "domainkit-reporting"),
    ]
    assert reporting._reports._worker is not None
    assert reporting._reports._worker.daemon  # Never holds up an exit.


def test_a_failing_reporter_is_named_and_the_others_still_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One reporter's failure is logged with its name; the next one still runs."""
    seen: list[str] = []

    def broken(exc: BaseException, context: Mapping[str, object]) -> None:
        raise RuntimeError("down")

    def kept(exc: BaseException, context: Mapping[str, object]) -> None:
        seen.append(str(exc))

    with caplog.at_level("ERROR", "domainkit.reporting"):
        reporting._report((broken, kept), ValueError("boom"), {})

    assert caplog.messages == [f"Error reporter {broken!r} failed"]
    assert seen == ["boom"]


def test_a_full_queue_drops_reports_and_logs_the_count(
    worker: _ReportingThread, caplog: pytest.LogCaptureFixture
) -> None:
    """Past the queue's room reports are dropped; the next one logs the count."""
    resume = held(worker)
    ran: list[str] = []

    assert worker.submit(lambda: ran.append("a"))
    assert worker.submit(lambda: ran.append("b"))
    assert not worker.submit(lambda: ran.append("lost"))
    assert not worker.submit(lambda: ran.append("lost too"))
    assert worker.dropped == 2
    resume.set()
    assert worker.flush(10)
    with caplog.at_level("WARNING", "domainkit.reporting"):
        assert worker.submit(lambda: ran.append("c"))
        assert worker.submit(lambda: ran.append("d"))  # Reported once only.
    assert worker.flush(10)

    assert ran == ["a", "b", "c", "d"]
    assert caplog.messages == ["Dropped 2 error reports: the reporting queue was full"]


def test_flush_gives_up_after_its_timeout(worker: _ReportingThread) -> None:
    """A held thread makes flush answer False in time, full or not."""
    resume = held(worker)
    assert not worker.flush(0.01)  # Room for the marker; never reached.
    assert worker.submit(lambda: None)
    assert not worker.flush(0.01)  # No room for the marker.
    resume.set()
    assert worker.flush(10)


def test_flushing_an_unused_thread_waits_for_nothing() -> None:
    """Before the first report there is no thread to wait for."""
    assert _ReportingThread(1).flush(0.01)


def test_the_thread_survives_calls_that_fail(
    worker: _ReportingThread, caplog: pytest.LogCaptureFixture
) -> None:
    """A call that raises is logged, and the thread goes on."""
    ran: list[str] = []

    def fail() -> None:
        raise RuntimeError("broken")

    assert worker.submit(fail)
    assert worker.submit(lambda: ran.append("after"))
    assert worker.flush(10)

    logged = [r.getMessage() for r in caplog.records if r.name == reporting.__name__]
    assert logged == ["Error reporting failed"]
    assert ran == ["after"]


def test_a_thread_that_ended_is_started_again() -> None:
    """Should the thread end, the next report or flush starts another."""
    worker = _ReportingThread(2)
    ended = threading.Thread(target=lambda: None)
    ended.start()
    ended.join()
    worker._worker = ended

    assert worker.flush(10)

    assert worker._worker is not ended
    assert worker._worker is not None
    assert worker._worker.is_alive()


def test_forked_children_start_over(worker: _ReportingThread) -> None:
    """A child has no thread and a fresh queue; the parent's reports are its own."""
    resume = held(worker)
    parents_queue = worker._queue

    worker._reset()

    assert worker._worker is None
    assert worker._queue is not parents_queue
    assert worker._queue.maxsize == 2
    resume.set()
    parents_queue.put_nowait(lambda: None)


@pytest.mark.anyio
async def test_shutdown_sends_waiting_reports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``close_resources`` waits for waiting reports; with no thread, for nothing."""
    ran: list[str] = []
    resume = threading.Event()

    class ReleasedByFlush(_ReportingThread):
        def flush(self, timeout: float) -> bool:
            resume.set()  # Only a flush lets the held report go.
            return super().flush(timeout)

    worker = ReleasedByFlush(5)
    monkeypatch.setattr(reporting, "_reports", worker)
    await reporting.aflush_reports()  # No thread yet: nothing to wait for.
    assert worker._worker is None
    assert not resume.is_set()

    entered = threading.Event()

    def hold() -> None:
        entered.set()
        resume.wait(30)

    worker.submit(hold)
    assert entered.wait(10)
    worker.submit(lambda: ran.append("waiting"))
    await close_resources()

    assert ran == ["waiting"]


@pytest.mark.anyio
async def test_flushes_wait_as_long_as_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both flushes hand on their timeout, ``FLUSH_TIMEOUT`` unless told."""
    waited: list[float] = []

    class Recording(_ReportingThread):
        def flush(self, timeout: float) -> bool:
            waited.append(timeout)
            return True

    worker = Recording(1)
    assert worker.submit(lambda: None)  # Started: aflush_reports waits for it.
    monkeypatch.setattr(reporting, "_reports", worker)

    assert flush_reports(1.5)
    await reporting.aflush_reports(2.5)
    await reporting.aflush_reports()

    assert waited == [1.5, 2.5, reporting.FLUSH_TIMEOUT]
