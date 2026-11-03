"""The routes that verify a user's phone numbers.

::

    from domainkit.contrib.phones.api import router as phones_router

    router.include_router(phones_router, prefix="/auth")

- ``POST /phones/codes`` (a user): ``{"phone"}``; a code goes to the
  number by text message. ``202``, with when it expires and when another
  may be asked for.
- ``POST /phones`` (a user): ``{"phone", "code"}``; the number, verified.
- ``GET /phones``, ``DELETE /phones/{id}`` (a user).
"""

from datetime import datetime
from typing import Annotated, Final
from uuid import UUID

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field

from domainkit.contrib.auth import AbstractBaseUser
from domainkit.http.access import IsUnscoped, Require

from .exceptions import WrongCode
from .services import confirm_code, remove_phone, send_code
from .stores import PhonesUnitOfWork, VerifiedPhone

__all__ = ["router"]

router = APIRouter(tags=["phones"])
SIGNED_IN = Depends(Require(IsUnscoped()))
PHONE_LENGTH: Final = 32
"""Characters a number may be sent with, separators included."""


class PhoneIn(BaseModel):
    """A number, in international form: ``+33 6 12 34 56 78``."""

    model_config = ConfigDict(extra="forbid")

    phone: str = Field(min_length=1, max_length=PHONE_LENGTH)


class ConfirmIn(PhoneIn):
    """The number, and the code sent to it."""

    code: str = Field(min_length=1, max_length=20, repr=False)


class PendingOut(BaseModel):
    """A code on its way."""

    phone: str
    expires_at: datetime
    resend_at: datetime


class PhoneOut(BaseModel):
    """A verified number."""

    id: UUID
    phone: str
    verified_at: datetime


def _out(phone: VerifiedPhone) -> PhoneOut:
    """The answer for ``phone``."""
    return PhoneOut(id=phone.id, phone=phone.phone, verified_at=phone.verified_at)


@router.post("/phones/codes", status_code=status.HTTP_202_ACCEPTED)
async def ask_for_code(
    body: PhoneIn, user: Annotated[AbstractBaseUser, SIGNED_IN]
) -> PendingOut:
    """Send a code to the number, to prove the user receives messages there."""
    async with PhonesUnitOfWork() as uow:
        pending = await send_code(uow, user.id, body.phone)
        await uow.commit()
    return PendingOut(
        phone=pending.phone, expires_at=pending.expires_at, resend_at=pending.resend_at
    )


@router.post("/phones", status_code=status.HTTP_201_CREATED)
async def verify_phone(
    body: ConfirmIn, user: Annotated[AbstractBaseUser, SIGNED_IN]
) -> PhoneOut:
    """Check the code; the number, verified."""
    async with PhonesUnitOfWork() as uow:
        verified = await confirm_code(uow, user.id, body.phone, body.code)
        await uow.commit()  # A wrong try is counted, then refused.
    if verified is None:
        raise WrongCode()
    return _out(verified)


@router.get("/phones")
async def list_phones(user: Annotated[AbstractBaseUser, SIGNED_IN]) -> list[PhoneOut]:
    """The user's verified numbers, oldest first."""
    async with PhonesUnitOfWork() as uow:
        return [_out(each) for each in await uow.phones.of_user(user.id)]


@router.delete("/phones/{phone_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_phone(
    phone_id: UUID, user: Annotated[AbstractBaseUser, SIGNED_IN]
) -> None:
    """Forget one of the user's verified numbers."""
    async with PhonesUnitOfWork() as uow:
        await remove_phone(uow, user.id, phone_id)
        await uow.commit()
