"""What moved before a limit bounded it, counted from the ledger; approved reversals.

Only the subjects a window bounds keep their movements (PERF-26), so a
limit set, changed, or enabled partway through its window counts the
ledger's postings since the window began (``postings.count_earlier``,
SEC-15b-5): every entry not reversed, in the limit's currency and
direction, of its kind's subjects, once. A reversal booked while an
approved request runs is counted, and frees nothing (FIN-88).
"""

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Final

import pytest

from domainkit import setup
from domainkit.apps import registry
from domainkit.clock import freeze_time
from domainkit.conf import override_settings
from domainkit.contrib.accounting import (
    AccountingUnitOfWork,
    AccountType,
    Line,
    PostEntry,
    ReverseEntry,
    post_entry,
    reverse_entry,
)
from domainkit.contrib.approvals import ENTRIES, REVERSES
from domainkit.contrib.approvals.guards import running
from domainkit.contrib.limits import (
    ChangeLimit,
    Direction,
    Limit,
    LimitExceeded,
    LimitsUnitOfWork,
    SetLimit,
    Window,
    change_limit,
    set_limit,
    usage_of,
)
from domainkit.contrib.limits.postings import count_earlier
from domainkit.contrib.limits.services import _count_earlier
from domainkit.db import connections
from domainkit_tests.conftest import ALPHA, Migrated, Shape
from domainkit_tests.test_approvals import FAST
from domainkit_tests.test_limits import (  # noqa: F401 - the fixture.
    TODAY,
    account,
    books,
    egp,
    limit,
    period,
    refused,
    spend,
    wallets,
)

MORNING: Final = datetime(2026, 10, 30, 9, tzinfo=UTC)
ELEVEN: Final = datetime(2026, 10, 30, 11, tzinfo=UTC)
NOON: Final = datetime(2026, 10, 30, 12, tzinfo=UTC)
APPROVED: Final = Shape(
    "limits_approvals",
    "limits",
    {
        "INSTALLED_DOMAINS": [
            "domainkit.contrib.messaging",
            "domainkit.contrib.audit",
            "domainkit.contrib.accounting",
            "domainkit.contrib.limits",
            "domainkit.contrib.approvals",
            ALPHA,
        ],
        "MESSAGING": FAST,
        "APPROVALS": {"ENTRY_THRESHOLDS": {"EGP": "1000.00"}},
    },
)
"""The ledger, limits, and approvals of postings over 1,000 pounds."""

pytestmark = pytest.mark.anyio


async def used(kind: str, subject: str) -> tuple[object, int]:
    """What ``subject`` used of its one limit, and how many movements."""
    (each,) = await usage_of(kind, subject)
    return each.used, each.count


@pytest.mark.usefixtures("books")
async def test_a_limit_set_at_eleven_counts_every_holders_morning() -> None:
    """Each holder's payouts count; reversed ones, top-ups, other tills do not."""
    await period()
    (first,), (till,) = await wallets("h-1", 1)
    (second,), (other,) = await wallets("h-2", 1)
    with freeze_time(MORNING):
        await post_entry(spend(first, till, egp("300"), "p-1"), AccountingUnitOfWork())
        undone = await post_entry(
            spend(first, till, egp("200"), "p-2"), AccountingUnitOfWork()
        )
        await reverse_entry(
            ReverseEntry(entry_id=undone.id, booked_on=TODAY), AccountingUnitOfWork()
        )
        await post_entry(
            spend(second, other, egp("100"), "p-3"), AccountingUnitOfWork()
        )
        await post_entry(  # Money in: another direction.
            PostEntry(
                source="topups",
                reference="t-1",
                booked_on=TODAY,
                lines=(
                    Line.debit(till.id, egp("50")),
                    Line.credit(first.id, egp("50")),
                ),
            ),
            AccountingUnitOfWork(),
        )
    with freeze_time(ELEVEN):
        await limit(code="daily", window=Window.DAY, max_amount=egp("500"))
        await limit(code="daily", window=Window.DAY, max_amount=egp("500"))  # Again.

    with freeze_time(NOON):
        assert await used("holder", "h-1") == (egp("300"), 1)
        assert await used("holder", "h-2") == (egp("100"), 1)
        with refused(LimitExceeded):
            await post_entry(
                spend(first, till, egp("201"), "p-4"), AccountingUnitOfWork()
            )
        await post_entry(spend(first, till, egp("200"), "p-5"), AccountingUnitOfWork())
        with refused(LimitExceeded):
            await post_entry(
                spend(second, other, egp("401"), "p-6"), AccountingUnitOfWork()
            )


@pytest.mark.usefixtures("books")
async def test_an_accounts_rolling_limit_counts_its_own_postings_only() -> None:
    """A limit of one account, over the last 24 hours: that account's, back to 9:00."""
    await period()
    (first, second), (till, other) = await wallets("h-1", 2)
    with freeze_time(MORNING):
        await post_entry(spend(first, till, egp("70"), "p-1"), AccountingUnitOfWork())
        await post_entry(spend(second, other, egp("90"), "p-2"), AccountingUnitOfWork())
    with freeze_time(ELEVEN):
        await limit(
            code="rolling",
            kind="account",
            subject=str(first.id),
            window=Window.ROLLING_DAY,
            max_amount=egp("100"),
        )

    with freeze_time(NOON):
        assert await used("account", str(first.id)) == (egp("70"), 1)
        with refused(LimitExceeded):
            await post_entry(
                spend(first, till, egp("31"), "p-3"), AccountingUnitOfWork()
            )


@pytest.mark.usefixtures("books")
async def test_an_exemption_lifted_counts_what_the_holder_moved_meanwhile() -> None:
    """Disabled for h-1 at 9:00, enabled again at 11:00: the 9:00 payout counts."""
    await period()
    (wallet,), (till,) = await wallets("h-1", 1)
    await limit(code="daily", window=Window.DAY, max_amount=egp("100"))
    await limit(
        code="daily",
        subject="h-1",
        window=Window.DAY,
        max_amount=egp("100"),
        enabled=False,
    )
    with freeze_time(MORNING):
        await post_entry(spend(wallet, till, egp("90"), "p-1"), AccountingUnitOfWork())
    async with LimitsUnitOfWork() as uow:
        stored = await uow.limits.filter()
    exempt_id = next(each.id for each in stored if each.subject == "h-1")
    with freeze_time(ELEVEN):
        await change_limit(
            ChangeLimit(limit_id=exempt_id, window=Window.DAY, max_amount=egp("100")),
            LimitsUnitOfWork(),
        )

    with freeze_time(NOON), refused(LimitExceeded):
        await post_entry(spend(wallet, till, egp("11"), "p-2"), AccountingUnitOfWork())


@pytest.mark.usefixtures("books")
async def test_only_a_ledger_kind_with_a_window_enabled_is_counted_back() -> None:
    """An actor's, a limit without a window, a disabled one, an empty window: none."""
    await period()
    (wallet,), (till,) = await wallets("h-1", 1)
    with freeze_time(MORNING):
        await post_entry(spend(wallet, till, egp("90"), "p-1"), AccountingUnitOfWork())
    cases = [
        Limit.define(
            "a", "actor", "EGP", window=Window.DAY, max_amount=egp("1"), subject="x"
        ),
        Limit.define("b", "holder", "EGP", per_transaction=egp("1")),
        Limit.define(
            "c", "holder", "EGP", window=Window.DAY, max_count=1, enabled=False
        ),
        Limit.define(
            "d", "holder", "EGP", direction=Direction.IN, window=Window.DAY, max_count=1
        ),
    ]
    with freeze_time(NOON):
        async with LimitsUnitOfWork() as uow:
            counted = [await count_earlier(uow.session, each) for each in cases]

    assert counted == [0, 0, 0, 0]


@pytest.fixture
def limits_alone() -> Iterator[None]:
    """Limits without the ledger, set up without a database."""
    registry.clear()
    with override_settings(INSTALLED_DOMAINS=["domainkit.contrib.limits", ALPHA]):
        setup()
        yield
    registry.clear()


async def test_without_the_ledger_nothing_is_counted_back(limits_alone: None) -> None:
    """No postings to read: a limit counts from when it is set."""
    daily = Limit.define("d", "holder", "EGP", window=Window.DAY, max_count=1)

    assert await _count_earlier(LimitsUnitOfWork(), daily) is None


@pytest.fixture
async def approving(migrated: Migrated) -> AsyncIterator[str]:
    """The ledger with limits and approvals, migrated."""
    with migrated(APPROVED) as url:
        yield url
        await connections.close_all()


@pytest.mark.usefixtures("approving")
async def test_an_approved_reversal_counts_and_frees_nothing() -> None:
    """A payout two people approved, then reversed on approval: the limit stays used.

    Released, the 2,000 would let the holder draw it again within the day
    (FIN-88); counted, the next 600 is refused.
    """
    await period()
    till = await account("till")
    wallet = await account("wallet", AccountType.LIABILITY, holder="h-1")
    await set_limit(
        SetLimit(
            code="daily",
            kind="holder",
            currency="EGP",
            window=Window.DAY,
            max_amount=egp("2500"),
        ),
        LimitsUnitOfWork(),
    )
    fund = PostEntry(
        source="topups",
        reference="f-1",
        booked_on=TODAY,
        lines=(Line.debit(till.id, egp("5000")), Line.credit(wallet.id, egp("5000"))),
    )
    payout = spend(wallet, till, egp("2000"), "p-1")
    with freeze_time(MORNING):
        with running(uuid.uuid4(), ENTRIES, fund, "maker", 2):
            await post_entry(fund, AccountingUnitOfWork())
        with running(uuid.uuid4(), ENTRIES, payout, "maker", 2):
            paid = await post_entry(payout, AccountingUnitOfWork())
        undo = ReverseEntry(entry_id=paid.id, booked_on=TODAY)
        with running(uuid.uuid4(), REVERSES, undo, "maker", 2):
            await reverse_entry(undo, AccountingUnitOfWork())

    with freeze_time(NOON):
        assert await used("holder", "h-1") == (egp("2000"), 1)
        with refused(LimitExceeded):
            await post_entry(
                spend(wallet, till, egp("600"), "p-2"), AccountingUnitOfWork()
            )
