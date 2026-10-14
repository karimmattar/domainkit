"""Range partitioning of large PostgreSQL tables, managed by the framework.

Declare a table partitioned where the table is defined::

    events = Table(
        "billing_events",
        metadata,
        Column("id", Uuid, primary_key=True),
        Column("occurred_at", UTCDateTime, primary_key=True),
        info={"domain": "billing"},
    )
    partition_by_range(events, "occurred_at", Interval.MONTH, premake=3, retain=24)

On PostgreSQL the table is created ``PARTITION BY RANGE (occurred_at)``, and
each period gets a partition named ``<table>_p<YYYYMMDD>`` (its start, in
UTC) covering ``[start, next start)``. ``migrate`` creates the current and
``premake`` future partitions; ``manage.py partitions`` keeps them coming
and, with ``--prune``, drops partitions older than ``retain`` periods. On
other databases the table is a plain table. See ADR 0010.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final

from sqlalchemy import Column, Connection, Date, DateTime, Table, text
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.types import TypeEngine

from domainkit.db.connections import connections, create_owner_engine
from domainkit.db.orm import metadata
from domainkit.db.routing import router
from domainkit.exceptions import ImproperlyConfigured

PARTITIONING: Final = "partitioning"
"""Key in ``Table.info`` holding a table's ``RangePartitioning``."""
_SUFFIX: Final = re.compile(r"_p(\d{8})$")
_NAME_LIMIT: Final = 63
"""PostgreSQL's identifier length; longer names are silently truncated."""
_SUFFIX_LENGTH: Final = len("_p20260101")
_LOCK: Final = "SELECT pg_advisory_xact_lock(hashtext('domainkit.partitions'))"
"""Serializes maintenance per database: replicas may migrate at the same time."""


class Interval(StrEnum):
    """The length of one partition."""

    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"


@dataclass(frozen=True, slots=True)
class RangePartitioning:
    """How a table is partitioned and how many partitions to keep."""

    column: str
    interval: Interval
    premake: int
    """Future periods kept ready, beyond the current one."""
    retain: int | None
    """Past periods kept by ``partitions --prune``; None keeps everything."""


@dataclass(frozen=True, slots=True, order=True)
class Partition:
    """One partition: ``[start, end)``."""

    start: datetime
    end: datetime
    name: str


@dataclass(frozen=True, slots=True)
class MaintenancePlan:
    """What maintenance would do to one table."""

    table: Table
    partitioning: RangePartitioning
    create: tuple[Partition, ...]
    drop: tuple[str, ...]


def partition_by_range(
    table: Table,
    column: str,
    interval: Interval | str,
    *,
    premake: int = 3,
    retain: int | None = None,
) -> Table:
    """Declare ``table`` range-partitioned by the timestamp ``column``.

    PostgreSQL requires the column in the primary key (and in every unique
    constraint), so ``(id, occurred_at)``, not ``id`` alone; this is checked
    here, when the table is defined.
    """
    period = Interval(interval)
    if PARTITIONING in table.info:
        raise ImproperlyConfigured(f"Table {table.name!r} is already partitioned.")
    if len(table.name) > _NAME_LIMIT - _SUFFIX_LENGTH:
        raise ImproperlyConfigured(
            f"Table {table.name!r} is too long to partition: names of its "
            f"partitions would exceed {_NAME_LIMIT} characters."
        )
    if column not in table.c:
        raise ImproperlyConfigured(f"Table {table.name!r} has no column {column!r}.")
    partition_column = table.c[column]
    if not partition_column.primary_key:
        raise ImproperlyConfigured(
            f"{table.name}.{column} must be part of the primary key: PostgreSQL "
            "requires the partition key in every unique constraint."
        )
    if not _is_temporal(partition_column):
        raise ImproperlyConfigured(
            f"{table.name}.{column} must hold dates or timestamps to be a range key."
        )
    if premake < 1:
        raise ImproperlyConfigured("premake must be at least 1.")
    if retain is not None and retain < 1:
        raise ImproperlyConfigured("retain must be at least 1, or None.")
    table.dialect_kwargs["postgresql_partition_by"] = f"RANGE ({column})"
    # Plain values: Alembic writes Table.info into revisions as Python literals.
    table.info[PARTITIONING] = {
        "column": column,
        "interval": period.value,
        "premake": premake,
        "retain": retain,
    }
    return table


def _storage_type(column: Column[object]) -> TypeEngine[object]:
    """The type a column is stored as (a ``TypeDecorator``'s implementation)."""
    return getattr(column.type, "impl_instance", column.type)


def _is_temporal(column: Column[object]) -> bool:
    """Whether ``column`` stores dates or timestamps."""
    return isinstance(_storage_type(column), DateTime | Date)


def _is_date(column: Column[object]) -> bool:
    """Whether ``column`` stores dates without a time of day."""
    return isinstance(_storage_type(column), Date)


def partitioning_of(table: Table) -> RangePartitioning | None:
    """The partitioning declared on ``table``, if any."""
    declared = table.info.get(PARTITIONING)
    if not isinstance(declared, dict):
        return None
    return RangePartitioning(
        column=declared["column"],
        interval=Interval(declared["interval"]),
        premake=declared["premake"],
        retain=declared["retain"],
    )


def period_start(interval: Interval, moment: datetime) -> datetime:
    """The start (UTC) of the period that contains ``moment``."""
    moment = moment.astimezone(UTC)
    day = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    match interval:
        case Interval.DAY:
            return day
        case Interval.WEEK:
            return day - timedelta(days=day.weekday())
        case Interval.MONTH:
            return day.replace(day=1)
        case _:  # Interval.YEAR
            return day.replace(month=1, day=1)


def shift(interval: Interval, start: datetime, periods: int) -> datetime:
    """The start ``periods`` periods after ``start`` (before, if negative)."""
    match interval:
        case Interval.DAY:
            return start + timedelta(days=periods)
        case Interval.WEEK:
            return start + timedelta(weeks=periods)
        case Interval.MONTH:
            months = start.year * 12 + start.month - 1 + periods
            return start.replace(year=months // 12, month=months % 12 + 1)
        case _:  # Interval.YEAR
            return start.replace(year=start.year + periods)


def partition_name(table_name: str, start: datetime) -> str:
    """``<table>_p<YYYYMMDD>``."""
    return f"{table_name}_p{start:%Y%m%d}"


def plan(
    table: Table, existing: set[str], now: datetime, *, prune: bool = False
) -> MaintenancePlan:
    """Partitions to create, and with ``prune`` the expired ones to drop.

    Only partitions named by this module are ever dropped; others are left
    alone.
    """
    partitioning = partitioning_of(table)
    if partitioning is None:
        raise ImproperlyConfigured(f"Table {table.name!r} is not partitioned.")
    interval = partitioning.interval
    current = period_start(interval, now)
    create = []
    for offset in range(partitioning.premake + 1):
        start = shift(interval, current, offset)
        name = partition_name(table.name, start)
        if name not in existing:
            create.append(Partition(start, shift(interval, start, 1), name))
    drop: list[str] = []
    if prune and partitioning.retain is not None:
        oldest_kept = shift(interval, current, -partitioning.retain)
        for name in sorted(existing):
            begins = _start_of(table.name, name)
            if begins is not None and shift(interval, begins, 1) <= oldest_kept:
                drop.append(name)
    return MaintenancePlan(table, partitioning, tuple(create), tuple(drop))


def _start_of(table_name: str, partition: str) -> datetime | None:
    """The start encoded in a partition name, or None for other names."""
    if not partition.startswith(f"{table_name}_p"):
        return None
    match = _SUFFIX.search(partition)
    if match is None or len(partition) != len(table_name) + _SUFFIX_LENGTH:
        return None
    return datetime.strptime(match[1], "%Y%m%d").replace(tzinfo=UTC)


def partitioned_tables(alias: str) -> list[Table]:
    """The partitioned tables that belong in the database ``alias``."""
    target = router.target_of_alias(alias)
    return [
        table
        for table in metadata.sorted_tables
        if partitioning_of(table) is not None
        and router.holds_tables(table.info.get("domain"), target)
    ]


def existing_partitions(connection: Connection) -> frozenset[str]:
    """Names of every partition table in the database (none outside PostgreSQL)."""
    if connection.dialect.name != "postgresql":
        return frozenset()
    rows = connection.exec_driver_sql(
        "SELECT relname FROM pg_class WHERE relispartition AND relkind IN ('r', 'p')"
    )
    return frozenset(rows.scalars())


async def maintain(
    alias: str,
    *,
    now: datetime | None = None,
    prune: bool = False,
    dry_run: bool = False,
) -> list[str]:
    """Bring the partitions of ``alias`` up to date; return the SQL run (or planned).

    Creates the current and ``premake`` future partitions of every
    partitioned table present in the database; with ``prune``, detaches and
    drops partitions older than ``retain`` periods. Does nothing outside
    PostgreSQL, or for tables not created yet.
    """
    tables = partitioned_tables(alias)
    if not tables:
        return []
    engine = create_owner_engine(connections.config(alias))
    statements: list[str] = []
    try:
        async with engine.begin() as connection:
            if connection.dialect.name != "postgresql":
                return []
            await connection.execute(text(_LOCK))
            for table in tables:
                existing = await _partitions_of(connection, table)
                if existing is None:
                    continue
                maintenance = plan(
                    table, existing, now or datetime.now(UTC), prune=prune
                )
                for sql in _statements(connection, maintenance):
                    statements.append(sql)
                    if not dry_run:
                        await connection.execute(text(sql))
    finally:
        await engine.dispose()
    return statements


async def _partitions_of(connection: AsyncConnection, table: Table) -> set[str] | None:
    """The partitions of ``table``; None when the table does not exist."""
    # The quoted, schema-qualified name, so mixed case and schemas resolve.
    name = connection.dialect.identifier_preparer.format_table(table)
    exists = await connection.execute(
        text("SELECT to_regclass(:name) IS NOT NULL"), {"name": name}
    )
    if not exists.scalar_one():
        return None
    rows = await connection.execute(
        text(
            "SELECT child.relname FROM pg_inherits "
            "JOIN pg_class child ON child.oid = pg_inherits.inhrelid "
            "WHERE pg_inherits.inhparent = to_regclass(:name)"
        ),
        {"name": name},
    )
    return set(rows.scalars())


def _statements(connection: AsyncConnection, maintenance: MaintenancePlan) -> list[str]:
    """The DDL that carries out ``maintenance``."""
    preparer = connection.dialect.identifier_preparer
    table = maintenance.table
    as_date = _is_date(table.c[maintenance.partitioning.column])
    parent = preparer.format_table(table)
    schema = f"{preparer.quote_schema(table.schema)}." if table.schema else ""

    def quote(name: str) -> str:
        """A partition's name, in its parent's schema."""
        return f"{schema}{preparer.quote(name)}"

    statements = [
        f"CREATE TABLE IF NOT EXISTS {quote(partition.name)} PARTITION OF {parent} "
        f"FOR VALUES FROM ('{_bound(partition.start, as_date=as_date)}') "
        f"TO ('{_bound(partition.end, as_date=as_date)}')"
        for partition in maintenance.create
    ]
    for name in maintenance.drop:
        statements.append(f"ALTER TABLE {parent} DETACH PARTITION {quote(name)}")
        statements.append(f"DROP TABLE {quote(name)}")
    return statements


def _bound(moment: datetime, *, as_date: bool) -> str:
    """A partition bound literal: a date, or an ISO timestamp with its offset."""
    return moment.date().isoformat() if as_date else moment.isoformat()
