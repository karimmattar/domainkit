"""Range partitioning: declarations, periods, and maintenance plans."""

import asyncio
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import Column, Date, DateTime, Integer, MetaData, String, Table, Uuid

from domainkit import setup
from domainkit.conf import override_settings
from domainkit.db import UTCDateTime, connections, create_engine
from domainkit.db import partitioning as partitioning_module
from domainkit.db.partitioning import (
    Interval,
    Partition,
    RangePartitioning,
    existing_partitions,
    maintain,
    partition_by_range,
    partition_name,
    partitioned_tables,
    partitioning_of,
    period_start,
    plan,
    shift,
)
from domainkit.exceptions import ImproperlyConfigured
from domainkit_tests.conftest import MakeDatabase
from domainkit_tests.test_management import run

NOW = datetime(2026, 10, 14, 9, 30, tzinfo=UTC)


def events_table(metadata: MetaData | None = None, name: str = "events") -> Table:
    """A table with a composite key that includes the timestamp."""
    return Table(
        name,
        metadata or MetaData(),
        Column("id", Uuid, primary_key=True),
        Column("occurred_at", UTCDateTime, primary_key=True),
        Column("body", String(100)),
    )


def test_declaring_a_partitioned_table() -> None:
    """PostgreSQL gets PARTITION BY RANGE; the table remembers its policy."""
    table = partition_by_range(events_table(), "occurred_at", "month", retain=12)

    assert table.dialect_kwargs["postgresql_partition_by"] == "RANGE (occurred_at)"
    assert partitioning_of(table) == RangePartitioning(
        "occurred_at", Interval.MONTH, premake=3, retain=12
    )
    assert partitioning_of(events_table()) is None


@pytest.mark.parametrize(
    ("column", "options", "message"),
    [
        ("missing", {}, "has no column 'missing'"),
        ("body", {}, "must be part of the primary key"),
        ("occurred_at", {"premake": 0}, "premake must be at least 1"),
        ("occurred_at", {"retain": 0}, "retain must be at least 1"),
    ],
)
def test_partition_declarations_are_checked(
    column: str, options: dict[str, int], message: str
) -> None:
    """PostgreSQL's rules fail where the table is defined, not at migrate."""
    with pytest.raises(ImproperlyConfigured, match=message):
        partition_by_range(events_table(), column, "day", **options)


def test_partition_limits_are_inclusive() -> None:
    """The longest allowed name and the smallest retention are accepted."""
    longest = events_table(name="x" * 53)

    partition_by_range(longest, "occurred_at", "day", premake=1, retain=1)

    assert partitioning_of(longest) == RangePartitioning(
        "occurred_at", Interval.DAY, premake=1, retain=1
    )


def test_partition_keys_hold_dates_or_timestamps() -> None:
    """A range key must be temporal; a known name or partitioned table is refused."""
    numbers = Table("numbers", MetaData(), Column("n", Integer, primary_key=True))
    opaque = Table("opaque", MetaData(), Column("k", Uuid, primary_key=True))
    days = Table("days", MetaData(), Column("d", Date, primary_key=True))
    stamps = Table("stamps", MetaData(), Column("t", DateTime, primary_key=True))
    partitioned = partition_by_range(events_table(), "occurred_at", "week")

    for table in (numbers, opaque):
        with pytest.raises(ImproperlyConfigured, match="dates or timestamps"):
            partition_by_range(table, table.c[0].name, "day")
    with pytest.raises(ImproperlyConfigured, match="already partitioned"):
        partition_by_range(partitioned, "occurred_at", "day")
    with pytest.raises(ImproperlyConfigured, match="too long to partition"):
        partition_by_range(events_table(name="x" * 54), "occurred_at", "day")
    assert partitioning_of(partition_by_range(days, "d", "year")) is not None
    assert partitioning_of(partition_by_range(stamps, "t", "day")) is not None


@pytest.mark.parametrize(
    ("interval", "start", "next_start", "previous"),
    [
        ("day", (2026, 10, 14), (2026, 10, 15), (2026, 10, 13)),
        ("week", (2026, 10, 12), (2026, 10, 19), (2026, 10, 5)),
        ("month", (2026, 10, 1), (2026, 11, 1), (2026, 9, 1)),
        ("year", (2026, 1, 1), (2027, 1, 1), (2025, 1, 1)),
    ],
)
def test_periods_start_and_follow_in_utc(
    interval: str,
    start: tuple[int, int, int],
    next_start: tuple[int, int, int],
    previous: tuple[int, int, int],
) -> None:
    """Periods start at midnight UTC: Monday, the first, January the first."""
    period = Interval(interval)
    begins = period_start(period, NOW)

    assert begins == datetime(*start, tzinfo=UTC)
    assert shift(period, begins, 1) == datetime(*next_start, tzinfo=UTC)
    assert shift(period, begins, -1) == datetime(*previous, tzinfo=UTC)


def test_periods_start_at_midnight_exactly() -> None:
    """Seconds and microseconds are dropped too, and other zones convert."""
    late = datetime(2026, 10, 14, 23, 59, 59, 999999, tzinfo=UTC)
    plus_three = timezone(timedelta(hours=3))

    assert period_start(Interval.DAY, late) == datetime(2026, 10, 14, tzinfo=UTC)
    assert period_start(
        Interval.DAY, datetime(2026, 10, 15, 1, 0, tzinfo=plus_three)
    ) == datetime(2026, 10, 14, tzinfo=UTC)


def test_months_roll_over_years() -> None:
    """Month arithmetic crosses year boundaries both ways."""
    december = datetime(2026, 12, 1, tzinfo=UTC)

    assert shift(Interval.MONTH, december, 1) == datetime(2027, 1, 1, tzinfo=UTC)
    assert shift(Interval.MONTH, december, -12) == datetime(2025, 12, 1, tzinfo=UTC)
    assert partition_name("events", december) == "events_p20261201"


def test_plans_create_the_current_and_upcoming_partitions() -> None:
    """The current period and premake future ones; existing ones are kept."""
    table = partition_by_range(events_table(), "occurred_at", "month", premake=2)

    first = plan(table, set(), NOW)
    second = plan(table, {"events_p20261001", "events_p20261101"}, NOW)

    assert [partition.name for partition in first.create] == [
        "events_p20261001",
        "events_p20261101",
        "events_p20261201",
    ]
    assert first.create[0] == Partition(
        datetime(2026, 10, 1, tzinfo=UTC),
        datetime(2026, 11, 1, tzinfo=UTC),
        "events_p20261001",
    )
    assert [partition.name for partition in second.create] == ["events_p20261201"]
    assert first.drop == second.drop == ()
    assert first.table is table
    assert first.partitioning == partitioning_of(table)


def test_pruning_drops_only_expired_partitions_named_by_the_framework() -> None:
    """The last retain periods are kept; partitions named otherwise never go."""
    table = partition_by_range(events_table(), "occurred_at", "month", retain=2)
    existing = {
        "events_p20260601",
        "events_p20260701",
        "events_p20260801",
        "events_p20260901",
        "events_legacy",
        "events_p2026",
        "events_p20260101x",
        "events_pp20260101",
        "events_p_p20260101",
        "other_p20200101",
    }

    pruned = plan(table, existing, NOW, prune=True)

    assert pruned.drop == ("events_p20260601", "events_p20260701")
    assert plan(table, existing, NOW).drop == ()
    unlimited = partition_by_range(events_table(name="logs"), "occurred_at", "day")
    assert plan(unlimited, {"logs_p20200101"}, NOW, prune=True).drop == ()


def test_only_partitioned_tables_have_plans() -> None:
    """Planning a plain table is a programming error."""
    with pytest.raises(ImproperlyConfigured, match="is not partitioned"):
        plan(events_table(), set(), NOW)


@pytest.fixture
def partitioned_project(
    make_database: MakeDatabase, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[MetaData]:
    """A default database and metadata holding one partitioned table."""
    metadata = MetaData()
    partition_by_range(events_table(metadata), "occurred_at", "day", premake=1)
    elsewhere = events_table(metadata, name="elsewhere_events")
    elsewhere.info["domain"] = "elsewhere"
    partition_by_range(elsewhere, "occurred_at", "day")
    Table("plain", metadata, Column("d", Date, primary_key=True))
    monkeypatch.setattr(partitioning_module, "metadata", metadata)
    with override_settings(
        DATABASES={"default": {"URL": make_database("default")}},
        MIGRATIONS_DIR=tmp_path / "migrations",
    ):
        setup()
        yield metadata


def test_partitioned_tables_follow_the_routing(partitioned_project: MetaData) -> None:
    """Only partitioned tables of this target: not plain ones, not other domains'."""
    assert [table.name for table in partitioned_tables("default")] == ["events"]


def test_maintenance_needs_postgresql_and_created_tables(
    partitioned_project: MetaData,
) -> None:
    """SQLite has no partitions, and PostgreSQL skips tables not created yet."""

    async def lookup() -> frozenset[str]:
        engine = create_engine(connections.config("default"))
        try:
            async with engine.connect() as connection:
                return await connection.run_sync(existing_partitions)
        finally:
            await engine.dispose()

    assert asyncio.run(maintain("default")) == []
    assert asyncio.run(lookup()) == frozenset()
    assert run("partitions") == "Partitions of 'default' are up to date.\n"
    assert run("partitions", "--dry-run", "--database", "default") == ""


def test_maintenance_skips_databases_without_partitioned_tables(
    partitioned_project: MetaData, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No partitioned tables, no connection."""
    monkeypatch.setattr(partitioning_module, "metadata", MetaData())

    assert asyncio.run(maintain("default", now=NOW)) == []


def test_date_bounds_are_dates() -> None:
    """Date keys get date literals; timestamp keys keep their offset."""
    assert (
        partitioning_module._bound(NOW, as_date=True) == date(2026, 10, 14).isoformat()
    )
    assert partitioning_module._bound(NOW, as_date=False) == "2026-10-14T09:30:00+00:00"
