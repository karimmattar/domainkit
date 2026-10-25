"""The database's clock, which decides when shared leases and keys expire.

Hosts that share a database share its rows' times. If each read its own
clock, a host running ahead would take other hosts' leases for expired, and
a host running behind would keep expired rows alive. So on PostgreSQL the
database's server decides: expiry checks, and the times written to be
checked later, compare against ``statement_timestamp()``. The outbox
(ADR 0043) and idempotency keys (ADR 0062) use it.

Two cases read ``domainkit.clock`` instead:

- SQLite, whose database lives in the host's process, so its clock is the
  host's anyway;
- a frozen clock (``freeze_time``): tests move time, and the leases move
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
    """The current time as a database of ``dialect`` should see it."""
    return at(dialect, 0.0)


def at(dialect: str, seconds: float) -> Moment:
    """``seconds`` from now (earlier when negative), on the database's clock."""
    if uses_database(dialect):
        moment: ColumnElement[datetime] = func.statement_timestamp(
            type_=DateTime(timezone=True)
        )
        return moment + timedelta(seconds=seconds) if seconds else moment
    return clock.now() + timedelta(seconds=seconds)
