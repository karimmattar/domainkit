"""The routes of sign-ins approved from the phone: the web's, and the phone's.

::

    from domainkit.contrib.ciba.api import router as sign_in_router

    router.include_router(sign_in_router, prefix="/auth")

The project's web pages (public, with the login captcha, ``START_RATE``
per address):

- ``POST /app-sign-in/push`` ``{"identifier"}``: the user's apps get a
  push. Answers the poll handle ``request``, ``expires_in``, ``interval``,
  and the ``match_code`` the page shows (``NUMBER_MATCHING``).
- ``POST /app-sign-in/qr``: answers ``request``, ``expires_in``,
  ``interval``, and ``qr``, what the page renders as a QR code.
- ``POST /app-sign-in/token`` ``{"request"}``: tokens as ``/login`` gives
  them, once the phone approved; else 400 ``authorization_pending``,
  ``slow_down``, ``expired_token``, ``access_denied`` or ``sign_in_unknown``.
- ``POST /app-sign-in/session`` ``{"request"}``: the same, as a browser
  session in cookies (``/session``).

The phone (a user's credentials without scopes, and a request signed by
one of their bound devices, ``DeviceSigned``):

- ``GET /app-sign-in/requests``: the requests waiting for an answer, with
  who asks and from where.
- ``POST /app-sign-in/requests/claim`` ``{"code"}``: the QR request scanned.
- ``POST /app-sign-in/requests/{id}/approve`` ``{"match_code"?}``.
- ``POST /app-sign-in/requests/{id}/deny`` ``{"report"?}``.
"""

from datetime import datetime
from typing import Annotated, Final
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from domainkit.contrib.auth import AbstractBaseUser
from domainkit.contrib.auth.captcha import captcha
from domainkit.contrib.auth.integration import (
    CsrfOut,
    TokenOut,
    address_key,
    attempt,
    check_origin,
    gates_passed,
    issue_login,
    start_browser_session,
)
from domainkit.contrib.auth.models import UserId
from domainkit.contrib.devices import DeviceSigned
from domainkit.contrib.devices.binding import signing_device
from domainkit.http.access import IsUnscoped, Public, Require
from domainkit.http.errors import error_responses

from .exceptions import SignInUnknown, WrongMatchCode
from .services import (
    Requester,
    Started,
    approve,
    ciba_settings,
    claim,
    deny,
    poll,
    start_push,
    start_qr,
)
from .stores import CibaUnitOfWork, SignInRequest

__all__ = ["router"]

router = APIRouter(tags=["sign-in approvals"])
DEVICE_SIGNED = Depends(Require(IsUnscoped(), DeviceSigned()))
HANDLE_LENGTH: Final = 100
"""Characters a poll handle may be sent with: 43 are made."""
CODE_LENGTH: Final = 400
"""Characters a scanned QR code may hold: ``QR_FORMAT`` and its 43."""
STARTS_THROTTLED: Final = "Too many sign-ins were started; try again later."


class IdentifierIn(BaseModel):
    """Who signs in: an email address, a mobile number, or a username."""

    model_config = ConfigDict(extra="forbid")

    identifier: str = Field(min_length=1, max_length=255)


class StartedOut(BaseModel):
    """A sign-in waiting for the phone: poll with ``request`` every ``interval`` s."""

    request: str = Field(repr=False)
    """The poll handle: keep it in the page, never in the QR code or a URL."""
    expires_in: int
    interval: int
    match_code: str | None = None
    """Show it: the phone types it to approve."""
    qr: str | None = Field(default=None, repr=False)
    """Render it as a QR code for the phone to scan."""


class PollIn(BaseModel):
    """The poll handle a start answered."""

    model_config = ConfigDict(extra="forbid")

    request: str = Field(min_length=1, max_length=HANDLE_LENGTH, repr=False)


class ClaimIn(BaseModel):
    """What the QR code the phone scanned holds."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=CODE_LENGTH, repr=False)


class ApproveIn(BaseModel):
    """The number the sign-in page shows, when it shows one."""

    model_config = ConfigDict(extra="forbid")

    match_code: str | None = Field(default=None, max_length=10)


class DenyIn(BaseModel):
    """Whether the user did not start it: someone who knows them did."""

    model_config = ConfigDict(extra="forbid")

    report: bool = False


class SignInOut(BaseModel):
    """A sign-in waiting for the phone's answer: who asks, from where, when."""

    id: UUID
    channel: str
    """``push`` or ``qr``."""
    client: str | None
    """The name of the service that asks; None: this site's own pages."""
    binding_message: str | None
    """The text the asking service shows too: they should match."""
    address: str | None
    """The address the sign-in was started from."""
    browser: str | None
    """The ``User-Agent`` it was started with."""
    same_network: bool | None
    """Whether the phone is on that address now; None: unknown."""
    match_required: bool
    """Approving needs the number the sign-in page shows."""
    created_at: datetime
    expires_at: datetime


def _no_store(response: Response) -> None:
    """Keep handles and tokens out of every cache."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _address(request: Request) -> str | None:
    """The client's address, as the throttles see it; None without one."""
    return (request.client.host or None) if request.client else None


def _requester(request: Request) -> Requester:
    """Who starts a sign-in: their address and browser, for the phone to see."""
    return Requester(_address(request), request.headers.get("user-agent") or None)


async def _count_start(request: Request) -> None:
    """Count a start against ``START_RATE`` for the client's address.

    Raises:
        APIError: 429 past the rate.
    """
    await attempt(
        request,
        [(address_key(request, "ciba-start"), ciba_settings().START_RATE)],
        action="ciba.start",
        detail=STARTS_THROTTLED,
    )


def _started(started: Started) -> StartedOut:
    """What a start answers."""
    return StartedOut(
        request=started.handle,
        expires_in=started.expires_in,
        interval=started.interval,
        match_code=started.match_code,
        qr=started.qr,
    )


@router.post(
    "/app-sign-in/push",
    dependencies=[Public, captcha("login")],
    response_model_exclude_none=True,
    responses=error_responses(status.HTTP_429_TOO_MANY_REQUESTS),
)
async def start_push_sign_in(
    body: IdentifierIn, request: Request, response: Response
) -> StartedOut:
    """Ask the user's phone to approve a sign-in of this browser.

    Whoever the identifier names, or not, the answer is the same: the
    page cannot tell a user who exists from one who does not.
    """
    _no_store(response)
    await _count_start(request)
    async with CibaUnitOfWork() as uow:
        user = await uow.users.find_by_identifier(body.identifier)
        started = await start_push(
            uow,
            user,
            requester=_requester(request),
            match=ciba_settings().NUMBER_MATCHING,
        )
        await uow.commit()
    return _started(started)


@router.post(
    "/app-sign-in/qr",
    dependencies=[Public, captcha("login")],
    response_model_exclude_none=True,
    responses=error_responses(status.HTTP_429_TOO_MANY_REQUESTS),
)
async def start_qr_sign_in(request: Request, response: Response) -> StartedOut:
    """A QR code for the phone to scan; ask for a new one when it expires."""
    _no_store(response)
    await _count_start(request)
    async with CibaUnitOfWork() as uow:
        started = await start_qr(uow, requester=_requester(request))
        await uow.commit()
    return _started(started)


async def _approved_user(body: PollIn) -> AbstractBaseUser:
    """The user whose phone approved the page's request, redeemed now.

    Raises:
        PollRefused: Not approved (yet), or not this page's to redeem.
    """
    approved = await poll(body.request, client_id=None)
    async with CibaUnitOfWork() as uow:
        user = (
            None
            if approved.user_id is None
            else await uow.users.find(UserId(approved.user_id))
        )
    if user is None or not user.is_active:
        raise SignInUnknown()
    return user


@router.post(
    "/app-sign-in/token",
    dependencies=[Public],
    response_model_exclude_none=True,
    responses=error_responses(status.HTTP_400_BAD_REQUEST),
)
async def redeem_sign_in(body: PollIn, response: Response) -> TokenOut:
    """Tokens, once the phone approved; until then, why not."""
    _no_store(response)
    user = await _approved_user(body)
    with gates_passed(user.id):  # The phone approved: that was the gate.
        issued = await issue_login(user, factor_proven=True)
    return TokenOut(
        access_token=issued.access_token,
        expires_in=issued.expires_in,
        refresh_token=issued.refresh_token,
    )


@router.post(
    "/app-sign-in/session",
    dependencies=[Public],
    responses=error_responses(status.HTTP_400_BAD_REQUEST, status.HTTP_403_FORBIDDEN),
)
async def start_approved_session(
    body: PollIn, request: Request, response: Response
) -> CsrfOut:
    """A browser session in cookies, once the phone approved (as ``/session``)."""
    _no_store(response)
    check_origin(request)
    user = await _approved_user(body)
    with gates_passed(user.id):
        return await start_browser_session(request, user, response)


async def _device_id(request: Request, user: AbstractBaseUser) -> UUID:
    """The bound device that signed the request (``DEVICE_SIGNED`` checked it)."""
    device = await signing_device(request, user)
    assert device is not None  # noqa: S101 - DeviceSigned allowed the request.
    return device.id


async def _out(
    uow: CibaUnitOfWork, request: Request, sign_ins: list[SignInRequest]
) -> list[SignInOut]:
    """What the phone is shown of each request."""
    names: dict[str, str] = {}
    for each in sign_ins:
        if each.client_id is not None and each.client_id not in names:
            client = await uow.clients.get(each.client_id)
            names[each.client_id] = each.client_id if client is None else client.name
    here = _address(request)
    return [
        SignInOut(
            id=each.id,
            channel=each.channel,
            client=None if each.client_id is None else names[each.client_id],
            binding_message=each.binding_message,
            address=each.requester_ip,
            browser=each.requester_agent,
            same_network=(
                None
                if here is None or each.requester_ip is None
                else here == each.requester_ip
            ),
            match_required=each.match_code is not None,
            created_at=each.created_at,
            expires_at=each.expires_at,
        )
        for each in sign_ins
    ]


@router.get("/app-sign-in/requests")
async def list_sign_ins(
    request: Request, user: Annotated[AbstractBaseUser, DEVICE_SIGNED]
) -> list[SignInOut]:
    """The user's sign-ins waiting for an answer, oldest first."""
    from domainkit.contrib.auth.integration import utc_now

    async with CibaUnitOfWork() as uow:
        return await _out(
            uow, request, await uow.requests.waiting_for(user.id, utc_now())
        )


@router.post(
    "/app-sign-in/requests/claim",
    responses=error_responses(
        status.HTTP_403_FORBIDDEN, status.HTTP_422_UNPROCESSABLE_CONTENT
    ),
)
async def claim_sign_in(
    body: ClaimIn, request: Request, user: Annotated[AbstractBaseUser, DEVICE_SIGNED]
) -> SignInOut:
    """Make the QR code's sign-in the user's, to approve or deny next."""
    async with CibaUnitOfWork() as uow:
        claimed = await claim(uow, user.id, body.code, address=_address(request))
        shown = await _out(uow, request, [claimed])
        await uow.commit()
    return shown[0]


@router.post(
    "/app-sign-in/requests/{request_id}/approve",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=error_responses(
        status.HTTP_404_NOT_FOUND, status.HTTP_422_UNPROCESSABLE_CONTENT
    ),
)
async def approve_sign_in(
    request_id: UUID,
    body: ApproveIn,
    request: Request,
    user: Annotated[AbstractBaseUser, DEVICE_SIGNED],
) -> None:
    """Approve the sign-in from this device; a wrong number denies it."""
    device_id = await _device_id(request, user)
    async with CibaUnitOfWork() as uow:
        approved = await approve(
            uow, user.id, request_id, device_id, match_code=body.match_code
        )
        await uow.commit()  # A wrong number denies it, then is refused.
    if approved is None:
        raise WrongMatchCode()


@router.post(
    "/app-sign-in/requests/{request_id}/deny",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=error_responses(status.HTTP_404_NOT_FOUND),
)
async def deny_sign_in(
    request_id: UUID, body: DenyIn, user: Annotated[AbstractBaseUser, DEVICE_SIGNED]
) -> None:
    """Deny the sign-in; ``report`` says the user did not start it."""
    async with CibaUnitOfWork() as uow:
        await deny(uow, user.id, request_id, report=body.report)
        await uow.commit()
