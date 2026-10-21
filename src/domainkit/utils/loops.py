"""Event loops for the framework's own entry points: uvloop when it is there.

Management commands with an ``async`` handle (``runworker`` among them) and
``runserver`` run on uvloop when the ``uvloop`` extra is installed and
``EVENT_LOOP`` is ``"auto"``, or always on asyncio's own loop with
``"asyncio"``. Other entry points share the choice through ``loop_factory``::

    asyncio.run(main(), loop_factory=loop_factory("auto"))
"""

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any, Literal

from domainkit.exceptions import ImproperlyConfigured

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
