"""Counting movements in a use case's own unit of work: windows, keys, defaults.

On SQLite and, with ``DOMAINKIT_TEST_POSTGRES_URL``, PostgreSQL; time is
the frozen clock's on both, as the database's clock gives way to it.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final

import pytest
from sqlalchemy import func, select

from domainkit.clock import freeze_time
from domainkit.conf import override_settings
from domainkit.contrib.limits import (
    Direction,
    LimitExceeded,
    LimitsUnitOfWork,
    Movement,
    SetLimit,
    TransactionLimitExceeded,
    Usage,
    Window,
    check,
    consume,
    purge_usage,
    release,
    set_limit,
    usage_of,
)
from domainkit.contrib.limits.tables import subjects, usage
from domainkit.contrib.limits.tasks import purge_old_usage
from domainkit.db import SqlAlchemyUnitOfWork, connections
from domainkit.ddd import ReadOnlyError
from domainkit.exceptions import ImproperlyConfigured
from domainkit.money import Money
from domainkit_tests.test_limits import (
    books,  # noqa: F401 - the fixture.
    egp,
    refused,
)

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("books")]

NOON: Final = datetime(2026, 10, 30, 12, tzinfo=UTC)


async def limit(**fields: object) -> None:
    """Set the limit ``fields`` describe: of holders, in pounds, by default."""
    await set_limit(
        SetLimit(**{"kind": "holder", "currency": "EGP", **fields}),  # type: ignore[arg-type]
        LimitsUnitOfWork(),
    )


def paid(amount: str, holder: str = "h-1", **subjects: str) -> Movement:
    """``amount`` pounds out, of ``holder`` and ``subjects``."""
    return Movement(egp(amount), {"holder": holder, **subjects})


async def spend(*movements: Movement, key: str | None = None) -> None:
    """Count ``movements`` in a unit of work of their own, and commit."""
    async with LimitsUnitOfWork() as uow:
        await consume(uow, *movements, key=key)
        await uow.commit()


async def used(holder: str = "h-1", **options: object) -> list[tuple[object, ...]]:
    """``(code, used, count)`` of each limit of ``holder``."""
    return [
        (each.code, each.used, each.count)
        for each in await usage_of("holder", holder, **options)  # type: ignore[arg-type]
    ]


async def test_a_daily_window_starts_again_at_midnight_in_its_zone() -> None:
    """Cairo's day: 21:59 UTC is still the 30th there, 22:00 is the 31st."""
    await limit(
        code="daily", window=Window.DAY, max_amount=egp("100"), time_zone="Africa/Cairo"
    )
    with freeze_time(datetime(2026, 10, 30, 21, 59, tzinfo=UTC)) as clock:
        await spend(paid("100"))
        with refused(LimitExceeded):
            await spend(paid("0.01"))
        clock.move_to(datetime(2026, 10, 30, 22, tzinfo=UTC))
        await spend(paid("100"))
        assert await used() == [("daily", egp("100"), 1)]


async def test_a_rolling_window_reaches_back_24_hours() -> None:
    """What was counted 24 hours ago leaves the window; what was since stays."""
    await limit(code="rolling", window=Window.ROLLING_DAY, max_amount=egp("100"))
    with freeze_time(NOON) as clock:
        await spend(paid("60"))
        clock.move_to(NOON + timedelta(hours=12))
        await spend(paid("40"))
        with refused(LimitExceeded):
            await spend(paid("1"))
        clock.move_to(NOON + timedelta(hours=24))
        await spend(paid("60"))
        assert await used() == [("rolling", egp("100"), 2)]


async def test_a_month_and_a_count_bound_together() -> None:
    """Each limit of a subject is judged in its own window; the first broken refuses."""
    await limit(code="monthly", window=Window.MONTH, max_amount=egp("1000"))
    await limit(code="thrice", window=Window.ROLLING_MONTH, max_count=3)
    with freeze_time(datetime(2026, 10, 2, tzinfo=UTC)) as clock:
        await spend(paid("400"))
        clock.move_to(datetime(2026, 10, 20, tzinfo=UTC))
        await spend(paid("400"), paid("100"))  # Two movements: two of the three.
        with refused(LimitExceeded, "as many of these") as raised:
            await spend(paid("1"))
        assert (raised.value.limit, raised.value.window) == ("thrice", "rolling_month")
        # The 30 days from November 2 leave out October 2: one more fits.
        clock.move_to(datetime(2026, 11, 2, tzinfo=UTC))
        await spend(paid("1"))
        with refused(LimitExceeded, "as many of these"):
            await spend(paid("1"))
        # November's own month: 1 so far, and 900 more fit, 100 after them not.
        clock.move_to(datetime(2026, 11, 20, tzinfo=UTC))
        await spend(paid("900"))
        with refused(LimitExceeded, "more than a limit allows") as raised:
            await spend(paid("100"))
        assert raised.value.limit == "monthly"
        assert await used() == [
            ("monthly", egp("901"), 2),
            ("thrice", egp("901"), 2),
        ]


async def test_per_transaction_bounds_each_movement_and_needs_no_window() -> None:
    """Over the most one may be, refused; at it, taken, and counted all the same."""
    await limit(code="each", per_transaction=egp("50"))
    with refused(TransactionLimitExceeded):
        await spend(paid("50.01"))
    await spend(paid("50"), paid("50"))

    (each,) = await usage_of("holder", "h-1")
    assert (each.window, each.since, each.used, each.count, each.remaining) == (
        None,
        None,
        egp("0"),
        0,
        None,
    )
    async with connections.engine().connect() as connection:
        assert await connection.scalar(select(func.count()).select_from(usage)) == 2


async def test_only_the_subjects_limits_in_its_currency_and_direction_count() -> None:
    """Another holder, currency, or direction is bounded by its own limits only."""
    await limit(code="daily", window=Window.DAY, max_amount=egp("100"))
    with freeze_time(NOON):
        await spend(paid("100"))
        await spend(paid("100", holder="h-2"))
        await spend(Movement(Money.of("500", "USD"), {"holder": "h-1"}))
        await spend(Movement(egp("500"), {"holder": "h-1"}, Direction.IN))
        await spend(Movement(egp("500"), {"merchant": "m-1"}))
        with refused(LimitExceeded):
            await spend(paid("1"))

        assert await used(direction=Direction.OUT, currency="EGP") == [
            ("daily", egp("100"), 1)
        ]
        assert await used(currency="USD") == []
        assert await used("h-2") == [("daily", egp("100"), 1)]


async def test_a_limit_for_one_subject_replaces_the_one_for_every_subject() -> None:
    """Raised for h-1; disabled for h-2, which is then exempt; others as before."""
    await limit(code="daily", window=Window.DAY, max_amount=egp("100"))
    await limit(code="daily", subject="h-1", window=Window.DAY, max_amount=egp("500"))
    await limit(
        code="daily",
        subject="h-2",
        window=Window.DAY,
        max_amount=egp("1"),
        enabled=False,
    )
    with freeze_time(NOON):
        await spend(paid("500"))
        await spend(paid("10000", holder="h-2"))
        with refused(LimitExceeded):
            await spend(paid("101", holder="h-3"))

        ((code, amount, count),) = await used()
        assert (code, amount, count) == ("daily", egp("500"), 1)
        assert await used("h-2") == []
        (mine,) = await usage_of("holder", "h-1")
    assert (mine.subject, mine.max_amount, mine.remaining) == (
        "h-1",
        egp("500"),
        egp("0"),
    )


async def test_settings_defaults_apply_until_a_stored_limit_replaces_them() -> None:
    """``LIMITS.DEFAULTS``: in force without a row; one of the same key wins."""
    defaults = {
        "DEFAULTS": [
            {
                "code": "daily",
                "kind": "holder",
                "currency": "EGP",
                "window": "day",
                "max_amount": "100",
            },
            {
                "code": "each",
                "kind": "holder",
                "currency": "USD",
                "per_transaction": "5",
            },
        ]
    }
    with override_settings(LIMITS=defaults), freeze_time(NOON):
        await spend(paid("100"))
        with refused(LimitExceeded):
            await spend(paid("1"))
        with refused(TransactionLimitExceeded):
            await spend(Movement(Money.of("6", "USD"), {"holder": "h-1"}))
        (default,) = await usage_of("holder", "h-1", currency="EGP")
        assert (default.stored, default.used) == (False, egp("100"))

        await limit(code="daily", window=Window.DAY, max_amount=egp("200"))
        await spend(paid("100"))
        (stored,) = await usage_of("holder", "h-1", currency="EGP")
        assert (stored.stored, stored.used, stored.max_amount) == (
            True,
            egp("200"),
            egp("200"),
        )


async def test_check_counts_nothing_and_says_what_consume_would() -> None:
    """A check refuses as the count would; passing, it leaves no trace."""
    await limit(code="daily", window=Window.DAY, max_amount=egp("100"))
    with freeze_time(NOON):
        async with LimitsUnitOfWork(readonly=True) as uow:
            await check(uow, paid("100"))
            await check(uow, Movement(egp("1000"), {"merchant": "m-1"}))
            with refused(LimitExceeded):
                await check(uow, paid("101"))
        await spend(paid("60"))
        async with LimitsUnitOfWork(readonly=True) as uow:
            await check(uow, paid("40"))
            with refused(LimitExceeded):
                await check(uow, paid("41"))
            # An account without limits is judged first: the holder still is.
            with refused(LimitExceeded):
                await check(uow, paid("41", account="a-1"))
        assert await used() == [("daily", egp("60"), 1)]


async def test_a_check_of_a_subject_that_never_moved_counts_nothing_used() -> None:
    """A count of one fits a subject with no row yet."""
    await limit(code="once", window=Window.DAY, max_count=1)
    async with LimitsUnitOfWork(readonly=True) as uow:
        await check(uow, paid("1", holder="new"))


async def test_subjects_of_one_name_are_apart_by_kind_direction_and_currency() -> None:
    """One subject string, four rows: each counts its own movements."""
    await limit(code="daily", window=Window.DAY, max_amount=egp("100"))
    await limit(code="acct", kind="account", window=Window.DAY, max_count=9)
    await limit(code="in", direction=Direction.IN, window=Window.DAY, max_count=9)
    await limit(code="usd", currency="USD", window=Window.DAY, max_count=9)
    with freeze_time(NOON):
        await spend(Movement(egp("10"), {"holder": "x", "account": "x"}))
        await spend(Movement(egp("20"), {"holder": "x"}, Direction.IN))
        await spend(Movement(Money.of("30", "USD"), {"holder": "x"}))
        await spend(Movement(egp("40"), {"holder": "x"}))
        async with LimitsUnitOfWork(readonly=True) as uow:
            await check(uow, Movement(egp("50"), {"holder": "x"}))
        assert await used("x", direction=Direction.OUT, currency="EGP") == [
            ("daily", egp("50"), 2)
        ]


async def test_a_limit_is_set_by_its_whole_key() -> None:
    """Limits of one code differing in one part of the key are set apart."""
    keys: list[dict[str, object]] = [
        {"per_transaction": egp("1")},
        {"per_transaction": egp("1"), "kind": "account"},
        {"per_transaction": egp("1"), "direction": Direction.IN},
        {"per_transaction": Money.of("1", "USD"), "currency": "USD"},
    ]
    for key in keys:
        await limit(code="k", **key)
    await limit(code="k", per_transaction=egp("9"))

    async with LimitsUnitOfWork(readonly=True) as uow:
        found = await uow.limits.filter()
    bounds = sorted(str(each.per_transaction) for each in found)
    assert bounds == ["1.00 EGP", "1.00 EGP", "1.00 USD", "9.00 EGP"]


async def test_release_gives_back_what_its_key_counted_once() -> None:
    """A refund frees its amount and its count; releasing again releases nothing."""
    await limit(code="daily", window=Window.DAY, max_amount=egp("100"), max_count=2)
    with freeze_time(NOON):
        await spend(paid("70"), key="payments:p-1")
        await spend(paid("30"))
        async with LimitsUnitOfWork() as uow:
            assert await release(uow, "payments:p-1") == 1
            assert await release(uow, "payments:p-1") == 0
            await uow.commit()
        assert await used() == [("daily", egp("30"), 1)]
        await spend(paid("70"))


async def test_a_count_rolled_back_with_its_use_case_is_not_kept() -> None:
    """Counted, then the use case fails: nothing stays counted."""
    await limit(code="daily", window=Window.DAY, max_amount=egp("100"))

    async def declined() -> None:
        async with LimitsUnitOfWork() as uow:
            await consume(uow, paid("100"))
            raise RuntimeError("the payment provider said no")

    with freeze_time(NOON):
        with pytest.raises(RuntimeError):
            await declined()
        await spend(paid("100"))
        assert await used() == [("daily", egp("100"), 1)]


async def test_old_usage_is_purged_and_idle_subjects_with_it() -> None:
    """Past ``KEEP_USAGE`` (35 days), usage goes; a subject with none left, too."""
    await limit(code="monthly", window=Window.MONTH, max_amount=egp("1000"))
    with freeze_time(NOON) as clock:
        await spend(paid("10", holder="h-old"))
        clock.move_to(NOON + timedelta(days=20))
        await spend(paid("10"))
        clock.move_to(NOON + timedelta(days=35))  # Kept exactly that long.
        assert await purge_usage() == 0
        clock.move_to(NOON + timedelta(days=36))
        assert await purge_usage() == 1
        await purge_old_usage()  # The periodic task: nothing more to purge.
        assert await purge_usage() == 0
    async with connections.engine().connect() as connection:
        kept = await connection.scalars(select(subjects.c.subject))
        assert list(kept) == ["h-1"]


async def test_purge_deletes_in_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    """More than one batch: every old row goes."""
    monkeypatch.setattr("domainkit.contrib.limits.usage.PURGE_BATCH", 2)
    await limit(code="monthly", window=Window.MONTH, max_amount=egp("1000"))
    with freeze_time(NOON) as clock:
        await spend(*(paid("1") for _ in range(5)))
        clock.move_to(NOON + timedelta(days=36))
        assert await purge_usage() == 5


async def test_usage_names_its_window_and_what_is_left() -> None:
    """Since when, what was used, and what remains."""
    await limit(
        code="daily",
        window=Window.DAY,
        max_amount=egp("100"),
        max_count=5,
        per_transaction=egp("80"),
    )
    with freeze_time(NOON):
        await spend(paid("30.50"))
        (found,) = await usage_of("holder", "h-1")
    assert found == Usage(
        code="daily",
        kind="holder",
        direction=Direction.OUT,
        subject="",
        window=Window.DAY,
        since=datetime(2026, 10, 30, tzinfo=UTC),
        used=egp("30.50"),
        count=1,
        per_transaction=egp("80"),
        max_amount=egp("100"),
        max_count=5,
        stored=True,
    )
    assert found.remaining == egp("69.50")
    assert found.used.amount == Decimal("30.50")


async def test_counting_needs_an_open_unit_of_work_that_writes_on_their_database() -> (
    None
):
    """Read-only, unopened, elsewhere, or nothing to count: refused with a reason."""
    with pytest.raises(ReadOnlyError) as readonly:
        async with LimitsUnitOfWork(readonly=True) as uow:
            await release(uow, "k")
    with pytest.raises(RuntimeError) as closed:
        await consume(LimitsUnitOfWork(), paid("1"))
    with pytest.raises(ValueError, match=r"^Give at least one movement\.$"):
        async with LimitsUnitOfWork() as uow:
            await consume(uow)
    assert str(readonly.value) == (
        "LimitsUnitOfWork is read-only; movements are counted in one that writes."
    )
    assert str(closed.value) == (
        "Count movements inside the unit of work: async with uow."
    )
    with pytest.raises(TypeError, match="Expected a Movement, not Money"):
        async with LimitsUnitOfWork() as uow:
            await consume(uow, egp("1"))  # type: ignore[arg-type]

    class Elsewhere(SqlAlchemyUnitOfWork):
        database = "other"

        def create_repositories(self, session: object) -> None:
            """None."""

    url = str(connections.engine().url.render_as_string(hide_password=False))
    with override_settings(
        DATABASES={"default": {"URL": url}, "other": {"URL": "sqlite+aiosqlite://"}}
    ):
        with pytest.raises(ImproperlyConfigured) as elsewhere_refused:
            async with Elsewhere() as elsewhere:
                await consume(elsewhere, paid("1"))
        await connections.close_all()
    assert str(elsewhere_refused.value) == (
        "Limits live in 'default' (DATABASE_ROUTING['limits']), and the unit of "
        "work in 'other': count movements in a unit of work on the limits' "
        "database."
    )


async def test_a_limit_set_partway_through_a_window_counts_what_moved_before() -> None:
    """Every movement is counted: a limit made later sees the morning's (SEC-15b-5)."""
    with freeze_time(datetime(2026, 10, 30, 9, tzinfo=UTC)):
        await spend(paid("60"))
        await spend(paid("10", holder="h-2"))
    await limit(code="daily", window=Window.DAY, max_amount=egp("100"))
    with freeze_time(NOON):
        with refused(LimitExceeded):
            await spend(paid("41"))
        await spend(paid("40"))
        assert await used() == [("daily", egp("100"), 2)]


async def test_lifting_an_exemption_counts_what_the_subject_moved_meanwhile() -> None:
    """Exempt, then not: the exempt movements of the window count again."""
    await limit(code="daily", window=Window.DAY, max_amount=egp("100"))
    await limit(
        code="daily",
        subject="h-1",
        window=Window.DAY,
        max_amount=egp("100"),
        enabled=False,
    )
    with freeze_time(datetime(2026, 10, 30, 9, tzinfo=UTC)):
        await spend(paid("90"))
    await limit(code="daily", subject="h-1", window=Window.DAY, max_amount=egp("100"))
    with freeze_time(NOON), refused(LimitExceeded):
        await spend(paid("11"))


async def test_a_limit_that_gains_a_window_counts_the_windows_movements() -> None:
    """Per transaction alone, then also a day: the day's movements count."""
    await limit(code="each", per_transaction=egp("80"))
    with freeze_time(datetime(2026, 10, 30, 9, tzinfo=UTC)):
        await spend(paid("80"))
    await limit(code="each", per_transaction=egp("80"), window=Window.DAY, max_count=1)
    with freeze_time(NOON), refused(LimitExceeded):
        await spend(paid("1"))
