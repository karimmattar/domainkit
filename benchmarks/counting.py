"""Count what work costs: Python function calls and database round trips.

Both counters are exact, so CI can gate on them where timings on a shared
runner cannot be trusted (docs/performance.md, ADR 0064).

- ``CallCounter`` counts every function call cProfile sees while it is
  entered, Python and built-in, in every thread of the process. Garbage
  collection is held off inside it, so a collection that happens to fall in
  the window does not add the calls of finalizers. Other threads must be
  quiet at both edges of the window: the ladder flushes the logging thread
  first.
- ``SqlCounter`` counts what reaches the database through SQLAlchemy, on
  every engine: statements sent to the driver, transactions begun,
  connections checked out of a pool, and the pings that test a connection
  before it is handed out (``POOL_PRE_PING``).

::

    with SqlCounter() as sql, CallCounter() as calls:
        await drive(app, requests=200)
    print(calls.calls / 200, sql.counts.statements / 200)
"""

import cProfile
import gc
import pstats
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Self

from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.engine.default import DefaultDialect
from sqlalchemy.engine.interfaces import DBAPIConnection, ExecutionContext
from sqlalchemy.pool import ConnectionPoolEntry, Pool, PoolProxiedConnection

type _Ping = Callable[[DefaultDialect, DBAPIConnection], bool]


class CallCounter:
    """Count the function calls made while it is entered.

    ``calls`` is set when the block ends; ``stats()`` gives cProfile's
    per-function figures, to see where calls went or to diff two runs.
    """

    def __init__(self) -> None:
        """Make a counter; nothing is counted until it is entered."""
        self.calls = 0
        """Calls counted, once the block has ended."""
        self._profile = cProfile.Profile()
        self._collecting = False

    def __enter__(self) -> Self:
        """Collect garbage, hold the collector off, and start counting."""
        self._collecting = gc.isenabled()
        gc.collect()
        gc.disable()
        self._profile.enable()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Stop counting and let the collector run again, as before."""
        self._profile.disable()
        if self._collecting:
            gc.enable()
        self.calls = sum(entry[1] for entry in self.stats().stats.values())  # type: ignore[attr-defined]

    def stats(self) -> pstats.Stats:
        """The statistics cProfile kept for the block, per function."""
        return pstats.Stats(self._profile)


@dataclass(slots=True)
class RoundTrips:
    """What reached the database."""

    statements: int = 0
    """Statements sent to the driver (an ``executemany`` counts once)."""
    transactions: int = 0
    """Transactions begun on a connection."""
    checkouts: int = 0
    """Connections taken from a pool."""
    pings: int = 0
    """Connections tested before being handed out (``POOL_PRE_PING``)."""
    verbs: Counter[str] = field(default_factory=Counter)
    """Statements by their first word, upper case (``SELECT``, ``INSERT``)."""


class SqlCounter:
    """Count statements, transactions, checkouts, and pings on every engine.

    Counters may be nested; each counts what happens while it is entered.
    """

    def __init__(self) -> None:
        """Make a counter; nothing is counted until it is entered."""
        self.counts = RoundTrips()
        self._ping: _Ping | None = None

    def __enter__(self) -> Self:
        """Start counting."""
        event.listen(Engine, "before_cursor_execute", self._statement)
        event.listen(Engine, "begin", self._begin)
        event.listen(Pool, "checkout", self._checkout)
        # A ping is no event: every pool pings through its dialect's
        # _do_ping_w_event (SQLAlchemy 2.0 and 2.1), so count it there.
        self._ping = original = DefaultDialect._do_ping_w_event
        counts = self.counts

        def counted(dialect: DefaultDialect, connection: DBAPIConnection) -> bool:
            counts.pings += 1
            return original(dialect, connection)

        DefaultDialect._do_ping_w_event = counted  # type: ignore[method-assign,assignment]
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Stop counting."""
        if self._ping is not None:
            DefaultDialect._do_ping_w_event = self._ping  # type: ignore[method-assign,assignment]
            self._ping = None
        event.remove(Pool, "checkout", self._checkout)
        event.remove(Engine, "begin", self._begin)
        event.remove(Engine, "before_cursor_execute", self._statement)

    def _statement(
        self,
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: Any,  # noqa: ANN401 - never read.
        context: ExecutionContext | None,
        executemany: bool,
    ) -> None:
        """Count a statement, and its verb."""
        self.counts.statements += 1
        words = statement.split(None, 1)
        self.counts.verbs[words[0].upper() if words else ""] += 1

    def _begin(self, connection: Connection) -> None:
        """Count a transaction."""
        self.counts.transactions += 1

    def _checkout(
        self,
        dbapi_connection: DBAPIConnection,
        record: ConnectionPoolEntry,
        proxy: PoolProxiedConnection,
    ) -> None:
        """Count a checkout."""
        self.counts.checkouts += 1
