"""SEC-14b-1: anyone who may post an entry can take the ledger's own sources.

``reverse_entry`` and ``capture_hold`` book under ``accounting.reversal``
and ``accounting.hold`` with the reversed entry's or the hold's id as the
reference. ``post_entry`` (``POST /entries``, ``defer_entry``) accepts
those sources from any caller, so a caller with ``add_entry`` alone can
post first under them:

- ``reverse_entry`` then returns the squatter's entry as "the reversal"
  (``same=lambda posted: True``), and the entry is never reversed;
- ``capture_hold`` posts its entry without the entry lock or a look-up and
  dies on the unique constraint: the hold can never be captured.

Each test asserts the safe behaviour and fails on ``ac71361``. Now
``post_entry`` refuses the ``accounting.`` sources; a reversal is the
entry that reverses the one asked, and a capture takes the entry's lock
and looks first, so an entry posted under their reference before the fix
is refused (``reference_reused``), never returned or crashed into.
"""

from decimal import Decimal

import pytest

from domainkit.contrib.accounting import (
    CAPTURES,
    REVERSALS,
    AccountingUnitOfWork,
    AccountType,
    CaptureHold,
    Entry,
    HoldStatus,
    InvalidEntry,
    Line,
    PlaceHold,
    PostEntry,
    ReferenceReused,
    ReleaseHold,
    ReverseEntry,
    capture_hold,
    place_hold,
    post_entry,
    release_hold,
    reverse_entry,
)
from domainkit.contrib.accounting.ledger import post
from domainkit.contrib.accounting.tables import entries
from domainkit.ddd import DomainError
from domainkit_tests.test_accounting import (  # noqa: F401 - the fixture.
    TODAY,
    account,
    balances,
    books,
    count,
    period,
    refused,
    transfer,
    usd,
)

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("books")]


async def _squat(source: str, reference: str) -> DomainError | None:
    """Post a one-cent entry between two unrelated accounts under ``source``."""
    a, b = await account("9001"), await account("9002")
    try:
        await post_entry(
            PostEntry(
                source=source,
                reference=reference,
                booked_on=TODAY,
                lines=(Line.debit(a.id, usd("0.01")), Line.credit(b.id, usd("0.01"))),
            ),
            AccountingUnitOfWork(),
        )
    except DomainError as refused:  # The safe behaviour: the source is reserved.
        return refused
    return None


async def test_a_squatted_reversal_source_does_not_fake_a_reversal() -> None:
    """``reverse_entry`` must reverse the entry, or refuse; never return another one."""
    await period()
    cash, sales = await account("1000"), await account("4000", type=AccountType.INCOME)
    entry = await post_entry(
        transfer(sales, cash, usd("100"), "sale-1"), AccountingUnitOfWork()
    )

    await _squat(REVERSALS, str(entry.id))
    reversal = await reverse_entry(
        ReverseEntry(entry_id=entry.id), AccountingUnitOfWork()
    )

    assert reversal.reverses == entry.id, (
        "reverse_entry returned an entry that does not reverse the one asked"
    )
    assert (await balances())[cash.id] == Decimal(0)


async def test_a_squatted_capture_source_does_not_block_the_capture() -> None:
    """A hold stays capturable whatever entries others post."""
    await period()
    wallet = await account("2100", type=AccountType.LIABILITY, may_go_negative=False)
    cash = await account("1000")
    merchant = await account("2200", type=AccountType.LIABILITY)
    await post_entry(
        PostEntry(
            source="topups",
            reference="t1",
            booked_on=TODAY,
            lines=(Line.debit(cash.id, usd("50")), Line.credit(wallet.id, usd("50"))),
        ),
        AccountingUnitOfWork(),
    )
    hold = await place_hold(
        PlaceHold(
            account_id=wallet.id, amount=usd("20"), source="cards", reference="a"
        ),
        AccountingUnitOfWork(),
    )

    await _squat(CAPTURES, str(hold.id))
    captured = await capture_hold(
        CaptureHold(hold_id=hold.id, counter_account_id=merchant.id),
        AccountingUnitOfWork(),
    )

    assert captured.status is HoldStatus.CAPTURED
    assert (await balances())[merchant.id] == Decimal("20.0000")


@pytest.mark.parametrize("source", [REVERSALS, CAPTURES, "accounting.", "accounting.x"])
async def test_the_ledgers_own_sources_are_refused(source: str) -> None:
    """Every source under ``accounting.``, before any lock or row."""
    await period()
    a, b = await account("9001"), await account("9002")

    def entry(source: str) -> PostEntry:
        return PostEntry(
            source=source,
            reference="r",
            booked_on=TODAY,
            lines=(Line.debit(a.id, usd("1")), Line.credit(b.id, usd("1"))),
        )

    with refused(InvalidEntry, match="are the ledger's own"):
        await post_entry(entry(source), AccountingUnitOfWork())
    assert await count(entries) == 0
    posted = await post_entry(entry("accountingx"), AccountingUnitOfWork())
    assert posted.source == "accountingx"


async def _posted_before_the_fix(source: str, reference: str) -> None:
    """Post under ``source`` as ``post_entry`` let any caller until 0.14."""
    a, b = await account("9001"), await account("9002")
    entry = Entry.draft(
        source,
        reference,
        TODAY,
        (Line.debit(a.id, usd("0.01")), Line.credit(b.id, usd("0.01"))),
        max_lines=2,
    )

    async def draft() -> Entry:
        return entry

    uow = AccountingUnitOfWork()
    await post(uow, source, reference, draft, same=lambda posted: False)


async def test_a_reversal_reference_taken_before_is_refused() -> None:
    """Not returned as the reversal: ``reference_reused``, and nothing moves."""
    await period()
    cash, sales = await account("1000"), await account("4000", type=AccountType.INCOME)
    entry = await post_entry(
        transfer(sales, cash, usd("100"), "sale-1"), AccountingUnitOfWork()
    )
    await _posted_before_the_fix(REVERSALS, str(entry.id))

    with refused(ReferenceReused):
        await reverse_entry(ReverseEntry(entry_id=entry.id), AccountingUnitOfWork())
    assert (await balances())[cash.id] == Decimal("100.0000")


async def test_a_capture_reference_taken_before_is_refused() -> None:
    """Refused as ``reference_reused`` (not a unique violation); the hold stays."""
    await period()
    wallet = await account("2100", type=AccountType.LIABILITY)
    merchant = await account("2200", type=AccountType.LIABILITY)
    hold = await place_hold(
        PlaceHold(account_id=wallet.id, amount=usd("5"), source="cards", reference="a"),
        AccountingUnitOfWork(),
    )
    await _posted_before_the_fix(CAPTURES, str(hold.id))

    with refused(ReferenceReused):
        await capture_hold(
            CaptureHold(hold_id=hold.id, counter_account_id=merchant.id),
            AccountingUnitOfWork(),
        )
    released = await release_hold(ReleaseHold(hold_id=hold.id), AccountingUnitOfWork())
    assert released.status is HoldStatus.RELEASED
    assert (await balances())[merchant.id] == Decimal(0)
