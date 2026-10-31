"""The ledger's use cases: open and close accounts and periods, post and reverse.

Each is a command run by a service, ``service(command, uow)``, as a
``ResourceRouter`` runs them; call them the same way from code::

    entry = await post_entry(
        PostEntry(
            source="payments",
            reference=payment_id,
            booked_on=today,
            lines=(Line.debit(cash, amount), Line.credit(sales, amount)),
        ),
        AccountingUnitOfWork(),
    )

A posting is idempotent: ``source`` and ``reference`` name one entry, so
the same command posted again (a retried request, a message delivered
twice) returns the entry it posted the first time; a different entry under
them is refused (``ReferenceReused``). Each runs again, whole, when its
transaction lost a race (``ACCOUNTING.RETRY_ATTEMPTS``).
"""

from collections.abc import Awaitable, Callable, Iterable, Sequence
from datetime import date
from uuid import UUID

from domainkit import clock
from domainkit.apps import registry
from domainkit.ddd import Command, Retry

from .exceptions import (
    InvalidEntry,
    NoOpenPeriod,
    PeriodsOverlap,
    ReferenceReused,
    ReversalNotReversible,
)
from .hooks import on_booking, on_posting
from .models import (
    Account,
    AccountId,
    AccountType,
    Entry,
    EntryId,
    Line,
    Period,
    PeriodId,
)
from .settings import AccountingSettings
from .uow import AccountingUnitOfWork

RESERVED = "accounting."
"""The prefix of the ledger's own sources, which ``post_entry`` refuses."""
REVERSALS = "accounting.reversal"
"""The source of reversing entries; each one's reference is the reversed entry's id."""


class OpenAccount(Command):
    """Open an account of the chart."""

    code: str
    name: str
    type: AccountType
    currency: str
    holder: str | None = None
    may_go_negative: bool = True


class CloseAccount(Command):
    """Close an account with nothing on it."""

    account_id: UUID


class PostEntry(Command):
    """Post a journal entry, once per ``source`` and ``reference``."""

    source: str
    reference: str
    booked_on: date
    lines: Sequence[Line]
    description: str = ""


class ReverseEntry(Command):
    """Post the entry that undoes another, dated ``booked_on`` (default: today)."""

    entry_id: UUID
    booked_on: date | None = None
    description: str = ""


class OpenPeriod(Command):
    """Open an accounting period from ``starts_on`` to ``ends_on``, both included."""

    name: str
    starts_on: date
    ends_on: date


class ClosePeriod(Command):
    """Close a period: no entry may be dated in it afterwards."""

    period_id: UUID


def config() -> AccountingSettings:
    """``ACCOUNTING``'s settings."""
    return registry.settings(AccountingSettings)


async def run[T](uow: AccountingUnitOfWork, work: Callable[[], Awaitable[T]]) -> T:
    """Run ``work`` in ``uow``, again when its transaction lost a race."""
    return await uow.run(work, retry=Retry(attempts=config().RETRY_ATTEMPTS))


async def open_account(command: OpenAccount, uow: AccountingUnitOfWork) -> Account:
    """Open an account; another with the same code is ``account_code_taken``."""
    account = Account.open(
        command.code,
        command.name,
        command.type,
        command.currency,
        holder=command.holder,
        may_go_negative=command.may_go_negative,
    )
    async with uow:
        await uow.accounts.add(account)
        await uow.commit()
    return account


async def close_account(command: CloseAccount, uow: AccountingUnitOfWork) -> Account:
    """Close an account whose balance is zero and that holds nothing."""

    async def work() -> Account:
        async with uow:
            account = await uow.accounts.get_for_update(AccountId(command.account_id))
            account.close()
            await uow.commit()
        return account

    return await run(uow, work)


async def lock_accounts(
    uow: AccountingUnitOfWork, ids: Iterable[AccountId]
) -> dict[AccountId, Account]:
    """The accounts ``ids``, locked in ascending order of their ids."""
    return await uow.accounts.get_many_for_update(sorted(set(ids)))


async def book(
    uow: AccountingUnitOfWork, entry: Entry, accounts: dict[AccountId, Account]
) -> None:
    """Add ``entry`` to the open ``uow``, moving the ``accounts`` it posts to.

    The accounts are locked already (after any hold); the period the
    entry's date falls in is locked last, for share. Then the apps that
    registered ``on_booking`` see the entry, and may refuse it.

    Raises:
        NoOpenPeriod: No period covers the date, and ``REQUIRE_PERIOD``.
        PeriodIsClosed: The period that covers it is closed.
        AccountIsClosed, InvalidEntry, InsufficientFunds: As ``Account.post``.
        DomainError: An ``on_booking`` hook refused the entry.
    """
    period = await uow.periods.covering(entry.booked_on)
    if period is not None:
        period.takes()
        entry.period_id = period.id
    elif config().REQUIRE_PERIOD:
        raise NoOpenPeriod("No open accounting period covers the entry's date.")
    for posting in entry.postings:
        accounts[posting.account_id].post(posting.side, posting.money)
    for hook in on_booking.hooks():
        await hook.call(uow, entry, accounts)
    await uow.entries.add(entry)


async def claim(
    uow: AccountingUnitOfWork,
    source: str,
    reference: str,
    *,
    same: Callable[[Entry], bool],
) -> Entry | None:
    """Lock ``source`` and ``reference`` in the open ``uow``; the entry under them.

    None when nothing was; the entry if ``same`` says it books the same.

    Raises:
        ReferenceReused: Another entry was posted under that reference.
    """
    await uow.lock(f"accounting.entry:{source}:{reference}")
    posted = await uow.entries.by_reference(source, reference)
    if posted is not None and not same(posted):
        raise ReferenceReused(
            "Another entry was posted under this source and reference."
        )
    return posted


async def post(
    uow: AccountingUnitOfWork,
    source: str,
    reference: str,
    draft: Callable[[], Awaitable[Entry]],
    *,
    same: Callable[[Entry], bool],
) -> Entry:
    """Post the entry ``draft`` makes, once per ``source`` and ``reference``.

    Under a lock of the two, in a transaction of ``uow``: an entry posted
    under them already is returned if ``same`` says it books the same, and
    refused otherwise (``claim``). ``draft`` runs in the transaction, so it
    may read; then the entry's accounts are locked, in the order of their ids.

    Raises:
        ReferenceReused: Another entry was posted under that reference.
    """
    async with uow:
        posted = await claim(uow, source, reference, same=same)
        if posted is not None:
            return posted
        entry = await draft()
        accounts = await lock_accounts(
            uow, (posting.account_id for posting in entry.postings)
        )
        await book(uow, entry, accounts)
        await uow.commit()
    return entry


async def post_entry(command: PostEntry, uow: AccountingUnitOfWork) -> Entry:
    """Post a balanced entry, or return the one posted under its reference before.

    The sources that start with ``RESERVED`` (``accounting.``) are the
    ledger's own: ``reverse_entry`` and ``capture_hold`` post under them.

    Raises:
        InvalidEntry, InvalidAmount, UnbalancedEntry: As ``Entry.draft``, or
            the source is one of the ledger's own.
        ReferenceReused: Another entry was posted under that reference.
        AccountNotFound: A line names no account.
        NoOpenPeriod, PeriodIsClosed: The date is in no open period.
        AccountIsClosed, InsufficientFunds: As ``Account.post``.
        DomainError: An ``on_posting`` hook refused it (``approval_required``).
    """
    lines = tuple(command.lines)
    max_lines = config().MAX_LINES

    def entry() -> Entry:
        return Entry.draft(
            command.source,
            command.reference,
            command.booked_on,
            lines,
            description=command.description,
            max_lines=max_lines,
        )

    async def draft() -> Entry:
        return entry()

    checked = entry()  # Refuse a malformed entry before taking any lock.
    if checked.source.startswith(RESERVED):
        raise InvalidEntry(
            "The sources that start with 'accounting.' are the ledger's own."
        )
    for hook in on_posting.hooks():
        await hook.call(command)
    return await run(
        uow,
        lambda: post(
            uow,
            checked.source,
            checked.reference,
            draft,
            same=lambda posted: posted.fingerprint == checked.fingerprint,
        ),
    )


async def reverse_entry(command: ReverseEntry, uow: AccountingUnitOfWork) -> Entry:
    """Post the reversal of an entry: each of its postings on the other side.

    Its source is ``REVERSALS`` and its reference the entry's id, so an
    entry is reversed once: reversing it again returns that reversal. A
    reversal is not reversed: that would book the entry it undid again,
    past every check a new entry takes (SEC-15b-3).

    Raises:
        EntryNotFound: No entry has that id.
        ReversalNotReversible: The entry is a reversal.
        ReferenceReused: An entry that does not reverse it was posted under
            its reference (only before ``post_entry`` refused the source).
        NoOpenPeriod, PeriodIsClosed: The date is in no open period.
        AccountIsClosed, InsufficientFunds: As ``Account.post``.
    """
    entry_id = EntryId(command.entry_id)
    booked_on = command.booked_on or clock.now().date()

    async def draft() -> Entry:
        original = await uow.entries.get(entry_id)
        if original.reverses is not None:
            raise ReversalNotReversible(
                "A reversal is not reversed: post the entry again instead."
            )
        lines = original.reversal_lines()
        return Entry.draft(
            REVERSALS,
            str(entry_id),
            booked_on,
            lines,
            description=command.description,
            reverses=entry_id,
            max_lines=len(lines),
        )

    return await run(
        uow,
        lambda: post(
            uow,
            REVERSALS,
            str(entry_id),
            draft,
            same=lambda posted: posted.reverses == entry_id,
        ),
    )


async def open_period(command: OpenPeriod, uow: AccountingUnitOfWork) -> Period:
    """Open a period; one whose dates another covers is refused.

    Raises:
        PeriodsOverlap: It ends before it starts, or another period covers
            some of its days.
        PeriodNameTaken: Another period has its name.
    """
    period = Period.open(command.name, command.starts_on, command.ends_on)

    async def work() -> Period:
        async with uow:
            await uow.lock("accounting.periods")
            if await uow.periods.overlapping(period.starts_on, period.ends_on):
                raise PeriodsOverlap("Another period covers some of these dates.")
            await uow.periods.add(period)
            await uow.commit()
        return period

    return await run(uow, work)


async def close_period(command: ClosePeriod, uow: AccountingUnitOfWork) -> Period:
    """Close a period, once the postings dated in it so far have committed.

    Raises:
        PeriodNotFound: No period has that id.
        PeriodIsClosed: It is closed already.
    """

    async def work() -> Period:
        async with uow:
            period = await uow.periods.get_for_update(PeriodId(command.period_id))
            period.close()
            await uow.commit()
        return period

    return await run(uow, work)
