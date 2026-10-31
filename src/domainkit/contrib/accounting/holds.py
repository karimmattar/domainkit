"""Holds: reserve an amount on an account, then capture it into an entry or release it.

What an account has available is its balance less its active holds. A
hold is placed once per ``source`` and ``reference``, as an entry is
posted; captured, it posts an entry taking the amount (or less: the rest
is released) out of the account into another, under the source
``CAPTURES`` and the hold's id. A hold expires at ``expires_at``: it can
no longer be captured, and the worker releases it within
``ACCOUNTING.EXPIRE_HOLDS_EVERY`` (``expire_holds``).
"""

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import select

from domainkit import clock
from domainkit.db import connections
from domainkit.ddd import Command
from domainkit.money import Money

from .exceptions import HoldNotActive, InvalidAmount, ReferenceReused
from .ledger import book, claim, config, lock_accounts, run
from .models import (
    ZERO,
    AccountId,
    Entry,
    Hold,
    HoldId,
    HoldStatus,
    Line,
)
from .tables import holds
from .uow import AccountingUnitOfWork, accounting_alias

CAPTURES = "accounting.hold"
"""The source of the entries captures post; each one's reference is the hold's id."""
EXPIRY_BATCH = 500
"""Expired holds ``expire_holds`` reads at a time."""


class PlaceHold(Command):
    """Reserve ``amount`` on an account, once per ``source`` and ``reference``."""

    account_id: UUID
    amount: Money
    source: str
    reference: str
    expires_at: datetime | None = None
    """When it expires; None: ``ACCOUNTING.HOLD_TTL`` from now."""


class CaptureHold(Command):
    """Post the hold's amount (or ``amount``, less) out of its account into another."""

    hold_id: UUID
    counter_account_id: UUID
    amount: Money | None = None
    booked_on: date | None = None
    """The entry's date; None: today."""
    description: str = ""


class ReleaseHold(Command):
    """Make a hold's amount available again, without posting anything."""

    hold_id: UUID


async def place_hold(command: PlaceHold, uow: AccountingUnitOfWork) -> Hold:
    """Reserve an amount on an account, or return the hold placed under its reference.

    Raises:
        ReferenceReused: Another hold was placed under that reference.
        AccountNotFound: No account has that id.
        InvalidEntry, InvalidAmount: As ``Hold.place``.
        AccountIsClosed, InsufficientFunds: As ``Account.reserve``.
    """
    account_id = AccountId(command.account_id)
    expires_at = command.expires_at or clock.now() + config().HOLD_TTL

    async def work() -> Hold:
        async with uow:
            await uow.lock(f"accounting.hold:{command.source}:{command.reference}")
            placed = await uow.holds.by_reference(command.source, command.reference)
            if placed is not None:
                if not placed.same_as(account_id, command.amount):
                    raise ReferenceReused(
                        "Another hold was placed under this source and reference."
                    )
                return placed
            account = await uow.accounts.get_for_update(account_id)
            hold = Hold.place(
                account, command.amount, command.source, command.reference, expires_at
            )
            await uow.holds.add(hold)
            await uow.commit()
        return hold

    return await run(uow, work)


async def capture_hold(command: CaptureHold, uow: AccountingUnitOfWork) -> Hold:
    """Capture a hold into an entry; capturing it again returns it as captured.

    The entry credits the hold's account on the side that lowers its
    balance and debits ``counter_account_id`` on the other, by ``amount``
    (the whole hold by default); what is left of the hold is released.

    Raises:
        HoldNotFound, AccountNotFound: No hold, or no account, has that id.
        HoldNotActive: It was released or has expired.
        InvalidAmount: ``amount`` is more than the hold, or not more than zero.
        NoOpenPeriod, PeriodIsClosed: The date is in no open period.
        ReferenceReused: An entry was posted under the hold's capture
            reference (only before ``post_entry`` refused the source).
    """
    hold_id = HoldId(command.hold_id)
    counter = AccountId(command.counter_account_id)
    booked_on = command.booked_on or clock.now().date()
    max_lines = config().MAX_LINES

    async def work() -> Hold:
        async with uow:
            await uow.lock(f"accounting.hold:{hold_id}")
            hold = await uow.holds.get_for_update(hold_id)
            if hold.status is HoldStatus.CAPTURED:
                return hold
            amount = command.amount or hold.money
            if amount.currency != hold.money.currency or not (
                ZERO < amount.amount <= hold.amount
            ):
                raise InvalidAmount("A capture takes more than zero, up to the hold.")
            # As ``post``: the entry's lock, then whether one was posted. The
            # hold is active, so no capture of it was.
            await claim(uow, CAPTURES, str(hold_id), same=lambda posted: False)
            accounts = await lock_accounts(uow, (hold.account_id, counter))
            held = accounts[hold.account_id]
            entry = Entry.draft(
                CAPTURES,
                str(hold_id),
                booked_on,
                (
                    Line(hold.account_id, held.normal_balance.opposite, amount),
                    Line(counter, held.normal_balance, amount),
                ),
                description=command.description,
                max_lines=max_lines,
            )
            hold.finish(held, HoldStatus.CAPTURED, entry.id)
            await book(uow, entry, accounts)
            # The hold refers to its entry once the entry's row is written:
            # PostgreSQL checks the reference at each statement.
            await uow.session.flush()
            hold.entry_id = entry.id
            await uow.commit()
        return hold

    return await run(uow, work)


async def release_hold(command: ReleaseHold, uow: AccountingUnitOfWork) -> Hold:
    """Release a hold; one released or expired already is returned as it is.

    Raises:
        HoldNotFound: No hold has that id.
        HoldNotActive: It was captured.
    """
    hold, _ = await finish(uow, HoldId(command.hold_id), HoldStatus.RELEASED)
    return hold


async def finish(
    uow: AccountingUnitOfWork, hold_id: HoldId, status: HoldStatus
) -> tuple[Hold, bool]:
    """End an active hold as ``status`` (released or expired); whether this did.

    A hold that finished already is left as it is, but releasing a
    captured one is refused (``HoldNotActive``).
    """

    async def work() -> tuple[Hold, bool]:
        async with uow:
            await uow.lock(f"accounting.hold:{hold_id}")
            hold = await uow.holds.get_for_update(hold_id)
            if hold.status is not HoldStatus.ACTIVE:
                if hold.status is HoldStatus.CAPTURED and status is HoldStatus.RELEASED:
                    raise HoldNotActive("The hold was captured.")
                return hold, False
            (account,) = (await lock_accounts(uow, (hold.account_id,))).values()
            hold.finish(account, status)
            await uow.commit()
        return hold, True

    return await run(uow, work)


async def expire_holds(now: datetime | None = None) -> int:
    """Release every active hold whose time is up, each in its own transaction.

    Returns:
        How many holds this call expired.
    """
    moment = now or clock.now()
    statement = (
        select(holds.c.id)
        .where(holds.c.status == HoldStatus.ACTIVE, holds.c.expires_at <= moment)
        .order_by(holds.c.expires_at, holds.c.id)
        .limit(EXPIRY_BATCH)
    )
    expired = 0
    while True:
        async with connections.engine(accounting_alias()).connect() as connection:
            due = list(await connection.scalars(statement))
        for hold_id in due:
            _, done = await finish(
                AccountingUnitOfWork(), HoldId(hold_id), HoldStatus.EXPIRED
            )
            expired += done
        if len(due) < EXPIRY_BATCH:
            return expired
