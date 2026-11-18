"""What the wallets domain does: wallets are accounts, payments are entries.

A wallet holds money the business owes its customer: a liability account
of ``domainkit.contrib.accounting``, which never goes below zero. Every
movement is one entry whose debits equal its credits, booked once per
source and reference. The ledger locks the accounts an entry moves in one
order, so two payments at once from one wallet wait for each other, and
the second is refused if the first left too little (``insufficient_funds``).

No HTTP here: the routes, the worker, ``manage.py seed``, and the tests
run the same functions.
"""

from datetime import timedelta
from decimal import Decimal
from typing import Final
from uuid import UUID, uuid4

from domainkit import clock
from domainkit.contrib.accounting import (
    Account,
    AccountId,
    AccountingUnitOfWork,
    AccountNotFound,
    AccountType,
    CaptureHold,
    Entry,
    Hold,
    HoldId,
    Line,
    OpenAccount,
    OpenPeriod,
    PlaceHold,
    PostEntry,
    ReferenceReused,
    ReleaseHold,
    Side,
    capture_hold,
    open_account,
    open_period,
    place_hold,
    post_entry,
    release_hold,
)
from domainkit.contrib.messaging import periodic
from domainkit.ddd import F
from domainkit.money import Money

CURRENCY: Final = "USD"
BANK: Final = "1000"
"""The business's bank account, where the customers' money is: an asset."""
MERCHANTS: Final = "2200"
"""What the business owes merchants until it pays them out: a liability."""
WALLETS: Final = "2100"
"""Each wallet's code starts so, then a random number that names nobody."""
HOUSE: Final = (
    (BANK, "Bank", AccountType.ASSET),
    (MERCHANTS, "Merchants payable", AccountType.LIABILITY),
)


@periodic(every=timedelta(days=1), name="wallets.open_books")
async def open_books() -> None:
    """Open this month's period and the next, and the business's own accounts.

    An entry is booked only in an open period, so the next month is opened
    ahead of it: the worker runs this each day, and ``manage.py seed
    wallets`` once, before the first payment. What is open stays as it is.
    """
    this_month = clock.today().replace(day=1)
    for first in (this_month, (this_month + timedelta(days=31)).replace(day=1)):
        last = (first + timedelta(days=31)).replace(day=1) - timedelta(days=1)
        async with AccountingUnitOfWork() as uow:
            period = await uow.periods.covering(first)
        if period is None:
            month = OpenPeriod(name=f"{first:%Y-%m}", starts_on=first, ends_on=last)
            await open_period(month, AccountingUnitOfWork())
    async with AccountingUnitOfWork() as uow:
        opened = {
            account.code
            for account in await uow.accounts.filter(F("code").in_([BANK, MERCHANTS]))
        }
    for code, name, kind in HOUSE:
        if code not in opened:
            house = OpenAccount(code=code, name=name, type=kind, currency=CURRENCY)
            await open_account(house, AccountingUnitOfWork())


async def house_account(code: str) -> AccountId:
    """The id of one of the business's own accounts."""
    async with AccountingUnitOfWork() as uow:
        (account,) = await uow.accounts.filter(F("code") == code)
    return account.id


async def open_wallet(holder: str) -> Account:
    """Open an empty wallet for ``holder``.

    Its code is random: erasing the holder renames the holder, never the
    code, so a code must name nobody.
    """
    wallet = OpenAccount(
        code=f"{WALLETS}-{uuid4().hex[:16]}",
        name="Wallet",
        type=AccountType.LIABILITY,
        currency=CURRENCY,
        holder=holder,
    )
    return await open_account(wallet, AccountingUnitOfWork())


async def find_wallet(wallet_id: UUID) -> Account:
    """A wallet; an account of the business's own is not found (404)."""
    async with AccountingUnitOfWork() as uow:
        account = await uow.accounts.get(AccountId(wallet_id))
    if not account.code.startswith(f"{WALLETS}-"):
        raise AccountNotFound(f"Account {wallet_id} does not exist.")
    return account


async def wallet_of(holder: str, wallet_id: UUID) -> Account:
    """``holder``'s wallet; anyone else's is not found (404)."""
    wallet = await find_wallet(wallet_id)
    if wallet.holder != holder:
        raise AccountNotFound(f"Account {wallet_id} does not exist.")
    return wallet


async def deposit(wallet: Account, amount: Money, key: UUID) -> Entry:
    """Book money that reached the bank for a wallet: both go up."""
    bank = await house_account(BANK)
    return await post_entry(
        PostEntry(
            source="wallets.deposit",
            reference=f"{wallet.id}:{key}",
            booked_on=clock.today(),
            description="Deposit",
            lines=(Line.debit(bank, amount), Line.credit(wallet.id, amount)),
        ),
        AccountingUnitOfWork(),
    )


async def pay(wallet: Account, amount: Money, key: UUID, description: str) -> Entry:
    """Pay ``amount`` from the wallet to the merchants, once per key.

    The reference is the wallet and the request's ``Idempotency-Key``. The
    ledger books one entry per reference: sent again on the day it was
    booked, the payment returns that entry. A week later, once the
    middleware has forgotten the key, the date differs, and the ledger
    refuses the reference (``reference_reused``); the entry booked under it
    is returned if it moved the same amount between the same accounts, as
    a retry of the same payment does. Anything else under the key is
    refused (409).
    """
    merchants = await house_account(MERCHANTS)
    payment = PostEntry(
        source="wallets.payment",
        reference=f"{wallet.id}:{key}",
        booked_on=clock.today(),
        description=description,
        lines=(Line.debit(wallet.id, amount), Line.credit(merchants, amount)),
    )
    try:
        return await post_entry(payment, AccountingUnitOfWork())
    except ReferenceReused:
        async with AccountingUnitOfWork() as uow:
            booked = await uow.entries.by_reference(payment.source, payment.reference)
        if booked is None or moves(booked) != moves(payment):
            raise
        return booked


def moves(entry: Entry | PostEntry) -> set[tuple[AccountId, Side, Decimal, str]]:
    """What an entry moves: each account, side, amount, and currency."""
    if isinstance(entry, PostEntry):
        return {
            (line.account_id, line.side, line.amount.amount, line.amount.currency.code)
            for line in entry.lines
        }
    return {
        (posting.account_id, posting.side, posting.amount, posting.currency)
        for posting in entry.postings
    }


async def authorize(wallet: Account, amount: Money, key: UUID) -> Hold:
    """Hold ``amount`` on the wallet: its balance stays, its available drops."""
    hold = PlaceHold(
        account_id=wallet.id,
        amount=amount,
        source="wallets.hold",
        reference=f"{wallet.id}:{key}",
    )
    return await place_hold(hold, AccountingUnitOfWork())


async def capture(hold_id: UUID, amount: Money) -> Hold:
    """Pay ``amount`` of a hold to the merchants, and give the rest back."""
    merchants = await house_account(MERCHANTS)
    command = CaptureHold(
        hold_id=HoldId(hold_id), counter_account_id=merchants, amount=amount
    )
    return await capture_hold(command, AccountingUnitOfWork())


async def release(hold_id: UUID) -> Hold:
    """Give a hold back whole."""
    return await release_hold(
        ReleaseHold(hold_id=HoldId(hold_id)), AccountingUnitOfWork()
    )
