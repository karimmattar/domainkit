"""Count the SQL a block of code runs, by statement shape, never by value.

``record_queries`` gives a ``QueryLog`` that every statement run inside it,
on any engine, adds to: how many, how long, and how often each shape ran.
A shape is the statement with its literals and placeholders replaced by
``?`` and lists of them by ``(...)``, so ``SELECT ... WHERE id = $1`` run
for each of fifty rows is one shape run fifty times: an N+1. Bound values
are never read.
"""

import re
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine, ExecutionContext

MAX_SHAPE: Final = 500
"""Characters of a shape kept; longer statements are cut."""
_STRING: Final = re.compile(r"'(?:[^']|'')*'")
_NUMBER: Final = re.compile(r"(?<![\w$.])-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?\b")
_PLACEHOLDER: Final = re.compile(r"\$\d+|%\(\w+\)s|%s|\?")
_LIST: Final = re.compile(r"\(\s*\?(?:\s*,\s*\?)+\s*\)")
_SPACE: Final = re.compile(r"\s+")
_STARTED: Final = "domainkit_debug_started"

_current: ContextVar["QueryLog | None"] = ContextVar("domainkit_queries", default=None)


@dataclass(slots=True)
class QueryLog:
    """The statements run while it was recording."""

    count: int = 0
    seconds: float = 0.0
    shapes: Counter[str] = field(default_factory=Counter)

    def add(self, statement: str, seconds: float) -> None:
        """Count one statement that took ``seconds``."""
        self.count += 1
        self.seconds += seconds
        self.shapes[statement_shape(statement)] += 1

    def repeated(self, times: int) -> list[tuple[str, int]]:
        """Shapes run at least ``times`` times, most often first."""
        return [
            (shape, runs) for shape, runs in self.shapes.most_common() if runs >= times
        ]


def statement_shape(statement: str) -> str:
    """``statement`` without its values: literals and placeholders become ``?``."""
    shape = _STRING.sub("?", statement)
    shape = _PLACEHOLDER.sub("?", shape)
    shape = _NUMBER.sub("?", shape)
    shape = _LIST.sub("(...)", shape)
    shape = _SPACE.sub(" ", shape).strip()
    return shape if len(shape) <= MAX_SHAPE else f"{shape[:MAX_SHAPE]}..."


@contextmanager
def record_queries() -> Iterator[QueryLog]:
    """Record the statements run in this context (and tasks it starts)."""
    _listen()
    log = QueryLog()
    token = _current.set(log)
    try:
        yield log
    finally:
        _current.reset(token)


def _listen() -> None:
    """Listen to every engine's statements, once."""
    if not event.contains(Engine, "before_cursor_execute", _before):
        event.listen(Engine, "before_cursor_execute", _before)
        event.listen(Engine, "after_cursor_execute", _after)


def _before(
    conn: Connection,
    cursor: Any,  # noqa: ANN401 - the driver's cursor.
    statement: str,
    parameters: Any,  # noqa: ANN401 - never read.
    context: ExecutionContext | None,
    executemany: bool,
) -> None:
    """Note when a statement starts, if something records."""
    if _current.get() is not None:
        conn.info.setdefault(_STARTED, []).append(time.perf_counter())


def _after(
    conn: Connection,
    cursor: Any,  # noqa: ANN401 - the driver's cursor.
    statement: str,
    parameters: Any,  # noqa: ANN401 - never read.
    context: ExecutionContext | None,
    executemany: bool,
) -> None:
    """Add the statement and its time to the log recording now."""
    log = _current.get()
    started = conn.info.get(_STARTED)
    if log is None or not started:
        return
    log.add(statement, time.perf_counter() - started.pop())
