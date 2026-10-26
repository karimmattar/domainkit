"""Count the SQL statements a block runs (Django's ``assertNumQueries``).

::

    with assert_num_queries(2):
        admin_client.get("/api/v1/shop/products")

    with assert_max_queries(5, using="reports") as queries:
        await build_report()
    assert "JOIN" in queries.statements[0]

Statements are counted on every engine of the process, in every thread and
event loop (a ``TestClient`` serves requests in a thread of its own), or
only on one alias's ``URL`` with ``using``. Each statement sent to the
driver counts once, an ``executemany`` included; the driver's own work
(connecting, beginning and ending transactions) does not, nor the timeouts
a PostgreSQL transaction sets for itself (``connections.INTERNAL``). A
failure lists the statements, as SQL with placeholders: bound values are
never kept.

The first block counted in a process puts one listener on SQLAlchemy's
``Engine`` class, which stays: it hands each statement to the blocks
entered at the time. SQLAlchemy's listener lists are read without a lock
by every thread that runs a statement, and adding or removing a listener
while another thread runs one fails that statement ("deque mutated during
iteration"), so entering and leaving a block never changes them.
"""

import threading
from types import TracebackType
from typing import Any, Final, Self

from sqlalchemy import event, make_url
from sqlalchemy.engine import URL, Connection, Engine, ExecutionContext

from domainkit.db import connections
from domainkit.db.connections import INTERNAL

EVENT: Final = "before_cursor_execute"
_lock: Final = threading.Lock()
"""Guards installing the listener, and replacing ``_capturing``."""
_capturing: "tuple[CapturedQueries, ...]" = ()
"""The blocks entered, in any thread. Replaced whole, never changed in place,
so the listener reads it without the lock."""


class CapturedQueries:
    """The SQL statements run while it is entered; checked against a count.

    ``assert_num_queries`` and ``assert_max_queries`` make one. Without an
    ``expected`` count it only captures.
    """

    def __init__(
        self,
        expected: int | None = None,
        *,
        at_most: bool = False,
        using: str | None = None,
    ) -> None:
        """Expect ``expected`` statements (at most, with ``at_most``) on ``using``."""
        self.expected = expected
        self.at_most = at_most
        self.using = using
        self.statements: list[str] = []
        """Each statement, in the order the driver received them."""
        self._url: URL | None = None

    def __len__(self) -> int:
        """Count the statements captured so far."""
        return len(self.statements)

    def __enter__(self) -> Self:
        """Start capturing."""
        global _capturing
        if self.using is not None:
            self._url = make_url(connections.config(self.using).URL.get_secret_value())
        with _lock:
            if not event.contains(Engine, EVENT, _dispatch):
                event.listen(Engine, EVENT, _dispatch)
            _capturing = (*_capturing, self)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Stop capturing; unless the block raised, check the count."""
        global _capturing
        with _lock:
            _capturing = tuple(each for each in _capturing if each is not self)
        if exc_type is None and not self.holds():
            raise AssertionError(self.report())

    def holds(self) -> bool:
        """Whether the count captured meets the expectation."""
        if self.expected is None:
            return True
        if self.at_most:
            return len(self) <= self.expected
        return len(self) == self.expected

    def report(self) -> str:
        """Describe the expectation and list the statements captured."""
        bound = "at most " if self.at_most else ""
        where = "" if self.using is None else f" on {self.using!r}"
        ending = ":" if self.statements else "."
        lines = [
            f"Expected {bound}{_queries(self.expected)}{where}, {len(self)} ran{ending}"
        ]
        lines += [
            f"{number}. {statement}"
            for number, statement in enumerate(self.statements, start=1)
        ]
        return "\n".join(lines)

    def _record(
        self,
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: Any,  # noqa: ANN401 - never read.
        context: ExecutionContext | None,
        executemany: bool,
    ) -> None:
        """Keep ``statement`` if it ran on the database counted, and is the work's."""
        if context is not None and context.execution_options.get(INTERNAL):
            return
        if self._url is None or connection.engine.url == self._url:
            self.statements.append(statement)


def _dispatch(
    connection: Connection,
    cursor: object,
    statement: str,
    parameters: Any,  # noqa: ANN401 - never read.
    context: ExecutionContext | None,
    executemany: bool,
) -> None:
    """Hand ``statement`` to every block entered now."""
    for capture in _capturing:
        capture._record(connection, cursor, statement, parameters, context, executemany)


def assert_num_queries(expected: int, *, using: str | None = None) -> CapturedQueries:
    """Check that the ``with`` block runs exactly ``expected`` SQL statements.

    ``using`` counts only those on that ``DATABASES`` alias's ``URL``.
    """
    return CapturedQueries(expected, using=using)


def assert_max_queries(limit: int, *, using: str | None = None) -> CapturedQueries:
    """Check that the ``with`` block runs at most ``limit`` SQL statements."""
    return CapturedQueries(limit, at_most=True, using=using)


def _queries(count: int | None) -> str:
    """``1 query``, ``2 queries``."""
    return f"{count} {'query' if count == 1 else 'queries'}"
