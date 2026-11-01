"""The money rungs' project: the catalog's routes, and money, a ledger, a card.

The rungs on this project (``project="money"`` in ``ladder``) install
``money_settings.DOMAINS`` beside the catalog's, with its ``SETTINGS``, and
serve ``router``:
a write of an amount, a posting a limit counts, a read of an encrypted card
number, and a route behind a step-up (PERF-32). The other rungs never
import this module, so what it loads costs them nothing.
"""

from datetime import timedelta
from typing import Annotated, Final
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from domainkit import clock
from domainkit.contrib.accounting import (
    AccountId,
    AccountingUnitOfWork,
    Line,
    PostEntry,
    post_entry,
)
from domainkit.contrib.auth import AnyUser
from domainkit.contrib.auth.access import StepUp
from domainkit.http.access import Require
from domainkit.money import Money

from .routes import router as catalog_router
from .vault.models import CardId, Wallet, WalletId
from .vault.uow import VaultUnitOfWork

Caller = Annotated[AnyUser, Depends(Require())]
"""Any user who logged in."""
RECENT: Final = Depends(Require(StepUp(max_age=timedelta(days=1))))
"""A user whose token stepped up within the day (the bench token did)."""

router = APIRouter()
router.include_router(catalog_router)


class WalletIn(BaseModel):
    """A new wallet and the money in it."""

    owner: str = Field(max_length=100)
    balance: Money


class WalletOut(BaseModel):
    """A wallet."""

    id: UUID
    owner: str
    balance: Money


class PostingIn(BaseModel):
    """An amount from one account to another."""

    debit: UUID
    credit: UUID
    amount: Money


@router.post("/wallets", status_code=201)
async def create_wallet(body: WalletIn, user: Caller) -> WalletOut:
    """A new wallet, its amount written as two columns, through a unit of work."""
    wallet = Wallet(id=WalletId(uuid4()), owner=body.owner, balance=body.balance)
    async with VaultUnitOfWork() as uow:
        await uow.wallets.add(wallet)
        await uow.commit()
    return WalletOut(id=wallet.id, owner=wallet.owner, balance=wallet.balance)


@router.post("/postings", status_code=201)
async def create_posting(body: PostingIn, user: Caller) -> dict[str, str]:
    """A two-line entry; the credited account's holder has a daily limit."""
    entry = await post_entry(
        PostEntry(
            source="bench",
            reference=uuid4().hex,
            booked_on=clock.now().date(),
            lines=(
                Line.debit(AccountId(body.debit), body.amount),
                Line.credit(AccountId(body.credit), body.amount),
            ),
        ),
        AccountingUnitOfWork(),
    )
    return {"id": str(entry.id)}


@router.get("/cards/{card_id}")
async def read_card(card_id: UUID, user: Caller) -> dict[str, str]:
    """A card, its number opened as the row loads; the last four digits shown."""
    async with VaultUnitOfWork() as uow:
        card = await uow.cards.get(CardId(card_id))
    return {"holder": card.holder, "number": card.number[-4:]}


@router.get("/stepped", dependencies=[RECENT])
async def stepped() -> dict[str, str]:
    """Behind a step-up: the token's last step-up is read for each request."""
    return {"status": "ok"}
