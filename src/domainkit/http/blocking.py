"""Find code that blocks the event loop before production does.

- With ``DEBUG`` on, the application runs its loop in asyncio's debug mode:
  a callback or task step that holds the loop for ``SLOW_CALLBACK_DURATION``
  seconds or more is logged by the ``asyncio`` logger, as ``Executing <Task
  ...> took 0.250 seconds``.
- ``pytest --detect-blocking`` fails a test that holds its loop that long
  (``domainkit.testing``).
- ``manage.py check`` warns about route handlers and dependencies of the
  installed domains written as ``def``: FastAPI calls them on its thread
  pool, a hop per call, and a busy service runs out of threads. Make them
  ``async def``, or mark one that must stay sync with ``allow_sync``::

      @allow_sync
      def render_pdf(order: Order) -> bytes: ...
"""

import asyncio
import functools
import inspect
from collections.abc import Callable, Iterable, Iterator, Sequence
from typing import Any, Final

from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, APIWebSocketRoute, iter_route_contexts
from starlette.routing import BaseRoute, Host, Mount

SYNC_ALLOWED: Final = "__domainkit_sync_allowed__"
"""The attribute ``allow_sync`` sets."""
ROUTES_NAMED: Final = 3
"""Routes a warning names before it counts the rest."""


def allow_sync[T](call: T) -> T:
    """Mark a sync route handler or dependency as sync on purpose.

    ``check`` stops warning about it; FastAPI still calls it on its thread
    pool. Mark a class to cover its instances.
    """
    setattr(call, SYNC_ALLOWED, True)
    return call


def watch_event_loop(loop: asyncio.AbstractEventLoop, seconds: float) -> None:
    """Log each callback or task step that holds ``loop`` for ``seconds`` or more.

    It turns on the loop's debug mode, which also checks that other threads
    hand it work with ``call_soon_threadsafe`` and reports resources left
    open: for development, not production.
    """
    loop.set_debug(True)
    loop.slow_callback_duration = seconds


def sync_calls(app: FastAPI, packages: Iterable[str]) -> list[str]:
    """Warn about each ``def`` handler or dependency of ``packages``' routes.

    Only what is defined in ``packages`` counts: the project can change it.
    Classes are left alone: FastAPI builds every class dependency on its
    thread pool, and ``async def`` cannot apply to one.
    """
    owned = tuple(f"{package}." for package in packages)
    routes_of: dict[str, list[str]] = {}
    for route, dependant in _dependants(app.routes):
        for name in dict.fromkeys(_sync_names(dependant)):
            if name.startswith(owned):
                routes_of.setdefault(name, []).append(route)
    return [
        f"{name} is a def, so FastAPI calls it on its thread pool for "
        f"{_listed(routes)}; make it async def, or mark it with "
        "domainkit.http.blocking.allow_sync."
        for name, routes in routes_of.items()
    ]


def _dependants(
    routes: Sequence[BaseRoute], prefix: str = ""
) -> Iterator[tuple[str, Dependant]]:
    """``METHOD /path`` and the dependencies of every route, mounts included."""
    for context in iter_route_contexts(routes):
        route = context.original_route
        # Routes of included routers keep their full path (and a WebSocket
        # its dependencies) here; FastAPI's own context has them for others.
        effective = getattr(context, "starlette_route", None)
        if isinstance(effective, type(route)):
            route = effective
        if isinstance(route, APIWebSocketRoute):
            yield f"WEBSOCKET {prefix}{route.path}", route.dependant
        elif isinstance(route, APIRoute):
            methods = " ".join(sorted(route.methods or ()))
            yield f"{methods} {prefix}{context.path}", context.dependant
        elif isinstance(route, Mount | Host) and route.routes:
            inner = prefix + route.path if isinstance(route, Mount) else prefix
            yield from _dependants(route.routes, inner)


def _calls(dependant: Dependant) -> Iterator[Callable[..., Any]]:
    """The handler, or dependency, and every dependency below it."""
    if dependant.call is not None:
        yield dependant.call
    for dependency in dependant.dependencies:
        yield from _calls(dependency)


def _sync_names(dependant: Dependant) -> Iterator[str]:
    """The names of the functions and callable objects FastAPI runs on threads."""
    for call in _calls(dependant):
        if inspect.isclass(_unwrapped(call)) or _allowed(call):
            continue
        if _runs_on_threads(call):
            yield _name(call)


def _runs_on_threads(call: Callable[..., Any]) -> bool:
    """Whether FastAPI calls ``call``, not a class, on its thread pool."""
    target = _unwrapped(call)
    if not inspect.isroutine(target):
        target = _unwrapped(type(target).__call__)  # A callable instance.
    return not (
        inspect.iscoroutinefunction(target) or inspect.isasyncgenfunction(target)
    )


def _unwrapped(call: Callable[..., Any]) -> Any:  # noqa: ANN401 - any callable.
    """``call`` without ``functools.partial`` or decorators around it."""
    while isinstance(call, functools.partial):
        call = call.func
    return inspect.unwrap(call)


def _allowed(call: Callable[..., Any]) -> bool:
    """Whether ``allow_sync`` marked ``call``, what it wraps, or its class."""
    return any(
        getattr(target, SYNC_ALLOWED, False) is True
        for target in (call, _unwrapped(call), type(call))
    )


def _name(call: Callable[..., Any]) -> str:
    """The dotted name of ``call``, or of its class for an instance."""
    target = _unwrapped(call)
    if not inspect.isroutine(target):
        target = type(target)
    return f"{target.__module__}.{target.__qualname__}"


def _listed(routes: list[str]) -> str:
    """The first routes, and how many more."""
    shown = ", ".join(routes[:ROUTES_NAMED])
    more = len(routes) - ROUTES_NAMED
    return f"{shown} and {more} more" if more > 0 else shown
