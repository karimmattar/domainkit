"""The routes of logging in and linking through identity providers.

::

    from domainkit.contrib.identity.api import router as identity_router

    router.include_router(identity_router, prefix="/auth")

- ``GET /identity/providers`` (anyone): the providers' names.
- ``POST /identity/{name}/login`` (anyone): ``{"authorization_url"}``, where
  to send the browser.
- ``POST /identity/{name}/link`` (a user): the same, to link an identity.
- ``POST /identity/{name}/callback`` (anyone): ``{"code", "state"}`` the
  provider sent back; tokens, a second factor's challenge (202), or
  ``{"linked"}``.
- ``POST /identity/ticket`` (anyone): ``{"ticket"}`` a posted response (SAML)
  gave; tokens or a challenge.
- ``GET /identity/linked`` (a user): the user's identities.
- ``DELETE /identity/linked/{name}`` (a user): unlink one, unless it is the
  last way in.
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from domainkit.contrib.auth import AbstractBaseUser
from domainkit.contrib.auth.integration import (
    PASSWORD_MAX_LENGTH,
    MfaChallengeOut,
    TokenOut,
    challenge,
    issue_login,
    reauthenticate,
)
from domainkit.contrib.auth.models import UserId
from domainkit.http.access import IsUnscoped, Public, Require

from .exceptions import NoAccount
from .services import LINK, LOGIN, begin, complete, identity_settings, unlink
from .stores import IdentityUnitOfWork

__all__ = ["router"]

router = APIRouter(tags=["identity"])
SIGNED_IN = Depends(Require(IsUnscoped()))


class ProvidersOut(BaseModel):
    """The providers users may log in with, by name."""

    providers: list[str]
    saml: list[str]


class AuthorizationOut(BaseModel):
    """Where to send the browser."""

    authorization_url: str


class CallbackIn(BaseModel):
    """What the provider sent the browser back with."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=4096, repr=False)
    state: str = Field(min_length=1, max_length=64)


class TicketIn(BaseModel):
    """A ticket a posted response gave."""

    model_config = ConfigDict(extra="forbid")

    ticket: str = Field(min_length=1, max_length=64, repr=False)


class LinkedOut(BaseModel):
    """The provider an identity was linked at."""

    linked: str


class IdentityOut(BaseModel):
    """One of the user's identities."""

    provider: str
    email: str | None
    created_at: datetime


@router.get("/identity/providers", dependencies=[Public])
async def providers() -> ProvidersOut:
    """The configured providers' names."""
    config = identity_settings()
    return ProvidersOut(providers=sorted(config.PROVIDERS), saml=sorted(config.SAML))


@router.post("/identity/{name}/login", dependencies=[Public])
async def start_login(name: str) -> AuthorizationOut:
    """Start a login at ``name``."""
    return AuthorizationOut(authorization_url=await begin(name, LOGIN))


class LinkIn(BaseModel):
    """The password, unless the user stepped up in the last ten minutes."""

    model_config = ConfigDict(extra="forbid")

    password: str | None = Field(
        default=None, min_length=1, max_length=PASSWORD_MAX_LENGTH, repr=False
    )


@router.post("/identity/{name}/link")
async def start_link(
    name: str,
    request: Request,
    user: Annotated[AbstractBaseUser, SIGNED_IN],
    body: LinkIn | None = None,
) -> AuthorizationOut:
    """Start linking an identity at ``name`` to the user, who authenticates again.

    A linked identity is a way into the account: the password, or a step-up
    in the last ten minutes, as for a passkey; never while staff act as the
    user.
    """
    password = None if body is None else body.password
    await reauthenticate(request, user, password, "link_identity")
    return AuthorizationOut(authorization_url=await begin(name, LINK, user.id))


@router.post(
    "/identity/{name}/callback",
    dependencies=[Public],
    response_model_exclude_none=True,
    responses={status.HTTP_202_ACCEPTED: {"model": MfaChallengeOut}},
)
async def callback(
    name: str, body: CallbackIn, response: Response
) -> TokenOut | MfaChallengeOut | LinkedOut:
    """Finish a login (tokens, or a challenge) or a link."""
    _no_store(response)
    flow, user = await complete(name, body.code, body.state)
    if user is None:
        return LinkedOut(linked=flow.provider)
    return await logged_in(user, response)


@router.post(
    "/identity/ticket",
    dependencies=[Public],
    response_model_exclude_none=True,
    responses={status.HTTP_202_ACCEPTED: {"model": MfaChallengeOut}},
)
async def redeem(body: TicketIn, response: Response) -> TokenOut | MfaChallengeOut:
    """Trade a ticket for tokens, once.

    Raises:
        NoAccount: The ticket is unknown, used, or expired.
    """
    _no_store(response)
    async with IdentityUnitOfWork() as uow:
        user_id = await uow.tickets.redeem(body.ticket)
        user = None if user_id is None else await uow.users.find(UserId(user_id))
        await uow.commit()
    if user is None or not user.is_active:
        raise NoAccount("That ticket expired or was used; log in again.")
    return await logged_in(user, response)


@router.get("/identity/linked")
async def linked(user: Annotated[AbstractBaseUser, SIGNED_IN]) -> list[IdentityOut]:
    """The user's identities."""
    async with IdentityUnitOfWork() as uow:
        found = await uow.identities.of_user(user.id)
    return [
        IdentityOut(provider=i.provider, email=i.email, created_at=i.created_at)
        for i in found
    ]


@router.delete("/identity/linked/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def remove(name: str, user: Annotated[AbstractBaseUser, SIGNED_IN]) -> None:
    """Unlink the user's identity at ``name``."""
    await unlink(user, name)


async def logged_in(
    user: AbstractBaseUser, response: Response
) -> TokenOut | MfaChallengeOut:
    """Tokens for ``user``, or their second factor's challenge (202)."""
    pending = await challenge(user)
    if pending is not None:
        response.status_code = status.HTTP_202_ACCEPTED
        return pending
    issued = await issue_login(user, factor_proven=False)
    return TokenOut(
        access_token=issued.access_token,
        expires_in=issued.expires_in,
        refresh_token=issued.refresh_token,
    )


def _no_store(response: Response) -> None:
    """Tokens are never cached."""
    response.headers["Cache-Control"] = "no-store"


def _include_saml() -> None:
    """Mount the SAML routes (they import signxml only when a response comes)."""
    from .saml import router as saml_router

    router.include_router(saml_router)


_include_saml()
