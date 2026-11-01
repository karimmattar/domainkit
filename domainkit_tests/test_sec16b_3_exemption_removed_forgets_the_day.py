"""SEC-16b-3: removing a subject's exemption lets the default start from nothing.

An exemption is a stored limit disabled for one subject: it shadows the
default of its code (``applicable``), so while it stands the subject has
no window and, since PERF-26, keeps no usage. ``set_limit`` and
``change_limit`` run ``count_earlier`` when they enable a limit, so
lifting an exemption by enabling it counts the day's postings. Lifting
it by removing it (``remove_limit``, ``DELETE /limits/{id}``, ``manage.py
limits remove``: the documented "a default of the settings with its key
applies again") runs nothing: the default's window, now in force, sees
an empty day. The same holds for a subject-specific limit with only
``per_transaction`` that is removed.

So a customer exempted this morning (a VIP, a mistaken exemption caught
by a supervisor) who moved 900 keeps a whole fresh daily 1,000 once the
exemption is removed. SEC-15b-5's test names exactly this ("the same
holds after an exemption ... is lifted").

The test asserts the safe behaviour (the second 900 is refused) and
fails on ``675dfd6``.
"""

from datetime import UTC, datetime

import pytest

from domainkit.clock import freeze_time
from domainkit.conf import override_settings
from domainkit.contrib.accounting import AccountingUnitOfWork, post_entry
from domainkit.contrib.limits import (
    LimitExceeded,
    LimitsUnitOfWork,
    RemoveLimit,
    SetLimit,
    Window,
    remove_limit,
    set_limit,
)
from domainkit_tests.test_limits import (  # noqa: F401 - the fixture.
    books,
    egp,
    period,
    spend,
    wallets,
)

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("books")]

DAILY = {
    "DEFAULTS": [
        {
            "code": "wallet.daily",
            "kind": "holder",
            "currency": "EGP",
            "window": "day",
            "max_amount": "1000",
            "time_zone": "UTC",
        }
    ]
}


async def test_removing_an_exemption_counts_what_moved_under_it_today() -> None:
    """A default daily 1,000; h-1 exempted; 900 moves; exemption removed; 900."""
    with override_settings(LIMITS=DAILY):
        await period()
        (wallet,), (till,) = await wallets("h-1", 1)
        with freeze_time(datetime(2026, 10, 30, 9, tzinfo=UTC)):
            exemption = await set_limit(
                SetLimit(
                    code="wallet.daily",
                    kind="holder",
                    currency="EGP",
                    subject="h-1",
                    window=Window.DAY,
                    max_amount=egp("1000"),
                    time_zone="UTC",
                    enabled=False,
                ),
                LimitsUnitOfWork(),
            )
        with freeze_time(datetime(2026, 10, 30, 10, tzinfo=UTC)):
            await post_entry(
                spend(wallet, till, egp("900"), "p-1"), AccountingUnitOfWork()
            )
        with freeze_time(datetime(2026, 10, 30, 11, tzinfo=UTC)):
            await remove_limit(RemoveLimit(limit_id=exemption.id), LimitsUnitOfWork())
        with (
            freeze_time(datetime(2026, 10, 30, 12, tzinfo=UTC)),
            pytest.raises(LimitExceeded),
        ):
            await post_entry(
                spend(wallet, till, egp("900"), "p-2"), AccountingUnitOfWork()
            )
