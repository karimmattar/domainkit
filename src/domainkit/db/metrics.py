"""Statement metrics (``METRICS_STATEMENTS``, with ``domainkit[metrics]``).

``db_statement_duration_seconds`` times each statement a pool's
connections send, from the moment SQLAlchemy hands it to the driver to
its answer, or its failure. It is labelled by the database's alias and the
statement's first keyword (``SELECT``, ``INSERT``, ``UPDATE``, ``DELETE``,
else ``OTHER``): never its text or its values, so the series stay few.
``ConnectionHandler`` listens on each engine it makes while the setting is
on; this module is imported only then.
"""

import time
from typing import Any, Final

from prometheus_client import Histogram
from sqlalchemy.engine import ExceptionContext

OPERATIONS: Final = ("SELECT", "INSERT", "UPDATE", "DELETE")
"""First keywords counted by name; any other statement is ``OTHER``."""
OTHER: Final = "OTHER"
STATEMENTS: Final = Histogram(
    "db_statement_duration_seconds",
    "SQL statements' time from the driver to their answer, by alias and first keyword.",
    ["database", "operation"],
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1, 5, 10),
)
STARTED: Final = "domainkit_started"
"""The attribute of a statement's execution context that holds its start
(``time.perf_counter``): set directly where every statement passes, as a
call costs more."""

type Timers = dict[str, Histogram]
"""One alias's histogram per first keyword, in either case, and ``OTHER``."""


def timers(database: str) -> Timers:
    """The histogram of each operation of ``database``, looked up once."""
    found: Timers = {
        operation: STATEMENTS.labels(database, operation)
        for operation in (*OPERATIONS, OTHER)
    }
    for operation in OPERATIONS:
        found[operation.lower()] = found[operation]
    return found


def started(
    connection: object,
    cursor: object,
    statement: str,
    parameters: object,
    context: Any,  # noqa: ANN401 - SQLAlchemy's execution context.
    executemany: bool,
) -> tuple[str, object]:
    """``before_cursor_execute``: note when the statement left for the driver."""
    context.domainkit_started = time.perf_counter()
    return statement, parameters


def ended(
    found: Timers,
    connection: object,
    cursor: object,
    statement: str,
    parameters: object,
    context: Any,  # noqa: ANN401 - SQLAlchemy's execution context.
    executemany: bool,
) -> None:
    """``after_cursor_execute``: time the statement that answered."""
    _observe(found, statement, time.perf_counter() - context.domainkit_started)


def failed(found: Timers, error: ExceptionContext) -> None:
    """``handle_error``: time the statement that failed, if one was sent.

    An error before the statement reached the driver (no connection, a
    refused parameter) has no start, and is not timed.
    """
    began = getattr(error.execution_context, STARTED, None)
    if began is not None:
        _observe(found, error.statement or "", time.perf_counter() - began)


def _observe(found: Timers, statement: str, seconds: float) -> None:
    """Record ``seconds`` for ``statement``, by its first keyword."""
    words = statement.split(None, 1)
    timer = found.get(words[0], found[OTHER]) if words else found[OTHER]
    timer.observe(seconds)
