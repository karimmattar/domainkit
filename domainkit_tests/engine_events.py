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
"""

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Final

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import Pool

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
