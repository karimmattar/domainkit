"""Structured logging: JSON for log pipelines, escaped text for terminals.

Every record gets ``request_id`` and ``client_ip`` from the request context, so
the application, access, and security lines of one request can be joined.
Query strings and request bodies are never logged.

``build_logging_config`` writes through a ``BackgroundHandler``: the caller,
the event loop included, only puts the record on a bounded queue, and one
thread writes it out. A full queue drops records and counts them instead of
waiting, and the count is logged once there is room again. Shutdown waits
for the queue to drain, within ``FLUSH_TIMEOUT``.
"""

import asyncio
import copy
import json
import logging
import logging.config
import logging.handlers
import os
import queue
import threading
import weakref
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
LOG_LOGGER: Final = "domainkit.log"
"""Where the logging itself reports records it had to drop."""

TEXT_FORMAT: Final = "%(asctime)s %(levelname)s [%(name)s] [%(request_id)s] %(message)s"
_STRUCTURED_FIELDS: Final = ("access",)
_CONTEXT_FIELDS: Final = ("request_id", "client_ip", "trace_id", "span_id")

DEFAULT_QUEUE_SIZE: Final = 10_000
"""Records that may wait for the logging thread; more are dropped and counted."""
FLUSH_TIMEOUT: Final = 5.0
"""Seconds shutdown waits for queued records to be written."""


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
        for field in _CONTEXT_FIELDS:
            value = record.__dict__.get(field)
            if value and value != "-":
                entry[field] = value
        for field in _STRUCTURED_FIELDS:
            if field in record.__dict__:
                entry[field] = record.__dict__[field]
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            entry["exception"] = record.exc_text
        if record.stack_info:
            entry["stack"] = self.formatStack(record.stack_info)
        return json.dumps(entry, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """Human-readable lines; CR/LF in messages are escaped to stop log forging."""

    def formatMessage(self, record: logging.LogRecord) -> str:
        """Escape line breaks that untrusted input (such as paths) may contain."""
        line = super().formatMessage(record)
        return line.replace("\r", "\\r").replace("\n", "\\n")


class _Flush:
    """A marker on the queue: the thread sets ``done`` when it reaches it."""

    def __init__(self) -> None:
        """Start unreached."""
        self.done = threading.Event()


_STOP: Final = object()
"""The last item the thread reads off the queue."""


class BackgroundListener(logging.handlers.QueueListener):
    """The logging thread: takes records off the queue and writes them.

    Handler levels are respected, and a handler that fails is reported
    through its ``handleError`` without stopping the thread.
    """

    def __init__(
        self,
        records: queue.Queue[Any],
        *handlers: logging.Handler,
        respect_handler_level: bool = True,
    ) -> None:
        """Write what ``records`` holds through ``handlers``."""
        super().__init__(
            records, *handlers, respect_handler_level=respect_handler_level
        )
        self.queue: queue.Queue[Any] = records
        self._worker: threading.Thread | None = None
        self._starting = threading.Lock()

    @property
    def running(self) -> bool:
        """Whether the thread has been started and not stopped."""
        return self._worker is not None

    def start(self) -> None:
        """Start the thread, unless it is running."""
        with self._starting:
            if self._worker is None:
                self._worker = threading.Thread(
                    target=self._write_queued, name="domainkit-logging", daemon=True
                )
                self._worker.start()

    def handle(self, record: Any) -> None:  # noqa: ANN401 - records and markers.
        """Write ``record`` through every handler whose level it meets."""
        if isinstance(record, _Flush):
            record.done.set()
            return
        for handler in self.handlers:
            if self.respect_handler_level and record.levelno < handler.level:
                continue
            try:
                handler.handle(record)
            except Exception:  # noqa: BLE001 - one handler must not stop the thread.
                handler.handleError(record)

    def flush(self, timeout: float = FLUSH_TIMEOUT) -> bool:
        """Wait until what was queued before now is written; False after ``timeout``.

        The wait for room on a full queue and the wait for the thread to
        reach the marker each take at most ``timeout``.
        """
        if not self.running:
            return self.queue.empty()
        marker = _Flush()
        try:
            self.queue.put(marker, timeout=timeout)
        except queue.Full:
            return False
        return marker.done.wait(timeout)

    def stop(self, timeout: float = FLUSH_TIMEOUT) -> bool:  # type: ignore[override]
        """Write what is queued, then end the thread; False if it is still busy.

        A thread that does not end in time, behind a handler that blocks, is
        left to end on its own: it is a daemon, and nothing waits for it.
        """
        with self._starting:
            worker, self._worker = self._worker, None
        if worker is None:
            return True
        try:
            self.queue.put(_STOP, timeout=timeout)
        except queue.Full:
            return False
        worker.join(timeout)
        return not worker.is_alive()

    def restart_on(self, records: queue.Queue[Any]) -> None:
        """Read ``records`` from now on, with no thread until the next record.

        For a forked child, which has no thread, and whose copy of the old
        queue may be locked by a thread of the parent.
        """
        self.queue = records
        self._worker = None
        self._starting = threading.Lock()

    def _write_queued(self) -> None:
        """The thread: write what the queue holds until it reads ``_STOP``."""
        records = self.queue
        while (record := records.get()) is not _STOP:
            self.handle(record)


class BackgroundHandler(logging.handlers.QueueHandler):
    """Put records on a bounded queue for ``BackgroundListener`` to write.

    The caller never waits for a stream, a file, or a socket. Records are
    formatted on the logging thread, from a copy with the message merged, so
    later changes to the arguments do not show. Filters run on the caller,
    where the request context is. In a ``LOGGING`` mapping::

        "background": {
            "class": "domainkit.log.BackgroundHandler",
            "handlers": ["console"],
            "queue": {"()": "queue.Queue", "maxsize": 10000},
            "listener": "domainkit.log.BackgroundListener",
            "filters": ["context"],
        }

    A full queue drops the record and counts it (``dropped``); the next
    record that fits is preceded by a warning with the count. Once closed
    (at exit, or when another ``dictConfig`` replaces it), records are
    written on the caller's thread, as a plain handler would.
    """

    def __init__(self, records: queue.Queue[Any]) -> None:
        """Queue records on ``records``, which must be bounded.

        Raises:
            ImproperlyConfigured: ``records`` has no ``maxsize``.
        """
        if getattr(records, "maxsize", 0) <= 0:
            raise ImproperlyConfigured(
                "BackgroundHandler needs a bounded queue, such as "
                '{"()": "queue.Queue", "maxsize": 10000}.'
            )
        self._listener: BackgroundListener | None = None
        super().__init__(records)
        self.queue: queue.Queue[Any] = records
        self.dropped = 0
        """Records dropped on a full queue since the handler was made."""
        self._unreported = 0
        self._counting = threading.Lock()
        self._closed = False
        _remember(self)

    @property
    def listener(self) -> BackgroundListener | None:
        """The thread's listener; ``dictConfig`` sets it from ``handlers``."""
        return self._listener

    @listener.setter
    def listener(self, listener: logging.handlers.QueueListener | None) -> None:
        """Take ``listener``, made a ``BackgroundListener`` if it is not one."""
        if listener is not None and not isinstance(listener, BackgroundListener):
            listener = BackgroundListener(
                self.queue,
                *listener.handlers,
                respect_handler_level=listener.respect_handler_level,
            )
        self._listener = listener

    @classmethod
    def writing_to(
        cls, *handlers: logging.Handler, maxsize: int = DEFAULT_QUEUE_SIZE
    ) -> "BackgroundHandler":
        """A handler that writes through ``handlers`` from a queue of ``maxsize``."""
        records: queue.Queue[Any] = queue.Queue(maxsize)
        handler = cls(records)
        handler.listener = BackgroundListener(records, *handlers)
        return handler

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        """A copy with the message merged; the exception stays to format later."""
        prepared = copy.copy(record)
        prepared.message = record.getMessage()
        prepared.msg, prepared.args = prepared.message, None
        return prepared

    def enqueue(self, record: logging.LogRecord) -> None:
        """Queue ``record`` without waiting; on a full queue, drop and count it."""
        listener = self._listener
        if listener is None or self._closed:
            self._write_here(record)
            return
        if not listener.running:
            listener.start()
        unreported = self._take_unreported()
        if unreported and not self._offer(self._drop_notice(unreported)):
            self._count_dropped(1, unreported=unreported)
            return
        if not self._offer(record):
            self._count_dropped(1)

    def flush(self) -> None:
        """Wait, at most ``FLUSH_TIMEOUT``, for queued records to be written."""
        self.wait_written(FLUSH_TIMEOUT)

    def wait_written(self, timeout: float) -> bool:
        """Report drops, then wait until queued records are written.

        Returns False when they were not written within ``timeout``.
        """
        listener = self._listener
        if listener is None:
            return True
        unreported = self._take_unreported()
        if unreported:
            try:
                self.queue.put(self._drop_notice(unreported), timeout=timeout)
            except queue.Full:
                self._count_dropped(0, unreported=unreported)
        return listener.flush(timeout)

    def close(self) -> None:
        """Write what is queued (within ``FLUSH_TIMEOUT``) and end the thread.

        Later records are written on the caller's thread.
        """
        if not self._closed:
            self.wait_written(FLUSH_TIMEOUT)
            self._closed = True
            if self._listener is not None:
                self._listener.stop(FLUSH_TIMEOUT)
        super().close()

    def _offer(self, item: logging.LogRecord) -> bool:
        """Put ``item`` on the queue if there is room."""
        try:
            self.queue.put_nowait(item)
        except queue.Full:
            return False
        return True

    def _count_dropped(self, records: int, *, unreported: int = 0) -> None:
        """Count ``records`` dropped now, plus a count not reported yet."""
        with self._counting:
            self.dropped += records
            self._unreported += records + unreported

    def _take_unreported(self) -> int:
        """The drops not reported yet, which are now this caller's to report."""
        if not self._unreported:
            return 0
        with self._counting:
            unreported, self._unreported = self._unreported, 0
        return unreported

    def _drop_notice(self, count: int) -> logging.LogRecord:
        """The warning that says ``count`` records were dropped."""
        record = logging.LogRecord(
            LOG_LOGGER,
            logging.WARNING,
            __file__,
            0,
            "Dropped %d log records: the logging queue was full",
            (count,),
            None,
        )
        record.__dict__.update(dict.fromkeys(_CONTEXT_FIELDS, "-"))
        return self.prepare(record)

    def _write_here(self, record: logging.LogRecord) -> None:
        """Write ``record`` on this thread (no listener, or closed)."""
        if self._listener is not None:
            self._listener.handle(record)

    def _after_fork(self) -> None:
        """Start over in a forked child: a new queue, and no thread yet.

        The parent's queue and locks may have been held by its threads at
        the fork; its queued records are the parent's to write.
        """
        self.queue = queue.Queue(self.queue.maxsize)
        self._counting = threading.Lock()
        self._unreported = 0
        if self._listener is not None:
            self._listener.restart_on(self.queue)


_handlers: "weakref.WeakSet[BackgroundHandler]" = weakref.WeakSet()
_handlers_lock = threading.Lock()


def _remember(handler: BackgroundHandler) -> None:
    """Track ``handler`` for ``flush_logs`` and forks."""
    with _handlers_lock:
        _handlers.add(handler)


def _live_handlers() -> list[BackgroundHandler]:
    """Every background handler still in use."""
    with _handlers_lock:
        return list(_handlers)


def _after_fork_in_child() -> None:
    """Give every background handler a fresh queue in a forked child."""
    for handler in _live_handlers():
        handler._after_fork()


if hasattr(os, "register_at_fork"):  # pragma: no branch - POSIX has it.
    os.register_at_fork(after_in_child=_after_fork_in_child)


def flush_logs(timeout: float = FLUSH_TIMEOUT) -> bool:
    """Wait until every background handler has written what it queued.

    Each handler gets ``timeout`` seconds; returns False if one did not
    finish in time (a stuck stream).
    """
    written = [handler.wait_written(timeout) for handler in _live_handlers()]
    return all(written)


async def aflush_logs(seconds: float = FLUSH_TIMEOUT) -> None:
    """``flush_logs`` on a worker thread, for the lifespan's shutdown."""
    if _live_handlers():
        await asyncio.to_thread(flush_logs, seconds)


def build_logging_config(
    *, log_format: str, level: str, queue_size: int | None = DEFAULT_QUEUE_SIZE
) -> dict[str, Any]:
    """Return a ``LOGGING`` mapping for ``text`` or ``json`` output on stdout.

    Records reach stdout from a thread, through a queue of ``queue_size``
    records (``BackgroundHandler``); with ``queue_size=None`` each caller
    writes its own, waiting for stdout as it does.
    """
    if log_format not in {"text", "json"}:
        raise ImproperlyConfigured(
            f"LOG_FORMAT must be 'text' or 'json', got {log_format!r}."
        )
    level = level.upper()
    if level not in logging.getLevelNamesMapping():
        raise ImproperlyConfigured(f"LOG_LEVEL {level!r} is not a logging level.")
    if queue_size is not None and queue_size < 1:
        raise ImproperlyConfigured(
            f"The logging queue_size must be 1 or more, or None; got {queue_size}."
        )
    console: dict[str, Any] = {
        "class": "logging.StreamHandler",
        "stream": "ext://sys.stdout",
        "formatter": log_format,
    }
    handlers: dict[str, Any] = {"console": console}
    if queue_size is None:
        console["filters"] = ["context"]
        root = "console"
    else:
        handlers["background"] = {
            "class": "domainkit.log.BackgroundHandler",
            "handlers": ["console"],
            "queue": {"()": "queue.Queue", "maxsize": queue_size},
            "listener": "domainkit.log.BackgroundListener",
            "respect_handler_level": True,
            # The request context exists on the caller's side only.
            "filters": ["context"],
        }
        root = "background"
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {"context": {"()": ContextFilter}},
        "formatters": {
            "text": {"()": TextFormatter, "fmt": TEXT_FORMAT},
            "json": {"()": JsonFormatter},
        },
        "handlers": handlers,
        "root": {"handlers": [root], "level": level},
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
