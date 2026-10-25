"""Structured logging: JSON for log pipelines, escaped text for terminals.

Every record gets ``request_id`` and ``client_ip`` from the request context, so
the application, access, and security lines of one request can be joined.
Query strings and request bodies are never logged.

``build_logging_config`` writes through a ``BackgroundHandler``: the caller,
the event loop included, only puts the record on a bounded queue, and one
thread writes it out. A full queue drops records and counts them instead of
waiting, and the count is logged once there is room again. The last tenth
of the queue is kept for warnings and errors (``WARNING_RESERVE``), so a
flood of lower records cannot crowd out a security event. Shutdown waits
for the queue to drain, within ``FLUSH_TIMEOUT``.

``JsonFormatter`` writes the line ``json.dumps`` wrote, byte for byte, in
40% less time. ``AccessSampler`` decides which access lines to write when
``ACCESS_LOG_SAMPLE_RATE`` is below 1: every response from 400 up, every
request without one, every slow request, and a fair share of the rest.
"""

import asyncio
import copy
import json
import logging
import logging.config
import logging.handlers
import math
import os
import queue
import random
import threading
import time
import weakref
from collections import Counter, OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from json.encoder import encode_basestring
from typing import Any, Final

from domainkit import clock
from domainkit.context import current_request
from domainkit.exceptions import ImproperlyConfigured
from domainkit.tracing import current_ids

__all__ = [
    "WARNING_RESERVE",
    "BackgroundHandler",
    "BackgroundListener",
    "ContextFilter",
    "JsonFormatter",
    "TextFormatter",
    "ThrottledLog",
    "aflush_logs",
    "aflush_throttled",
    "build_logging_config",
    "configure_logging",
    "flush_logs",
    "flush_throttled",
]

SECURITY_LOGGER: Final = "domainkit.security"
"""Rejected requests and other security events (``django.security``)."""
ACCESS_LOGGER: Final = "domainkit.access"
"""One line per HTTP request; replaces uvicorn's access log."""
LOG_LOGGER: Final = "domainkit.log"
"""Where the logging itself reports records it had to drop."""

TEXT_FORMAT: Final = "%(asctime)s %(levelname)s [%(name)s] [%(request_id)s] %(message)s"
_STRUCTURED_FIELDS: Final = ("access",)
_CONTEXT_FIELDS: Final = ("request_id", "client_ip", "trace_id", "span_id")
_KEYS: Final = {
    name: f', "{name}": ' for name in (*_CONTEXT_FIELDS, *_STRUCTURED_FIELDS)
}
"""Each optional field's key, as ``json.dumps`` writes it after the field before."""
_ENCODER: Final = json.JSONEncoder(ensure_ascii=False, default=str)
"""The encoder ``json.dumps(value, default=str, ensure_ascii=False)`` makes."""

DEFAULT_QUEUE_SIZE: Final = 10_000
"""Records that may wait for the logging thread; more are dropped and counted."""
FLUSH_TIMEOUT: Final = 5.0
"""Seconds shutdown waits for queued records to be written."""
WARNING_RESERVE: Final = 0.1
"""Part of a logging queue only records at ``WARNING`` and above may fill.

Records below it may fill the rest: 9,000 of the default 10,000 places.
A queue of fewer than 10 places keeps none."""


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


ALWAYS_LOGGED_FROM: Final = 400
"""The first status whose access line sampling never leaves out.

Client errors are refusals as much as 401, 403, and 429 are: a scoped
resource answers 404 for what the caller may not reach, so probing other
tenants' ids is a run of 404s. They are few in a healthy service, and
they are what an investigation reads.
"""


class AccessSampler:
    """Which access lines to write: all that matter, and a fair share of the rest.

    With a ``rate`` under 1, a line is still written for every response
    with a status of 400 or more (client and server errors, refusals of
    credentials, access, or rate, and the 404 of what is out of the
    caller's reach), for every request that got no response at all, and
    for every request that took ``slow`` seconds or more. Each other
    request, a success or a redirect, is written with probability
    ``rate``, drawn on its own: a fair sample whatever the traffic's
    pattern, which callers cannot steer. Security events have a logger of
    their own (``domainkit.security``) that sampling never touches, and the
    audit trail is not a log at all.
    """

    def __init__(
        self,
        rate: float = 1.0,
        slow: float = 1.0,
        *,
        draw: Callable[[], float] = random.random,
    ) -> None:
        """Write a ``rate`` (0 to 1) of lines; all of ``slow`` seconds or more.

        ``draw`` returns a number from 0 up to 1, uniformly.

        Raises:
            ValueError: ``rate`` is not from 0 to 1, or ``slow`` not positive.
        """
        if not 0.0 <= rate <= 1.0:
            raise ValueError(f"The sample rate must be from 0 to 1, not {rate!r}.")
        if not slow > 0.0:
            raise ValueError(f"The slow duration must be positive, not {slow!r}.")
        self.rate = rate
        self.slow = slow
        self._draw = draw

    def keep(self, status: int | None, seconds: float) -> bool:
        """Whether to write the line of a request answered ``status`` in ``seconds``.

        ``None`` is a request that got no response: an error.
        """
        if self.rate >= 1.0:
            return True
        if status is None or status >= ALWAYS_LOGGED_FROM or seconds >= self.slow:
            return True
        return self._draw() < self.rate


THROTTLE_INTERVAL: Final = 10.0
"""Seconds ``ThrottledLog`` holds back repeats of one event from one key."""
THROTTLE_KEYS: Final = 1024
"""Events and keys a ``ThrottledLog`` remembers; the oldest go first."""


@dataclass(slots=True)
class _Throttled:
    """One event from one key: when its repeats may be written again."""

    until: float
    held: int = 0
    """Repeats held back since its last line."""


class ThrottledLog:
    """Warnings written once per event and key each ``interval``, with a count.

    A flood of refused requests (bad ``Host`` headers, oversized bodies,
    bad credentials) would otherwise write a line each, fill the logging
    queue, and crowd out lines that matter. The first line of an event
    from a key (a caller's address, or its user) is always written; its
    repeats within ``interval`` seconds are counted. The count is written
    with the key's next line or, if none comes, by the next warning for
    any key once the key's ``interval`` has passed, or when the process
    shuts down (``flush_throttled``): a burst that stops still shows its
    size. When more than ``keys`` are remembered the oldest is let go,
    with a line for its held-back repeats if it has any. Keys come from
    callers, so they are bounded: an address, never a header's text. Event
    loops on several threads share one (free-threaded Python), so its
    state changes only under its lock.
    """

    def __init__(
        self,
        logger: logging.Logger,
        *,
        interval: float = THROTTLE_INTERVAL,
        keys: int = THROTTLE_KEYS,
    ) -> None:
        """Write to ``logger``; hold repeats back for ``interval`` seconds."""
        self.logger = logger
        self.interval = interval
        self.keys = keys
        self._seen: OrderedDict[tuple[str, str], _Throttled] = OrderedDict()
        """Events and keys by the time of their last line, the oldest first."""
        self._lock = threading.Lock()
        with _handlers_lock:
            _throttled_logs.add(self)

    def warning(self, event: str, key: str, message: str, *args: object) -> None:
        """Warn ``message % args`` unless ``event`` from ``key`` was just written.

        ``event`` names the kind of line (``invalid_host``), ``key`` whom
        it is about. Nothing is formatted for a line that is held back.
        Every call also writes the counts of other keys whose ``interval``
        has passed, and forgets those keys.
        """
        if not self.logger.isEnabledFor(logging.WARNING):
            return
        with self._lock:
            now = clock.monotonic()
            seen = self._seen.get((event, key))
            held: int | None = None
            if seen is not None and now < seen.until:
                seen.held += 1
            else:
                held = 0 if seen is None else seen.held
                self._seen[(event, key)] = _Throttled(now + self.interval)
                self._seen.move_to_end((event, key))
            gone = self._expired(now)
            if len(self._seen) > self.keys:
                oldest, evicted = self._seen.popitem(last=False)
                gone.append((oldest, evicted.held))
        if held:
            self.logger.warning(
                "%s (and %d more like it since the last line)",
                message % args if args else message,
                held,
            )
        elif held == 0:
            self.logger.warning(message, *args)
        self._write_held(gone)

    def flush(self) -> None:
        """Write every count held back, and forget every key."""
        with self._lock:
            gone = [(seen, kept.held) for seen, kept in self._seen.items()]
            self._seen.clear()
        self._write_held(gone)

    def _expired(self, now: float) -> list[tuple[tuple[str, str], int]]:
        """Forget the keys whose interval has passed; each with what it held back.

        Keys are kept in the order of their last line, and every line holds
        its key back for the same ``interval``, so the expired ones are at
        the front. Called under the lock.
        """
        gone: list[tuple[tuple[str, str], int]] = []
        while self._seen:
            first = next(iter(self._seen))
            if self._seen[first].until > now:
                break
            gone.append((first, self._seen.pop(first).held))
        return gone

    def _write_held(self, gone: list[tuple[tuple[str, str], int]]) -> None:
        """A line for each forgotten key that held repeats back."""
        for (event, key), count in gone:
            if count:
                self.logger.warning(
                    "%d more %s line(s) for %s were held back", count, event, key
                )


class JsonFormatter(logging.Formatter):
    """Render one JSON object per line; control characters are always escaped.

    The fields, in order: ``timestamp`` (UTC, milliseconds), ``level``,
    ``logger``, ``message``; the request and trace ids and the client
    address when known; ``access`` on access lines; ``exception`` and
    ``stack`` when the record has them. The line is the one
    ``json.dumps(entry, default=str, ensure_ascii=False)`` writes, byte for
    byte: text goes through the encoder ``json.dumps`` uses for text, other
    values through ``json.dumps``'s own, and the timestamp's date and time
    are worked out once a second.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401 - Formatter's.
        """Take ``logging.Formatter``'s arguments; the format is fixed."""
        super().__init__(*args, **kwargs)
        self._second: tuple[int, str] = (0, utc_second(0))
        """The last second formatted, and its date and time: one tuple, so
        threads formatting at once see a pair that belongs together."""

    def format(self, record: logging.LogRecord) -> str:
        """Serialize the record with UTC timestamps and structured extras."""
        fields = record.__dict__
        parts = [
            '{"timestamp": "',
            self.timestamp(record.created),
            '", "level": ',
            _json_value(record.levelname),
            ', "logger": ',
            _json_value(record.name),
            ', "message": ',
            encode_basestring(record.getMessage()),
        ]
        for field in _CONTEXT_FIELDS:
            value = fields.get(field)
            if value and value != "-":
                parts += (_KEYS[field], _json_value(value))
        for field in _STRUCTURED_FIELDS:
            if field in fields:
                parts += (_KEYS[field], _json_value(fields[field]))
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            parts += (', "exception": ', encode_basestring(record.exc_text))
        if record.stack_info:
            stack = self.formatStack(record.stack_info)
            parts += (', "stack": ', encode_basestring(stack))
        parts.append("}")
        return "".join(parts)

    def timestamp(self, created: float) -> str:
        """``created`` as ``datetime.fromtimestamp(created, UTC)`` writes it to the ms.

        Rounded to the microsecond as ``datetime`` rounds (half to even,
        carrying into the next second), then cut to milliseconds as
        ``isoformat(timespec="milliseconds")`` cuts.
        """
        fraction, whole = math.modf(created)
        micro = round(fraction * 1e6)
        second = int(whole)
        if micro >= MICROSECONDS:
            second, micro = second + 1, micro - MICROSECONDS
        elif micro < 0:
            second, micro = second - 1, micro + MICROSECONDS
        cached, text = self._second
        if cached != second:
            text = utc_second(second)
            self._second = (second, text)
        return f"{text}.{micro // 1000:03d}+00:00"


MICROSECONDS: Final = 1_000_000
"""Microseconds in a second."""


def utc_second(second: int) -> str:
    """The UTC date and time of ``second`` since the epoch: ``2026-10-21T09:30:15``."""
    return datetime.fromtimestamp(second, UTC).isoformat()[:19]


def _json_value(value: object) -> str:
    """``value`` as ``json.dumps(value, default=str, ensure_ascii=False)`` writes it."""
    if type(value) is str:
        return encode_basestring(value)
    return _ENCODER.encode(value)


class TextFormatter(logging.Formatter):
    """Human-readable lines; CR/LF in messages are escaped to stop log forging."""

    def formatMessage(self, record: logging.LogRecord) -> str:
        """Escape line breaks that untrusted input (such as paths) may contain."""
        line = super().formatMessage(record)
        return line.replace("\r", "\\r").replace("\n", "\\n")


def _seconds() -> float:
    """Real seconds, for the deadlines of waits on the logging thread.

    Not ``domainkit.clock``: these bound real waits on another thread, which
    a frozen clock would not move.
    """
    return time.monotonic()


def _left(deadline: float) -> float:
    """Seconds until ``deadline`` (a ``_seconds`` reading); none once past."""
    return max(0.0, deadline - _seconds())


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
        reach the marker share the ``timeout``.
        """
        if not self.running:
            return self.queue.empty()
        deadline = _seconds() + timeout
        marker = _Flush()
        try:
            self.queue.put(marker, timeout=timeout)
        except queue.Full:
            return False
        return marker.done.wait(_left(deadline))

    def stop(self, timeout: float = FLUSH_TIMEOUT) -> bool:  # type: ignore[override]
        """Write what is queued, then end the thread; False if it is still busy.

        A thread that does not end in time, behind a handler that blocks, is
        left to end on its own: it is a daemon, and nothing waits for it.
        """
        with self._starting:
            worker, self._worker = self._worker, None
        if worker is None:
            return True
        deadline = _seconds() + timeout
        try:
            self.queue.put(_STOP, timeout=timeout)
        except queue.Full:
            return False
        worker.join(_left(deadline))
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

    A full queue drops the record and counts it (``dropped``), by level;
    the next record that fits with it is preceded by a warning with the
    counts. Records below ``WARNING`` may only fill the queue up to
    ``WARNING_RESERVE`` from the top, so warnings and errors find room
    when lower records are being dropped. Once closed (at exit, or when
    another ``dictConfig`` replaces it), records are written on the
    caller's thread, as a plain handler would.
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
        self._below_warning = records.maxsize - int(records.maxsize * WARNING_RESERVE)
        """Places records below ``WARNING`` may fill."""
        self._unreported: Counter[int] = Counter()
        """Records dropped and not yet reported, by level."""
        self._counting = threading.Lock()
        # Not ``_closed``: ``logging.Handler`` has its own, which it sets.
        self._stopped = False
        self._flushed_by: float | None = None
        """The last ``flush()``'s deadline, which a ``close()`` before it shares."""
        self._gave_up = False
        """Whether the last ``flush()`` gave up on a stuck stream."""
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
        """A copy with the message merged; the exception stays to format later.

        A plain ``LogRecord`` is copied field by field, as ``copy.copy``
        copies it, without its generic machinery; other kinds of record by
        ``copy.copy``, which honours their own ``__copy__``.
        """
        if type(record) is logging.LogRecord:
            prepared = object.__new__(logging.LogRecord)
            prepared.__dict__.update(record.__dict__)
        else:
            prepared = copy.copy(record)
        prepared.message = record.getMessage()
        prepared.msg, prepared.args = prepared.message, None
        return prepared

    def enqueue(self, record: logging.LogRecord) -> None:
        """Queue ``record`` without waiting; on a full queue, drop and count it.

        Drops not yet reported go ahead of it in a notice, if both fit;
        otherwise a later record says them.
        """
        listener = self._listener
        if listener is None or self._stopped:
            self._write_here(record)
            return
        if not listener.running:
            listener.start()
        level = record.levelno
        unreported = self._take_unreported()
        if unreported is not None and not (
            self._room(level, 2) and self._offer(self._drop_notice(unreported))
        ):
            self._count_dropped(unreported=unreported)
        if not self._offer(record):
            self._count_dropped(level)

    def flush(self) -> None:
        """Wait, at most ``FLUSH_TIMEOUT``, for queued records to be written.

        At exit, ``logging.shutdown`` calls it and then ``close()``: they
        share the one ``FLUSH_TIMEOUT``, and ``close()`` waits no more once
        this gave up on a stuck stream.
        """
        self._flushed_by = _seconds() + FLUSH_TIMEOUT
        self._gave_up = not self.wait_written(FLUSH_TIMEOUT)

    def wait_written(self, timeout: float) -> bool:
        """Report drops, then wait until queued records are written.

        Returns False when they were not written within ``timeout``, which
        the waits for room and for the thread share.
        """
        listener = self._listener
        if listener is None:
            return True
        deadline = _seconds() + timeout
        unreported = self._take_unreported()
        if unreported is not None:
            try:
                self.queue.put(self._drop_notice(unreported), timeout=timeout)
            except queue.Full:
                self._count_dropped(unreported=unreported)
        return listener.flush(_left(deadline))

    def close(self) -> None:
        """Write what is queued (within ``FLUSH_TIMEOUT``) and end the thread.

        Right after ``flush()``, only within what is left of its time, and
        without waiting if it gave up: the thread then writes on, if it
        can, while nothing waits for it. Later records are written on the
        caller's thread.
        """
        if not self._stopped:
            deadline = _seconds() + (0.0 if self._gave_up else self._time_left())
            self.wait_written(_left(deadline))
            self._stopped = True
            if self._listener is not None:
                self._listener.stop(_left(deadline))
        super().close()

    def _time_left(self) -> float:
        """Seconds ``close()`` may wait: what ``flush()`` left, or ``FLUSH_TIMEOUT``."""
        flushed_by, now = self._flushed_by, _seconds()
        if flushed_by is not None and now < flushed_by:
            return flushed_by - now
        return FLUSH_TIMEOUT

    def _room(self, level: int, places: int = 1) -> bool:
        """Whether ``places`` records of ``level`` fit on the queue now."""
        top = self.queue.maxsize if level >= logging.WARNING else self._below_warning
        return self.queue.qsize() + places <= top

    def _offer(self, item: logging.LogRecord) -> bool:
        """Put ``item`` on the queue if there is room for its level."""
        if not self._room(item.levelno):
            return False
        try:
            self.queue.put_nowait(item)
        except queue.Full:
            return False
        return True

    def _count_dropped(
        self, level: int | None = None, *, unreported: Counter[int] | None = None
    ) -> None:
        """Count a record of ``level`` dropped now, and put back drops not reported."""
        with self._counting:
            if level is not None:
                self.dropped += 1
                self._unreported[level] += 1
            if unreported is not None:
                self._unreported.update(unreported)

    def _take_unreported(self) -> Counter[int] | None:
        """The drops not reported yet, which are now this caller's to report."""
        if not self._unreported:
            return None
        with self._counting:
            unreported, self._unreported = self._unreported, Counter()
        return unreported or None

    def _drop_notice(self, counts: Counter[int]) -> logging.LogRecord:
        """The warning that says how many records of each level were dropped."""
        levels = ", ".join(
            f"{count} {logging.getLevelName(level)}"
            for level, count in sorted(counts.items())
        )
        record = logging.LogRecord(
            LOG_LOGGER,
            logging.WARNING,
            __file__,
            0,
            "Dropped %d log records (%s): the logging queue was full",
            (counts.total(), levels),
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
        self._unreported = Counter()
        if self._listener is not None:
            self._listener.restart_on(self.queue)


_handlers: "weakref.WeakSet[BackgroundHandler]" = weakref.WeakSet()
_handlers_lock = threading.RLock()
"""Guards ``_handlers`` and ``_throttled_logs``. Reentrant, as ``logging``'s
own lock: a fork from a signal handler that interrupted the thread holding
it takes it again."""
_throttled_logs: "weakref.WeakSet[ThrottledLog]" = weakref.WeakSet()
"""Every ``ThrottledLog`` in use, for ``flush_throttled`` at shutdown."""


def _remember(handler: BackgroundHandler) -> None:
    """Track ``handler`` for ``flush_logs`` and forks."""
    with _handlers_lock:
        _handlers.add(handler)


def _live_handlers() -> list[BackgroundHandler]:
    """Every background handler still in use."""
    with _handlers_lock:
        return list(_handlers)


def _before_fork() -> None:
    """Hold the handlers' lock across a fork, so no thread is changing them then."""
    _handlers_lock.acquire()


def _after_fork_in_parent() -> None:
    """Let the parent's threads at the handlers again."""
    _handlers_lock.release()


def _after_fork_in_child() -> None:
    """Give every background handler a fresh queue in a forked child.

    The child's copy of the handlers' lock is held (``_before_fork`` took
    it, and no thread of the child will let it go), so the child makes a
    new lock before it reads them, as ``logging`` does with its own.
    """
    global _handlers_lock
    _handlers_lock = threading.RLock()
    for handler in _live_handlers():
        handler._after_fork()


if hasattr(os, "register_at_fork"):  # pragma: no branch - POSIX has it.
    os.register_at_fork(
        before=_before_fork,
        after_in_parent=_after_fork_in_parent,
        after_in_child=_after_fork_in_child,
    )


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


def flush_throttled() -> None:
    """Write the counts every live ``ThrottledLog`` still holds back."""
    with _handlers_lock:
        throttled = list(_throttled_logs)
    for log in throttled:
        log.flush()


async def aflush_throttled() -> None:
    """``flush_throttled``, for the shutdown's last phase, before the logs'."""
    flush_throttled()


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
            # httpx2 logs each call's full URL, query string and all, at INFO;
            # domainkit.http.client logs calls without it.
            "httpx2": {"level": "WARNING"},
            "httpcore2": {"level": "WARNING"},
        },
    }


def configure_logging(config: Mapping[str, Any]) -> None:
    """Apply a ``LOGGING`` mapping with ``logging.config.dictConfig``."""
    logging.config.dictConfig(dict(config))
