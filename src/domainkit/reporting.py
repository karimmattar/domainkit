"""Send unexpected errors to error trackers (Sentry, Rollbar, ...).

``ERROR_REPORTERS`` lists callables by dotted path; each receives the
exception and a context mapping (request id, path, message id, ...). For
Sentry::

    def report(exc: BaseException, context: Mapping[str, object]) -> None:
        sentry_sdk.capture_exception(exc)

``report_error`` returns at once: the reporters run on one reporting thread,
so a slow tracker never holds a request or the event loop. They run in a
copy of the caller's context, so a tracker still sees the request (Sentry's
scope) and their logs keep its request id. At most ``REPORT_QUEUE_SIZE``
reports wait; more are dropped and counted, and the count is logged once
there is room again. A reporter that fails is logged and never breaks the
caller. Shutdown waits for waiting reports, within ``FLUSH_TIMEOUT``;
``flush_reports`` waits for them too, as tests do before checking what
was reported.
"""

import asyncio
import atexit
import contextvars
import logging
import os
import queue
import threading
from collections.abc import Callable, Mapping
from functools import cache, partial
from typing import Final

from domainkit.conf import settings
from domainkit.exceptions import ImproperlyConfigured
from domainkit.utils.module_loading import import_string

type Reporter = Callable[[BaseException, Mapping[str, object]], None]

logger = logging.getLogger(__name__)

REPORT_QUEUE_SIZE: Final = 100
"""Reports that may wait for the reporting thread; more are dropped and counted."""
FLUSH_TIMEOUT: Final = 5.0
"""Seconds shutdown waits for waiting reports to be sent."""


class _ReportingThread:
    """One daemon thread that runs reports in order, from a bounded queue."""

    _queue: queue.Queue[Callable[[], object]]
    _worker: threading.Thread | None
    _starting: threading.Lock
    _counting: threading.Lock
    _unreported: int

    def __init__(self, maxsize: int) -> None:
        """Hold at most ``maxsize`` waiting reports; start the thread lazily."""
        self.maxsize = maxsize
        self.dropped = 0
        """Reports dropped on a full queue since the process started."""
        self._reset()

    def submit(self, call: Callable[[], object]) -> bool:
        """Queue ``call``; False, and counted, when the queue is full."""
        self._start()
        try:
            self._queue.put_nowait(call)
        except queue.Full:
            with self._counting:
                self.dropped += 1
                self._unreported += 1
            return False
        if self._unreported:
            with self._counting:
                unreported, self._unreported = self._unreported, 0
            logger.warning(
                "Dropped %d error reports: the reporting queue was full", unreported
            )
        return True

    def flush(self, timeout: float) -> bool:
        """Wait until the reports queued before now ran; False after ``timeout``."""
        if self._worker is None:
            return True
        self._start()
        done = threading.Event()
        try:
            self._queue.put(done.set, timeout=timeout)
        except queue.Full:
            return False
        return done.wait(timeout)

    def _reset(self) -> None:
        """Start empty, with no thread (at first, and in a forked child)."""
        self._queue = queue.Queue(self.maxsize)
        self._worker = None
        self._starting = threading.Lock()
        self._counting = threading.Lock()
        self._unreported = 0

    def _start(self) -> None:
        """Start the thread unless it runs."""
        with self._starting:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(
                    target=self._run, name="domainkit-reporting", daemon=True
                )
                self._worker.start()

    def _run(self) -> None:
        """The thread: run each queued call, for as long as the process lives."""
        calls = self._queue
        while True:
            call = calls.get()
            try:
                call()
            except Exception:
                logger.exception("Error reporting failed")


_reports: Final = _ReportingThread(REPORT_QUEUE_SIZE)

if hasattr(os, "register_at_fork"):  # pragma: no branch - POSIX has it.
    os.register_at_fork(after_in_child=_reports._reset)


@cache
def _load(paths: tuple[str, ...]) -> tuple[Reporter, ...]:
    """Import the reporters named in ``ERROR_REPORTERS``."""
    reporters = []
    for path in paths:
        try:
            reporter = import_string(path)
        except ImportError as exc:
            raise ImproperlyConfigured(f"ERROR_REPORTERS: {exc}") from exc
        if not callable(reporter):
            raise ImproperlyConfigured(f"ERROR_REPORTERS: {path!r} is not callable.")
        reporters.append(reporter)
    return tuple(reporters)


def reporters() -> tuple[Reporter, ...]:
    """Return the configured reporters, failing fast on bad paths."""
    return _load(tuple(settings.ERROR_REPORTERS))


def report_error(exc: BaseException, **context: object) -> None:
    """Hand ``exc`` to every reporter on the reporting thread; never waits."""
    found = reporters()
    if found:
        caller = contextvars.copy_context()
        _reports.submit(partial(caller.run, _report, found, exc, context))


def _report(
    found: tuple[Reporter, ...], exc: BaseException, context: Mapping[str, object]
) -> None:
    """Call each reporter; one that fails is logged, and the rest still run."""
    for reporter in found:
        try:
            reporter(exc, context)
        except Exception:
            logger.exception("Error reporter %r failed", reporter)


def flush_reports(timeout: float = FLUSH_TIMEOUT) -> bool:
    """Wait until the reports made so far were sent; False after ``timeout``."""
    return _reports.flush(timeout)


async def aflush_reports(seconds: float = FLUSH_TIMEOUT) -> None:
    """``flush_reports`` on a worker thread, for the lifespan's shutdown."""
    if _reports._worker is not None:
        await asyncio.to_thread(flush_reports, seconds)


atexit.register(flush_reports)
