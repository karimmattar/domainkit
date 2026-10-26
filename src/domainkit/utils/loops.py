"""Event loops for the framework's own entry points: uvloop when it is there.

Management commands with an ``async`` handle (``runworker`` among them) and
``runserver`` run on uvloop when the ``uvloop`` extra is installed and
``EVENT_LOOP`` is ``"auto"``, or always on asyncio's own loop with
``"asyncio"``. Other entry points share the choice through ``loop_factory``::

    asyncio.run(main(), loop_factory=loop_factory("auto"))

``PerLoop`` keeps one value per event loop (a pool, a set of slots) for
code that serves loops on several threads.
"""

import asyncio
import threading
from collections.abc import Callable, Coroutine
from typing import Any, Literal
from weakref import WeakKeyDictionary

from domainkit.exceptions import ImproperlyConfigured

__all__ = [
    "PerLoop",
    "loop_factory",
]

type LoopKind = Literal["auto", "asyncio", "uvloop"]
"""``auto`` (uvloop when installed), ``asyncio``, or ``uvloop``."""
type LoopFactory = Callable[[], asyncio.AbstractEventLoop]


def loop_factory(kind: LoopKind = "auto") -> LoopFactory | None:
    """The factory of ``kind``'s loops; None stands for asyncio's own.

    ``auto`` takes uvloop when it can be imported; ``uvloop`` requires it.

    Raises:
        ImproperlyConfigured: ``uvloop`` was asked for and is not installed.
    """
    if kind == "asyncio":
        return None
    try:
        import uvloop
    except ImportError:
        if kind == "uvloop":
            raise ImproperlyConfigured(
                "EVENT_LOOP 'uvloop' needs the uvloop extra: "
                "pip install 'domainkit[uvloop]'."
            ) from None
        return None
    factory: LoopFactory = uvloop.new_event_loop
    return factory


def run[T](main: Coroutine[Any, Any, T], *, kind: LoopKind = "auto") -> T:
    """Run ``main`` to completion in a new loop of ``kind`` (``asyncio.run``).

    Raises:
        ImproperlyConfigured: ``uvloop`` was asked for and is not installed;
            ``main`` is closed unstarted.
    """
    try:
        factory = loop_factory(kind)
    except ImproperlyConfigured:
        main.close()
        raise
    return asyncio.run(main, loop_factory=factory)


class PerLoop[V]:
    """One value per event loop, made on the loop's first use.

    Loops on several threads (free-threaded Python, a server's loop per
    thread) share the registry: it is changed and walked only with its lock
    held, so two loops never race to add themselves, and a loop finds only
    its own value. Adding a loop drops the closed ones: a value that refers
    to its loop (a connection pool does) would otherwise keep it, and itself,
    alive, since the registry holds its loops weakly.
    """

    __slots__ = ("_lock", "_values")

    def __init__(self) -> None:
        """Start with no loop."""
        self._lock = threading.Lock()
        self._values: WeakKeyDictionary[asyncio.AbstractEventLoop, V] = (
            WeakKeyDictionary()
        )

    def get(self, make: Callable[[], V]) -> V:
        """The running loop's value, made now with ``make`` if it has none.

        ``make`` runs on the loop, outside the lock: another thread's loop
        never waits for it.

        Raises:
            RuntimeError: No event loop is running.
        """
        loop = asyncio.get_running_loop()
        value = self._values.get(loop)  # Only this loop adds its own.
        if value is not None:
            return value
        value = make()
        with self._lock:
            for closed in [other for other in self._values if other.is_closed()]:
                del self._values[closed]
            self._values[loop] = value
        return value

    def find(self) -> V | None:
        """The running loop's value; None: it has none yet.

        Raises:
            RuntimeError: No event loop is running.
        """
        return self._values.get(asyncio.get_running_loop())

    def pop(self) -> V | None:
        """Forget the running loop's value, and return it (None: it had none).

        Raises:
            RuntimeError: No event loop is running.
        """
        loop = asyncio.get_running_loop()
        with self._lock:
            return self._values.pop(loop, None)

    def __len__(self) -> int:
        """How many loops have a value (closed ones not dropped yet included)."""
        with self._lock:
            return len(self._values)
