"""On PostgreSQL, limits stamp and judge by the database's clock, whatever the host's.

Each test runs the host's clock off the real one, not frozen. A window's
start, a subject's touch, a movement's and a release's time, and the purge's
cutoff are PostgreSQL's (``dbclock``), as every process that counts against
one limit must agree on them. PostgreSQL only: SQLite's clock is the host's.
"""

import os
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Final

import pytest
from sqlalchemy import func, select

from domainkit import clock
from domainkit.contrib.limits import (
    Direction,
    LimitsUnitOfWork,
    Movement,
    Window,
    consume,
    purge_usage,
    release,
    usage_of,
)
from domainkit.contrib.limits.tables import subjects, usage
from domainkit.db import connections
from domainkit_tests.conftest import POSTGRES_VARIABLE
from domainkit_tests.test_limits import books, egp  # noqa: F401 - the fixture.
from domainkit_tests.test_limits_usage import limit

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.usefixtures("books"),
    pytest.mark.skipif(
        not os.environ.get(POSTGRES_VARIABLE), reason=f"set {POSTGRES_VARIABLE}"
    ),
]
NEAR: Final = timedelta(minutes=5)
"""How far apart two readings of one clock may be, however slow the test runs."""

type Skew = Callable[[timedelta], None]


@pytest.fixture
def skew(monkeypatch: pytest.MonkeyPatch) -> Skew:
    """Set how far the host's clock runs from the real one (none to start with)."""
    real = clock.now
    off = [timedelta(0)]
    monkeypatch.setattr(clock, "now", lambda: real() + off[0])

    def set_off(by: timedelta) -> None:
        off[0] = by

    return set_off


async def database_now() -> datetime:
    """PostgreSQL's clock."""
    async with connections.engine().connect() as connection:
        moment = await connection.scalar(select(func.statement_timestamp()))
    assert isinstance(moment, datetime)
    return moment


async def spend(holder: str, *, key: str | None = None) -> None:
    """Count a pound out of ``holder``, and commit."""
    async with LimitsUnitOfWork() as uow:
        await consume(uow, Movement(egp("1"), {"holder": holder}), key=key)
        await uow.commit()


async def times(column: object) -> list[datetime]:
    """The values of ``column``, in no order."""
    async with connections.engine().connect() as connection:
        return list(await connection.scalars(select(column)))  # type: ignore[call-overload]


def near(moments: list[datetime], now: datetime) -> bool:
    """Whether every one of ``moments`` is ``now``, near enough."""
    return bool(moments) and all(abs(each - now) < NEAR for each in moments)


async def test_movements_and_subjects_are_stamped_by_the_database(
    skew: Skew,
) -> None:
    """A bounded subject's touch and its usage are the database's.

    The payee no window bounds keeps neither (PERF-26).
    """
    await limit(code="daily", window=Window.DAY, max_amount=egp("100"))
    skew(timedelta(days=3))
    await spend("h-1", key="k")
    async with LimitsUnitOfWork() as uow:
        await consume(uow, Movement(egp("1"), {"payee": "p-1"}))
        await uow.commit()
    async with LimitsUnitOfWork() as uow:
        await release(uow, "k")
        await uow.commit()
    now = await database_now()

    assert near(await times(subjects.c.touched_at), now)
    assert near(await times(usage.c.at), now)
    released = [each for each in await times(usage.c.released_at) if each]
    assert near(released, now)


async def test_a_window_starts_by_the_databases_day(skew: Skew) -> None:
    """Two days ahead on the host, the window is still the database's day."""
    await limit(code="daily", window=Window.DAY, max_amount=egp("100"))
    await spend("h-1")
    skew(timedelta(days=2))
    (found,) = await usage_of("holder", "h-1")
    now = await database_now()

    assert found.since == datetime(now.year, now.month, now.day, tzinfo=UTC)
    assert found.count == 1


async def test_the_purge_cuts_off_by_the_databases_clock(skew: Skew) -> None:
    """Forty days ahead on the host, nothing the database dates today is old.

    A window bounds the subject, so it has a row and usage (PERF-26).
    """
    await limit(code="daily", window=Window.DAY, max_amount=egp("100"))
    await spend("h-1")
    skew(timedelta(days=40))

    assert await purge_usage() == 0
    assert len(await times(subjects.c.subject)) == 1


async def test_an_idle_subject_goes_by_the_databases_clock(skew: Skew) -> None:
    """Forty days behind on the host, a subject idle 36 days goes with its usage."""
    long_ago = await database_now() - timedelta(days=36)
    subject_id = uuid.uuid4()
    async with connections.engine().begin() as connection:
        await connection.execute(
            subjects.insert().values(
                id=subject_id,
                kind="holder",
                direction=Direction.OUT,
                currency="EGP",
                subject="h-idle",
                touched_at=long_ago,
            )
        )
        await connection.execute(
            usage.insert().values(
                id=uuid.uuid4(), subject_id=subject_id, amount=1, at=long_ago
            )
        )
    skew(-timedelta(days=40))

    assert await purge_usage() == 1
    assert await times(subjects.c.subject) == []
