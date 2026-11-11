"""Watch SQLAlchemy's events on every engine and pool, without changing its listeners.

SQLAlchemy reads its listener lists without a lock as each statement runs,
in whichever thread runs it (a ``TestClient`` serves requests in one of its
own). A test that put a listener on the ``Engine`` or ``Pool`` class and took
it off again changed those lists under such threads, and failed their
statements now and then ("deque mutated during iteration"). So one
listener per event is put on the class when this module is imported, before
any test runs, and stays; ``watching`` only adds a callable to the tuple it
reads, and takes it off again::

    with watching(Engine, "before_cursor_execute", record):
        client.get("/products")

It also names the test that leaks a pooled connection. The garbage
collector closes such a connection in whichever test runs then, which fails
(SQLAlchemy warns); each checkout keeps where it was made, and that is
logged when the collector closes one (``_name_the_leak``), and listed at
the end of the run (``leaks``, which ``conftest`` prints): the collector
often runs in a test that passes, whose log pytest drops.

A connection that was given back and never closed, because its engine was
dropped without ``dispose()``, is not such a leak, and fails a later test
all the same: aiosqlite warns that it "was deleted before being closed".
Each connection a pool opens is kept by its address until the pool closes
it (``OPENED``), and a failure that quotes that warning is listed with the
test that opened the connection (``name_the_opener``).
"""

import logging
import os
import re
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import FrameType
from typing import Final

import greenlet  # type: ignore[import-untyped]
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import ConnectionPoolEntry, Pool

import domainkit

type Watcher = Callable[..., object]
type _Key = tuple[type, str]

WATCHED: Final[tuple[_Key, ...]] = (
    (Engine, "before_cursor_execute"),
    (Engine, "begin"),
    (Pool, "checkout"),
    (Pool, "connect"),
    (Pool, "invalidate"),
)
"""The events a test may watch: each has its one lasting listener."""
_lock: Final = threading.Lock()
"""Guards replacing ``_watchers``' tuples."""
_watchers: dict[_Key, tuple[Watcher, ...]] = dict.fromkeys(WATCHED, ())
"""Each event's watchers. A tuple is replaced whole, never changed in place,
so the listeners read it without the lock."""


def _dispatcher(key: _Key) -> Callable[..., None]:
    """The lasting listener for ``key``: it calls each of the event's watchers."""

    def dispatch(*args: object) -> None:
        for watcher in _watchers[key]:
            watcher(*args)

    return dispatch


for _target, _name in WATCHED:
    event.listen(_target, _name, _dispatcher((_target, _name)))


@contextmanager
def watching(target: type, name: str, watcher: Watcher) -> Iterator[None]:
    """Call ``watcher`` with the event's arguments while the block runs."""
    key = (target, name)
    if key not in _watchers:
        raise ValueError(f"{target.__name__}.{name} is not watched; add it to WATCHED.")
    with _lock:
        _watchers[key] = (*_watchers[key], watcher)
    try:
        yield
    finally:
        with _lock:
            _watchers[key] = tuple(
                each for each in _watchers[key] if each is not watcher
            )


CHECKED_OUT_AT: Final = "domainkit_tests.checked_out_at"
"""The key of a pooled connection's ``info`` that says where it was taken."""
_STACK_DEPTH: Final = 40
"""How many frames a checkout keeps: enough to reach the test's own."""
_OURS: Final = (
    str(Path(domainkit.__file__).parent) + os.sep,
    str(Path(__file__).parent) + os.sep,
)
"""The frames worth keeping: domainkit's and its tests', not the libraries'."""
leak_log: Final = logging.getLogger("domainkit_tests.leaked_connections")
leaks: Final[list[str]] = []
"""Each leak this process saw, as it was logged: the run's summary lists them."""


OPENED: Final[dict[int, str]] = {}
"""Where each driver connection a pool opened and has not closed was opened,
by the driver connection's address (what its warning prints)."""
_UNCLOSED: Final = re.compile(r"aiosqlite\.core\.Connection object at (0x[0-9a-f]+)")
"""The connection aiosqlite's "was deleted before being closed" names."""


def _here() -> tuple[str, list[str]]:
    """The test that is running, and the frames of domainkit and its tests."""
    return (
        os.environ.get("PYTEST_CURRENT_TEST", "?"),
        [
            f"{frame.f_code.co_filename}:{frame.f_lineno} in {frame.f_code.co_name}"
            for frame in _callers()
            if frame.f_code.co_filename.startswith(_OURS)
            and frame.f_code.co_filename != __file__  # Not the listeners' own.
        ],
    )


def _remember_checkout(
    _dbapi: object, record: ConnectionPoolEntry, _proxy: object
) -> None:
    """Keep where each connection was taken from its pool, cheaply.

    A connection that is never given back is closed by the garbage
    collector, in whichever test is running then: SQLAlchemy warns, and that
    test fails, though another one leaked it. The frames kept here name the
    one that did (``_name_the_leak``).
    """
    record.info[CHECKED_OUT_AT] = _here()


def _driver(dbapi_connection: object) -> object:
    """The driver's own connection: SQLAlchemy wraps an async driver's."""
    return getattr(dbapi_connection, "_connection", dbapi_connection)


def _remember_open(dbapi_connection: object, _record: object) -> None:
    """Keep where a pool opened a connection, until the pool closes it."""
    test, frames = _here()
    where = "\n  ".join(frames) or "(no frame of domainkit or its tests)"
    OPENED[id(_driver(dbapi_connection))] = f"{test}\n  {where}"


def _forget_closed(dbapi_connection: object, _record: object = None) -> None:
    """A connection its pool closed is no one's to name."""
    OPENED.pop(id(_driver(dbapi_connection)), None)


def name_the_opener(failure: str, test: str) -> None:
    """List where the connection a failure says was never closed was opened.

    ``failure`` is a failed test's report. aiosqlite's warning in it gives
    the connection's address, which ``OPENED`` still holds when no pool
    closed it: its engine was dropped without ``dispose()``. An address is
    reused once its object is gone, so a connection opened since at the
    same one would be named in its place: the test listed is the likely
    one, and the frames say where to look.
    """
    for address in dict.fromkeys(_UNCLOSED.findall(failure)):
        where = OPENED.pop(int(address, 16), None)
        if where is not None:
            leaks.append(
                f"opened during {where}\n"
                f"never closed; the collector deleted it during {test}"
            )


def _callers() -> list[FrameType]:
    """The frames that led to this call, the innermost first.

    An async engine checks out in a greenlet of its own, whose stack ends at
    SQLAlchemy's bridge. The greenlet that started it waits in the caller's
    frames: they follow.
    """
    frames: list[FrameType] = []
    frame: FrameType | None = sys._getframe(2)
    parent = greenlet.getcurrent().parent
    while len(frames) < _STACK_DEPTH:
        if frame is None:
            if parent is None:
                break
            frame, parent = parent.gr_frame, parent.parent
            continue
        frames.append(frame)
        frame = frame.f_back
    return frames


def _name_the_leak(dbapi_connection: object) -> None:
    """Log where a connection the garbage collector closes was taken.

    SQLAlchemy closes a detached connection with this event both when it is
    told to and when the garbage collector finds one that was never given
    back. Only the second runs inside ``_finalize_fairy`` with
    ``is_gc_cleanup`` set, and its warning follows.
    """
    frame: FrameType | None = sys._getframe(1)
    while frame is not None and frame.f_code.co_name != "_finalize_fairy":
        frame = frame.f_back
    if frame is None or not frame.f_locals.get("is_gc_cleanup"):
        return
    record = frame.f_locals.get("connection_record")
    test, frames = (
        record.info.get(CHECKED_OUT_AT, ("?", [])) if record is not None else ("?", [])
    )
    where = "\n  ".join(frames) or "(no frame of domainkit or its tests)"
    leaks.append(
        f"taken during {test}\n  {where}\n"
        f"closed during {os.environ.get('PYTEST_CURRENT_TEST', '?')}"
    )
    leak_log.error(
        "The connection %r was never given back; it was taken during %s, at:\n  %s",
        dbapi_connection,
        test,
        where,
    )


event.listen(Pool, "checkout", _remember_checkout)
event.listen(Pool, "close_detached", _name_the_leak)
event.listen(Pool, "connect", _remember_open)
event.listen(Pool, "close", _forget_closed)
event.listen(Pool, "close_detached", _forget_closed)
