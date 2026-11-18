"""The HTTP API of the wallets domain.

Customers open wallets and pay from them: any logged-in user, on their own
wallets only. Requests that move money carry an ``Idempotency-Key``: the
middleware answers a retry with the first answer, and the books book each
key once. Clerks book deposits and settle holds, with the accounting app's
model permissions; the rest of the back office is that app's own API,
mounted at /api/accounting (config/urls.py).
"""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Self
from uuid import UUID

from domainkit.contrib.accounting import Account, Hold, HoldStatus
from domainkit.contrib.auth import AuthenticatedUser
from domainkit.ddd import DomainError
from domainkit.http.access import HasPermissions, IsAuthenticated, Require
from domainkit.money import Money
from fastapi import APIRouter, Depends, Header, status
from pydantic import BaseModel, Field

from domains.wallets import controllers

router = APIRouter(tags=["wallets"])
CUSTOMER = Depends(Require(IsAuthenticated()))


class IdempotencyKeyRequired(DomainError):
    """Requests that move money carry an ``Idempotency-Key`` (400)."""


async def idempotency_key(
    key: Annotated[UUID | None, Header(alias="Idempotency-Key")] = None,
) -> UUID:
    """The request's ``Idempotency-Key``, a UUID; without one, it is refused.

    ``IdempotencyMiddleware`` replays a retry's answer; this makes the key
    required where money moves, so no client can leave it out.
    """
    if key is None:
        raise IdempotencyKeyRequired(
            "Send an Idempotency-Key with requests that move money."
        )
    return key


Key = Annotated[UUID, Depends(idempotency_key)]


class WalletOut(BaseModel):
    """A wallet: what it holds, what holds reserve, and what is left to spend."""

    id: UUID
    balance: Money
    held: Money
    available: Money

    @classmethod
    def of(cls, wallet: Account) -> Self:
        """Show ``wallet``."""

        def money(amount: Decimal) -> Money:
            return Money.of(amount, wallet.currency)

        return cls(
            id=wallet.id,
            balance=money(wallet.balance),
            held=money(wallet.held),
            available=money(wallet.available),
        )


class AmountIn(BaseModel):
    """An amount of money."""

    amount: Money


class PaymentIn(AmountIn):
    """A payment to the merchants, and what it is for (no personal data)."""

    description: str = Field(min_length=1, max_length=200)


class EntryOut(BaseModel):
    """A movement booked: its entry, which a refund reverses."""

    entry_id: UUID
    amount: Money


class HoldOut(BaseModel):
    """A hold on a wallet, and the entry its capture booked."""

    id: UUID
    amount: Money
    status: HoldStatus
    expires_at: datetime
    entry_id: UUID | None

    @classmethod
    def of(cls, hold: Hold) -> Self:
        """Show ``hold``."""
        return cls(
            id=hold.id,
            amount=Money.of(hold.amount, hold.currency),
            status=hold.status,
            expires_at=hold.expires_at,
            entry_id=hold.entry_id,
        )


@router.post("/wallets", status_code=status.HTTP_201_CREATED, dependencies=[CUSTOMER])
async def open_wallet(user: AuthenticatedUser) -> WalletOut:
    """Open an empty wallet for the caller."""
    return WalletOut.of(await controllers.open_wallet(str(user.id)))


@router.get("/wallets/{wallet_id}", dependencies=[CUSTOMER])
async def read_wallet(wallet_id: UUID, user: AuthenticatedUser) -> WalletOut:
    """One of the caller's wallets."""
    return WalletOut.of(await controllers.wallet_of(str(user.id), wallet_id))


@router.post(
    "/wallets/{wallet_id}/payments",
    status_code=status.HTTP_201_CREATED,
    dependencies=[CUSTOMER],
)
async def pay(
    wallet_id: UUID, body: PaymentIn, user: AuthenticatedUser, key: Key
) -> EntryOut:
    """Pay the merchants from one of the caller's wallets, once per key."""
    wallet = await controllers.wallet_of(str(user.id), wallet_id)
    entry = await controllers.pay(wallet, body.amount, key, body.description)
    return EntryOut(entry_id=entry.id, amount=body.amount)


@router.post(
    "/wallets/{wallet_id}/holds",
    status_code=status.HTTP_201_CREATED,
    dependencies=[CUSTOMER],
)
async def authorize(
    wallet_id: UUID, body: AmountIn, user: AuthenticatedUser, key: Key
) -> HoldOut:
    """Hold an amount on one of the caller's wallets, as a card authorization."""
    wallet = await controllers.wallet_of(str(user.id), wallet_id)
    return HoldOut.of(await controllers.authorize(wallet, body.amount, key))


@router.post(
    "/wallets/{wallet_id}/deposits",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(Require(HasPermissions("accounting.add_entry")))],
)
async def deposit(wallet_id: UUID, body: AmountIn, key: Key) -> EntryOut:
    """Book money the bank received for a wallet."""
    wallet = await controllers.find_wallet(wallet_id)
    entry = await controllers.deposit(wallet, body.amount, key)
    return EntryOut(entry_id=entry.id, amount=body.amount)


@router.post(
    "/holds/{hold_id}/capture",
    dependencies=[Depends(Require(HasPermissions("accounting.capture_hold")))],
)
async def capture(hold_id: UUID, body: AmountIn) -> HoldOut:
    """Pay part or all of a hold to the merchants; the rest goes back."""
    return HoldOut.of(await controllers.capture(hold_id, body.amount))


@router.post(
    "/holds/{hold_id}/release",
    dependencies=[Depends(Require(HasPermissions("accounting.release_hold")))],
)
async def release(hold_id: UUID) -> HoldOut:
    """Give a hold back whole."""
    return HoldOut.of(await controllers.release(hold_id))
