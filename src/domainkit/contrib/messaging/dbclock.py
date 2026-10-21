"""The clock that decides when outbox work is due and when locks expire.

Workers on several hosts share an outbox. If each read its own clock, a host
running ahead would take other workers' locks for expired and run their
messages twice, and a host running behind would leave due work waiting.
So on PostgreSQL the outbox's own server decides: due checks, lock expiry,
and the times workers write compare against ``statement_timestamp()`` (see
ADR 0043).

Two cases read ``domainkit.clock`` instead:

- SQLite, whose database lives in the worker's process, so its clock is
  the host's anyway;
- a frozen clock (``freeze_time``): tests move time, and the outbox moves
  with it on every database.
"""

from datetime import datetime, timedelta

from sqlalchemy import ColumnElement, func
from sqlalchemy.types import DateTime

from domainkit import clock

type Moment = datetime | ColumnElement[datetime]
"""A time to compare or store: a value, or the database's clock in SQL."""


def uses_database(dialect: str) -> bool:
    """Report whether the database's clock decides on ``dialect`` right now."""
    return dialect == "postgresql" and clock.frozen() is None


def now(dialect: str) -> Moment:
    """The current time as the outbox of ``dialect`` should see it."""
    return at(dialect, 0.0)


def at(dialect: str, seconds: float) -> Moment:
    """``seconds`` from now (earlier when negative), on the outbox's clock."""
    if uses_database(dialect):
        moment: ColumnElement[datetime] = func.statement_timestamp(
            type_=DateTime(timezone=True)
        )
        return moment + timedelta(seconds=seconds) if seconds else moment
    return clock.now() + timedelta(seconds=seconds)
