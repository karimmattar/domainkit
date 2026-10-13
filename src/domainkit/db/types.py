"""Column types that behave the same on every supported database."""

from datetime import UTC, datetime

from sqlalchemy import DateTime
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC timestamps, also on SQLite (which has no time zones).

    Naive datetimes are rejected rather than guessed; values come back aware
    and in UTC, so comparisons never mix naive and aware times.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(
        self, value: datetime | None, dialect: Dialect
    ) -> datetime | None:
        """Convert to UTC; SQLite stores it without the offset."""
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("UTCDateTime needs a timezone-aware datetime.")
        value = value.astimezone(UTC)
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(
        self, value: datetime | None, dialect: Dialect
    ) -> datetime | None:
        """Return an aware UTC datetime."""
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
