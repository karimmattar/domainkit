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
  ``interval``, ``qr``, what the page renders as a QR code, and
  ``qr_expires_in``, when to show a new one if no phone scanned it; and
  a ``match_code`` under ``QR_NUMBER_MATCHING``.
- ``POST /app-sign-in/token`` ``{"request"}``: tokens as ``/login`` gives
  them, once the phone approved; else 400 ``authorization_pending``,
  ``slow_down``, ``expired_token``, ``access_denied`` or ``sign_in_unknown``.
- ``POST /app-sign-in/session`` ``{"request"}``: the same, as a browser
  session in cookies (``/session``).
- ``POST /app-sign-in/wait`` ``{"request"}``: holds until the phone
  answers, at most ``WAIT_SECONDS``; ``{"status": "approved"}`` (redeem it
  now) or ``"pending"`` (wait again). It never redeems, nor counts as a
  poll. ``/app-sign-in/authorizations/wait``,
  ``/app-sign-in/transactions/wait`` and ``/app-sign-in/step-up/wait`` do
  the same for a session's asks.
- ``POST /app-sign-in/cancel`` ``{"request"}``: withdraw the sign-in; the
  phone no longer lists it, and nothing is reported.
  ``/app-sign-in/authorizations/cancel`` and
  ``/app-sign-in/transactions/cancel`` do the same for a session's asks.

A domain's ``on_start`` hook may name a page's sign-ins, refuse them, or
ask more or less of them than the settings do (``SignInPolicy``, ADR
0155).

A signed-in web session (credentials without scopes) asks the user's
phone for more (ADR 0129), counted per user against ``START_RATE``; only
that session redeems the answer, or withdraws the request:

- ``POST /app-sign-in/authorizations/{authorization_id}``: confirm one of
  the session's action authorizations (ADR 0144); the phone is shown the
  act as auth keeps it, and signs its digest.
  ``POST /app-sign-in/authorizations/token`` ``{"request"}`` answers the
  authorization ``<id>.<code>``, for the act's ``Action-Authorization``;
  ``POST /app-sign-in/authorizations/wait`` ``{"request"}`` holds until
  the phone answers, as ``/app-sign-in/wait`` does.
- ``POST /app-sign-in/transactions/{challenge_id}``: confirm one of the
  user's ``auth.sca`` transactions; the phone is shown its amount and
  payee. ``POST /app-sign-in/transactions/token`` ``{"request"}``
  answers its authorization ``<id>.<code>``, as ``/sca/{id}/confirm``.
- ``POST /app-sign-in/step-up`` ``{"message"}``, deprecated in 2.4 and
  removed in 3.0: approve what the page's message says.
  ``POST /app-sign-in/step-up/token`` ``{"request"}`` steps the session
  up, ``amr`` ``["hwk"]``, once approved: a window for every guarded act.

The phone (a user's credentials without scopes, and a request signed by
one of their bound devices, ``DeviceSigned``):

- ``GET /app-sign-in/requests``: the requests waiting for an answer, with
  who asks and from where.
- ``POST /app-sign-in/requests/claim`` ``{"code"}``: the QR request scanned.
- ``POST /app-sign-in/requests/{id}/approve`` ``{"match_code"?}``, and an
  act's ``{"digest"}``.
- ``POST /app-sign-in/requests/{id}/deny`` ``{"report"?}``.

A new phone is bound from one the user bound before, which scans it
(ADR 0137):

- ``POST /app-sign-in/device`` ``{"name", "public_key"}``, signed by the
  new key: a QR code for the new phone to show. Nothing reaches the user's
  phone until it scans the code (``/app-sign-in/requests/claim``), and
  approves.
- ``POST /app-sign-in/device/token`` ``{"request", "public_key"}``, signed
  by the new key: once approved, the key is bound, and the phone logged in.
"""

import base64
import functools
import hashlib
import hmac
import warnings
from contextlib import suppress
from datetime import UTC, date, datetime
from typing import Annotated, Final, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.exceptions import HTTPException

from domainkit import deadlines
from domainkit.contrib.auth import AbstractBaseUser
from domainkit.contrib.auth.actions import ActionAuthorizationOut, AuthorizationNotFound
from domainkit.contrib.auth.authentication import Credentials, Proof
from domainkit.contrib.auth.captcha import captcha
from domainkit.contrib.auth.integration import (
    ActionAuthorization,
    AuthorizationChanged,
    AuthorizationNeedsOtherFactors,
    CsrfOut,
    LoginMethod,
    ReauthenticationRequired,
    StepUpOut,
    TokenOut,
    action_authorization,
    action_for,
    address_key,
    attempt,
    auth_settings,
    check_origin,
    confirm_action_with,
    confirm_transaction_with,
    confirmed_action_authorization,
    impersonated,
    issue_login,
    link_action_authorization,
    linked_action_authorization,
    note_login_sign_in,
    pass_login_gates,
    record_step_up,
    sca_transaction,
    start_browser_session,
    step_up_answer,
    stepping_credentials,
)
from domainkit.contrib.auth.models import UserId
from domainkit.contrib.devices import (
    BoundDevice,
    DeviceSignatureRefused,
    DeviceSigned,
    DevicesUnitOfWork,
)
from domainkit.contrib.devices.binding import (
    bind_key,
    device_key,
    mark_signed_by,
    proves_key,
    signed_message,
    signing_device,
)
from domainkit.ddd import DomainError, NotFound
from domainkit.deprecation import DomainkitDeprecationWarning
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.access import IsUnscoped, Public, Require
from domainkit.http.errors import error_responses
from domainkit.http.versioning import Deprecation
from domainkit.money import Money

from .exceptions import (
    DigestRequired,
    SignInNotFound,
    SignInUnknown,
    TransactionClosed,
    TransactionNeedsOtherFactors,
    WrongMatchCode,
)
from .gate import mark_approved
from .hooks import SignInPolicy, on_approve, on_start
from .services import (
    PHONE_CONFIRMS,
    Requester,
    Started,
    approve,
    ask_signed_in,
    cancel,
    ciba_settings,
    claim,
    deny,
    matches,
    poll,
    secret_digest,
    start_push,
    start_qr,
    wait,
    waiting,
)
from .stores import (
    AUTHORIZATION,
    DEVICE,
    QR,
    STEP_UP,
    TRANSACTION,
    CibaUnitOfWork,
    SignInRequest,
)

__all__ = ["count_start", "router"]

router = APIRouter(tags=["sign-in approvals"])
DEVICE_SIGNED = Depends(Require(IsUnscoped(), DeviceSigned()))
SIGNED_IN = Depends(Require(IsUnscoped()))
MESSAGE_LENGTH: Final = 64
"""Characters a step-up's message may have: the phone shows it whole."""
HWK: Final = frozenset({"hwk"})
"""What the phone's approval proves: a key in its hardware (RFC 8176)."""
HANDLE_LENGTH: Final = 100
"""Characters a poll handle may be sent with: 43 are made."""
CODE_LENGTH: Final = 400
"""Characters a scanned QR code may hold: ``QR_FORMAT`` and its 43."""
STARTS_THROTTLED: Final = "Too many sign-ins were started; try again later."
ASKS_THROTTLED: Final = "Too many requests were sent to your phone; try again later."
PHONE_COVERS: Final = frozenset({"otp", "phone"})
"""What an act may ask the phone's approval to prove: ``hwk`` stands for
an authenticator's code (``otp``), never for the password (``pwd``)."""
DEVICE_TIME: Final = "x-device-time"
DEVICE_SIGNATURE: Final = "x-device-signature"
"""The headers of a device's signature (``devices.binding``)."""
FREE_TEXT_STEP_UP: Final = Deprecation(
    since=date(2026, 11, 15),
    link="https://github.com/karimmattar/domainkit/blob/main/docs/auth/ciba.md"
    "#the-pages-words-and-a-window-deprecated",
)
"""``POST /app-sign-in/step-up``, its token and wait: deprecated in 2.4, gone in 3.0."""
FREE_TEXT_STEP_UP_WARNING: Final = (
    "POST /app-sign-in/step-up is deprecated: the phone shows the page's words, "
    "and its approval opens a window for every guarded act. Ask for an action "
    "authorization of the act, and have the phone confirm it "
    "(POST /app-sign-in/authorizations/{id}); see docs/auth/actions.md. The "
    "route is removed in domainkit 3.0."
)


class IdentifierIn(BaseModel):
    """Who signs in: an email address, a mobile number, or a username."""

    model_config = ConfigDict(extra="forbid")

    identifier: str = Field(min_length=1, max_length=255)


class DeviceRequestIn(BaseModel):
    """The phone that asks to be bound: a name for it, and the key it holds.

    It names no account: the phone that scans its code decides whose it is.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=MESSAGE_LENGTH)
    """What the user's bound phone is shown: "Omar's new phone"."""
    public_key: str = Field(min_length=1, max_length=1000)


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
    qr_expires_in: int | None = None
    """Seconds the QR code can be scanned: then start a new one. Once
    scanned, keep polling: the phone has ``expires_in`` to answer."""


class WaitOut(BaseModel):
    """What a wait found: the phone approved, or has not answered yet."""

    status: Literal["approved", "pending"]
    """``approved``: redeem it now (``…/token``); ``pending``: wait again."""


def _wait_seconds() -> float:
    """``WAIT_SECONDS``, within the request's deadline less a second to answer."""
    seconds = float(ciba_settings().WAIT_SECONDS)
    return max(deadlines.remaining(at_most=seconds + 1.0) - 1.0, 0.0)


def _waited(approved: bool) -> WaitOut:
    """The answer of a wait."""
    return WaitOut(status="approved" if approved else "pending")


class PollIn(BaseModel):
    """The poll handle a start answered."""

    model_config = ConfigDict(extra="forbid")

    request: str = Field(min_length=1, max_length=HANDLE_LENGTH, repr=False)


class DeviceRedeemIn(PollIn):
    """The poll handle, and the key the request was made for."""

    public_key: str = Field(min_length=1, max_length=1000)


class BoundDeviceOut(BaseModel):
    """The device bound: its id signs the phone's requests from now on."""

    id: UUID
    name: str
    created_at: datetime


class DeviceBoundOut(TokenOut):
    """The device bound, and the tokens of the login it made."""

    device: BoundDeviceOut


class ClaimIn(BaseModel):
    """What the QR code the phone scanned holds."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=CODE_LENGTH, repr=False)


class ApproveIn(BaseModel):
    """The number the sign-in page shows, when it shows one; an act's digest."""

    model_config = ConfigDict(extra="forbid")

    match_code: str | None = Field(default=None, max_length=10)
    digest: str | None = Field(default=None, min_length=1, max_length=64)
    """Needed for an act (kind ``authorization``), and for it only: the
    digest the phone was shown, which the device's signature then covers."""


class DenyIn(BaseModel):
    """Whether the user did not start it: someone who knows them did."""

    model_config = ConfigDict(extra="forbid")

    report: bool = False


class StepUpIn(BaseModel):
    """What the phone is asked to approve, in words the user recognizes."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=MESSAGE_LENGTH)
    """Such as "Transfer 250.00 EUR to DE89…": printable characters only."""

    @field_validator("message")
    @classmethod
    def _printable(cls, message: str) -> str:
        """Refuse control characters: the phone shows the message as is."""
        if not message.isprintable() or not message.strip():
            msg = "A message is printable characters, not only spaces."
            raise ValueError(msg)
        return message


class TransactionOut(BaseModel):
    """The transaction the phone is asked to confirm, as auth keeps it."""

    amount: Money
    payee: str
    purpose: str


class ActOut(ActionAuthorizationOut):
    """The act the phone is asked to confirm, as auth keeps it, and its digest.

    ``purpose``, and ``shown``: the title and details the server built when
    the authorization was asked for, never words a page sent.
    """

    digest: str
    """Send it back to approve: the device's signature then covers it."""


class AuthorizationOut(BaseModel):
    """The transaction's authorization: hand it to the use case that acts, once."""

    authorization: str = Field(repr=False)
    expires_at: datetime


class SignInOut(BaseModel):
    """A request waiting for the phone's answer: what, who asks, from where, when."""

    id: UUID
    kind: str
    """``sign_in``, ``step_up`` (``binding_message`` says what for),
    ``transaction`` (``transaction`` says what), ``authorization``
    (``authorization`` says what), or ``device``: a phone asks to be
    bound, and ``binding_message`` is the name it gave."""
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
    transaction: TransactionOut | None = None
    """A transaction's amount, payee and purpose, read from auth's record."""
    authorization: ActOut | None = None
    """An act's purpose, title, details and digest, read from auth's record."""
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


async def count_start(
    request: Request, *, user: AbstractBaseUser | None = None
) -> None:
    """Count a start against ``START_RATE``: per client address, or per ``user``.

    For a project's own start routes too, so they share the routes' count.
    A page's sign-in names no one yet, and is counted per address. What a
    signed-in session asks ``user``'s phone (an act, a transaction) is
    counted per user, from any address: the people behind one address, an
    office's, each have the rate (user report A15).

    Raises:
        APIError: 429 past the rate.
    """
    rate = ciba_settings().START_RATE
    if user is not None:
        key = f"ciba-ask:user:{user.id}"
        await attempt(request, [(key, rate)], action="ciba.ask", detail=ASKS_THROTTLED)
        return
    key = address_key(request, "ciba-start")
    await attempt(request, [(key, rate)], action="ciba.start", detail=STARTS_THROTTLED)


async def _start_policy(request: Request) -> SignInPolicy:
    """What the domains' ``on_start`` hooks ask of the page's sign-in; they may refuse.

    Every hook runs. The first binding message given is kept. Number
    matching is asked if a hook asks it, and the same network required if
    a hook requires it: a hook relaxes a setting only where none asks more.
    A field no hook gives stays None, for the setting to decide.

    Raises:
        ImproperlyConfigured: A hook gave a message the phone cannot show,
            or something other than a message, a ``SignInPolicy``, or None.
    """
    message: str | None = None
    matching: set[bool] = set()
    networks: set[Literal["warn", "require"]] = set()
    for hook in on_start:
        given = await hook.call(request)
        if isinstance(given, str):
            given = SignInPolicy(binding_message=given)
        if given is None:
            continue
        if not isinstance(given, SignInPolicy) or not _policy_holds(given):
            raise ImproperlyConfigured(
                f"{hook.domain}'s on_start hook gave {given!r}: return a binding "
                "message, a SignInPolicy (number_matching a bool, same_network "
                '"warn" or "require"), or None.'
            )
        if given.binding_message is not None and message is None:
            if not _showable(given.binding_message):
                raise ImproperlyConfigured(
                    f"{hook.domain}'s on_start hook gave a message the phone "
                    f"cannot show: 1 to {MESSAGE_LENGTH} printable characters."
                )
            message = given.binding_message
        if given.number_matching is not None:
            matching.add(given.number_matching)
        if given.same_network is not None:
            networks.add(given.same_network)
    return SignInPolicy(
        binding_message=message,
        number_matching=(True in matching) if matching else None,
        same_network=(
            ("require" if "require" in networks else "warn") if networks else None
        ),
    )


def _policy_holds(policy: SignInPolicy) -> bool:
    """Whether a hook's policy holds values the start can apply."""
    return (
        (policy.binding_message is None or isinstance(policy.binding_message, str))
        and (policy.number_matching is None or isinstance(policy.number_matching, bool))
        and policy.same_network in (None, "warn", "require")
    )


def _showable(message: str) -> bool:
    """Whether the phone can show ``message`` whole: printable, 1 to 64 characters."""
    return message.isprintable() and 0 < len(message.strip()) <= MESSAGE_LENGTH


def _matching(policy: SignInPolicy, setting: bool) -> bool:
    """Whether the start asks the number: the page's policy, else ``setting``."""
    return setting if policy.number_matching is None else policy.number_matching


def _started(started: Started) -> StartedOut:
    """What a start answers."""
    return StartedOut(
        request=started.handle,
        expires_in=started.expires_in,
        interval=started.interval,
        match_code=started.match_code,
        qr=started.qr,
        qr_expires_in=started.qr_expires_in,
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
    await count_start(request)
    policy = await _start_policy(request)
    async with CibaUnitOfWork() as uow:
        user = await uow.users.find_by_identifier(body.identifier)
        started = await start_push(
            uow,
            user,
            requester=_requester(request),
            match=_matching(policy, ciba_settings().NUMBER_MATCHING),
            binding_message=policy.binding_message,
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
    """A QR code for the phone to scan; ask for a new one when it expires.

    With a ``match_code``, show it beside the code: the phone that scanned
    it types it to approve.
    """
    _no_store(response)
    await count_start(request)
    policy = await _start_policy(request)
    async with CibaUnitOfWork() as uow:
        started = await start_qr(
            uow,
            requester=_requester(request),
            binding_message=policy.binding_message,
            match=_matching(policy, ciba_settings().QR_NUMBER_MATCHING),
            same_network=policy.same_network,
        )
        await uow.commit()
    return _started(started)


def _key_digest(key: bytes) -> str:
    """What a device request keeps of the key it is for: its SHA-256, in hex."""
    return hashlib.sha256(key).hexdigest()


@router.post(
    "/app-sign-in/device",
    dependencies=[Public, captcha("login")],
    response_model_exclude_none=True,
    responses=error_responses(
        status.HTTP_401_UNAUTHORIZED, status.HTTP_429_TOO_MANY_REQUESTS
    ),
)
async def ask_to_bind_device(
    body: DeviceRequestIn, request: Request, response: Response
) -> StartedOut:
    """A QR code for this phone to show, for a phone the user bound to scan.

    The request is signed by the key it asks to bind. It names no account
    and pushes nothing: the user opens the app on a phone they bound,
    scans the code (``/app-sign-in/requests/claim``), is shown the name and
    where the request comes from, and approves. A user with no bound phone
    enrolls instead (``/devices/enroll``).

    Raises:
        DeviceSignatureRefused: The key did not sign the request.
    """
    _no_store(response)
    await count_start(request)
    algorithm, key = device_key(body.public_key)
    if not await proves_key(request, algorithm, key):
        raise DeviceSignatureRefused()
    async with CibaUnitOfWork() as uow:
        started = await start_qr(
            uow,
            requester=_requester(request),
            binding_message=body.name,
            kind=DEVICE,
            credential_digest=_key_digest(key),
        )
        await uow.commit()
    return _started(started)


@router.post(
    "/app-sign-in/device/token",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Public],
    response_model_exclude_none=True,
    responses=error_responses(
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_409_CONFLICT,
    ),
)
async def bind_approved_device(
    body: DeviceRedeemIn, request: Request, response: Response
) -> DeviceBoundOut:
    """Bind the key, once the user's phone approved, and log this phone in.

    Signed by the key again: only its holder redeems the request made for
    it. The login is the new device's own, and runs every login gate.

    Raises:
        DeviceSignatureRefused: The key did not sign the request.
        PollRefused: Not approved (yet), or not this key's to redeem.
        TooManyKeys: The user holds ``DEVICES.MAX_DEVICES`` already.
    """
    _no_store(response)
    algorithm, key = device_key(body.public_key)
    if not await proves_key(request, algorithm, key):
        raise DeviceSignatureRefused()
    approved = await poll(
        body.request, client_id=None, kind=DEVICE, credential_digest=_key_digest(key)
    )
    async with CibaUnitOfWork() as uow:
        user = (
            None
            if approved.user_id is None
            else await uow.users.find(UserId(approved.user_id))
        )
        if user is None or not user.is_active:
            raise SignInUnknown()
        device = await bind_key(
            uow, user.id, approved.binding_message or "", algorithm, key
        )
        await uow.commit()
    mark_signed_by(request, device)  # The login that follows is its own.
    mark_approved(request, user)
    await pass_login_gates(
        request, user, method=LoginMethod.DEVICE, amr=HWK, device_id=device.id
    )
    issued = await issue_login(
        user,
        factor_proven=True,
        method=LoginMethod.DEVICE,
        device_id=device.id,
        amr=HWK,
    )
    return DeviceBoundOut(
        access_token=issued.access_token,
        expires_in=issued.expires_in,
        refresh_token=issued.refresh_token,
        device=BoundDeviceOut(
            id=device.id, name=device.name, created_at=device.created_at
        ),
    )


async def _approved_user(
    request: Request, body: PollIn
) -> tuple[AbstractBaseUser, str]:
    """The user whose phone approved the page's request, redeemed now, and how.

    How: ``LoginMethod.PUSH`` or ``QR``, the request's channel. The login
    notes the request it redeems, for its gates (``note_login_sign_in``).

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
    note_login_sign_in(request, approved.id, approved.binding_message)
    method = LoginMethod.QR if approved.channel == QR else LoginMethod.PUSH
    return user, method


@router.post(
    "/app-sign-in/token",
    dependencies=[Public],
    response_model_exclude_none=True,
    responses=error_responses(status.HTTP_400_BAD_REQUEST),
)
async def redeem_sign_in(
    body: PollIn, request: Request, response: Response
) -> TokenOut:
    """Tokens, once the phone approved; until then, why not.

    The login runs every login gate, as ``/login`` does; the phone's
    approval stands for its device's signature.
    """
    _no_store(response)
    user, method = await _approved_user(request, body)
    mark_approved(request, user)
    await pass_login_gates(request, user, method=method, amr=HWK)
    issued = await issue_login(user, factor_proven=True, method=method, amr=HWK)
    return TokenOut(
        access_token=issued.access_token,
        expires_in=issued.expires_in,
        refresh_token=issued.refresh_token,
    )


@router.post(
    "/app-sign-in/wait",
    dependencies=[Public],
    responses=error_responses(status.HTTP_400_BAD_REQUEST),
)
async def wait_for_sign_in(body: PollIn, response: Response) -> WaitOut:
    """Hold until the phone answers the page's sign-in, at most ``WAIT_SECONDS``.

    Instead of polling (user report L2): ``approved`` comes as soon as the
    phone approves; the page then redeems it (``/token``, ``/session``).
    Denied, expired, or unknown answer as a poll does. It never redeems,
    nor counts as a poll.
    """
    _no_store(response)
    return _waited(await wait(body.request, seconds=_wait_seconds()))


@router.post(
    "/app-sign-in/cancel",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Public],
    responses=error_responses(status.HTTP_400_BAD_REQUEST),
)
async def cancel_sign_in(body: PollIn) -> None:
    """Withdraw the page's sign-in, as its dialog closes (user report A16).

    It ends now: the phone no longer lists it, nor can answer it, and it
    no longer counts against ``MAX_PENDING``. Nobody redeems it, and
    nothing is reported. A sign-in answered or ended already answers the
    same; an unknown or redeemed handle, ``sign_in_unknown``.
    """
    await cancel(body.request, client_id=None)


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
    user, method = await _approved_user(request, body)
    mark_approved(request, user)
    await pass_login_gates(request, user, method=method, amr=HWK)
    return await start_browser_session(request, user, response, method=method, amr=HWK)


async def _device(request: Request, user: AbstractBaseUser) -> BoundDevice:
    """The bound device that signed the request (``DEVICE_SIGNED`` checked it)."""
    device = await signing_device(request, user)
    assert device is not None  # noqa: S101 - DeviceSigned allowed the request.
    return device


async def _out(
    uow: CibaUnitOfWork, request: Request, sign_ins: list[SignInRequest]
) -> list[SignInOut]:
    """What the phone is shown of each request."""
    names: dict[str, str] = {}
    transactions: dict[UUID, TransactionOut | None] = {}
    acts: dict[UUID, ActOut | None] = {}
    for each in sign_ins:
        if each.client_id is not None and each.client_id not in names:
            client = await uow.clients.get(each.client_id)
            names[each.client_id] = each.client_id if client is None else client.name
        if each.sca_challenge_id is not None and each.user_id is not None:
            transactions[each.id] = await _transaction(
                each.sca_challenge_id, each.user_id
            )
        if each.kind == AUTHORIZATION and each.user_id is not None:
            acts[each.id] = await _act(each.id, each.user_id)
    here = _address(request)
    return [
        SignInOut(
            id=each.id,
            kind=each.kind,
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
            transaction=transactions.get(each.id),
            authorization=acts.get(each.id),
            created_at=each.created_at,
            expires_at=each.expires_at,
        )
        for each in sign_ins
    ]


async def _transaction(challenge_id: UUID, user_id: UUID) -> TransactionOut | None:
    """What the phone is shown of a transaction; None if auth no longer has it."""
    try:
        challenge = await sca_transaction(challenge_id, user_id)
    except NotFound:
        return None
    return TransactionOut(
        amount=challenge.amount, payee=challenge.payee, purpose=challenge.purpose
    )


async def _act(request_id: UUID, user_id: UUID) -> ActOut | None:
    """What the phone is shown of an act; None if auth no longer links one."""
    try:
        authorization = await linked_action_authorization(request_id, user_id)
    except AuthorizationNotFound:
        return None
    return ActOut(
        **ActionAuthorizationOut.of(authorization).model_dump(),
        digest=authorization.digest,
    )


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
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
    ),
)
async def approve_sign_in(
    request_id: UUID,
    body: ApproveIn,
    request: Request,
    user: Annotated[AbstractBaseUser, DEVICE_SIGNED],
) -> None:
    """Approve the sign-in from this device; a wrong number denies it.

    An act (kind ``authorization``) is approved with the digest the phone
    was shown: auth confirms its authorization with the device's key, the
    bytes it signed, and its signature, kept to check it again. Another
    digest denies the request (``409 authorization_changed``). An
    ``on_approve`` hook's refusal denies it too, and is answered.
    """
    device = await _device(request, user)
    async with CibaUnitOfWork() as uow:
        asked = await waiting(uow, user.id, request_id)
    try:
        for hook in on_approve:  # Raising refuses it: denied, nothing confirmed.
            await hook.call(request, user, asked)
    except (DomainError, HTTPException) as refusal:
        if not isinstance(refusal, HTTPException) or refusal.status_code < 500:
            await _deny_refused(user, asked)
        raise
    if asked.kind == AUTHORIZATION and matches(asked, body.match_code):
        await _confirm_act(request, user, asked, device, body.digest)
    async with CibaUnitOfWork() as uow:
        approved = await approve(
            uow, user.id, request_id, device.id, match_code=body.match_code
        )
        await uow.commit()  # A wrong number denies it, then is refused.
    if approved is None:
        raise WrongMatchCode()


async def _deny_refused(user: AbstractBaseUser, asked: SignInRequest) -> None:
    """Deny the request an ``on_approve`` hook refused; report nothing.

    Its initiator's poll and wait answer ``access_denied`` at once, rather
    than when it expires (user report A17). A request answered or ended
    meanwhile (a hook that denied it itself) stays as it is. A hook's own
    failure (an error of 500 or more) refuses nothing: it denies nothing.
    """
    with suppress(SignInNotFound):
        async with CibaUnitOfWork() as uow:
            await deny(uow, user.id, asked.id)
            await uow.commit()


async def _confirm_act(
    request: Request,
    user: AbstractBaseUser,
    asked: SignInRequest,
    device: BoundDevice,
    digest: str | None,
) -> None:
    """Confirm the act's authorization, signed by ``device``; deny it if it changed.

    Each step is a transaction of its own: auth's confirmation commits
    before the request is approved. Should that approval then lose (the
    request answered or ended meanwhile), the authorization is confirmed
    and nobody can redeem it.

    Raises:
        DigestRequired: The phone sent no digest.
        AuthorizationNotFound: Auth no longer links an authorization to it.
        AuthorizationChanged: Not the act's digest: the request is denied.
        AuthorizationNeedsOtherFactors: The act asks more than the phone proves.
        APIError: 409 ``authorization_confirmed`` or ``authorization_expired``.
    """
    if digest is None:
        raise DigestRequired()
    authorization = await linked_action_authorization(
        asked.id, user.id, login=asked.credential_digest
    )
    if not hmac.compare_digest(digest.encode(), authorization.digest.encode()):
        async with CibaUnitOfWork() as uow:
            await deny(uow, user.id, asked.id)
            await uow.commit()
        raise AuthorizationChanged
    signed_at, signature, signed = await _signed(request)
    await confirm_action_with(
        authorization.id,
        PHONE_CONFIRMS,
        user_id=user.id,
        login=asked.credential_digest,
        digest=digest,
        device_id=device.id,
        algorithm=device.algorithm,
        public_key=device.public_key,
        signature=signature,
        signed=signed,
        signed_at=signed_at,
    )


async def _signed(request: Request) -> tuple[datetime, bytes, bytes]:
    """When the device signed ``request``, its signature, and the bytes it signed.

    The message ``devices.binding`` verified (``signed_message``): the
    method, the path with its query as sent, the time, and the body's
    digest. ``DEVICE_SIGNED`` checked the headers already.
    """
    time = int(request.headers[DEVICE_TIME])
    encoded = request.headers[DEVICE_SIGNATURE]
    signature = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    target = request.scope.get("raw_path", b"").decode("latin-1") or request.url.path
    if query := request.scope.get("query_string", b""):
        target = f"{target}?{query.decode('latin-1')}"
    signed = signed_message(request.method, target, time, await request.body())
    return datetime.fromtimestamp(time / 1000, UTC), signature, signed


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


def _session_of(request: Request) -> tuple[Credentials, str]:
    """The session's credentials, and the digest a request is bound to.

    Raises:
        ReauthenticationRequired: Staff act as the user: the phone is theirs.
        APIError: 400 ``step_up_unsupported``: the credentials cannot keep
            a step-up, nor be told apart.
    """
    if impersonated(request):
        raise ReauthenticationRequired
    credentials = stepping_credentials(request)
    held = credentials.token_id or str(credentials.login)
    return credentials, secret_digest(f"{credentials.scheme}:{held}")


async def _ask(
    request: Request,
    user: AbstractBaseUser,
    digest: str,
    *,
    kind: str,
    message: str | None = None,
    sca_challenge_id: UUID | None = None,
) -> StartedOut:
    """Ask the user's phone, counted against ``START_RATE`` per user."""
    await count_start(request, user=user)
    async with CibaUnitOfWork() as uow:
        started = await ask_signed_in(
            uow,
            user,
            kind=kind,
            credential_digest=digest,
            requester=_requester(request),
            message=message,
            sca_challenge_id=sca_challenge_id,
            match=ciba_settings().NUMBER_MATCHING,
        )
        await uow.commit()
    return _started(started)


async def _redeemed(body: PollIn, digest: str, kind: str) -> SignInRequest:
    """The request of ``kind`` this session asked, approved, and redeemed now.

    The session's credentials are the user's: a request bound to them is
    theirs.

    Raises:
        PollRefused: Not approved (yet), or not this session's to redeem.
    """
    return await poll(body.request, client_id=None, kind=kind, credential_digest=digest)


@functools.cache
def _warn_free_text_step_up() -> None:
    """Warn that the free-text step-up goes in 3.0: once a process.

    Warned again while the warning is an error (a project's tests), so
    each use fails there.
    """
    warnings.warn(FREE_TEXT_STEP_UP_WARNING, DomainkitDeprecationWarning, stacklevel=2)


@router.post(
    "/app-sign-in/step-up",
    response_model_exclude_none=True,
    dependencies=[Depends(FREE_TEXT_STEP_UP)],
    responses=error_responses(
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_409_CONFLICT,
        status.HTTP_429_TOO_MANY_REQUESTS,
    ),
)
async def ask_phone_step_up(
    body: StepUpIn,
    request: Request,
    response: Response,
    user: Annotated[AbstractBaseUser, SIGNED_IN],
) -> StartedOut:
    """Ask the user's phone to approve what ``message`` says, for this session.

    Deprecated in 2.4, removed in 3.0: the phone shows the page's words,
    and the approval opens a window for every guarded act. Ask for an
    action authorization, and ``/app-sign-in/authorizations/{id}``.
    """
    _warn_free_text_step_up()
    _no_store(response)
    _, digest = _session_of(request)
    return await _ask(request, user, digest, kind=STEP_UP, message=body.message)


@router.post(
    "/app-sign-in/step-up/wait",
    dependencies=[Depends(FREE_TEXT_STEP_UP)],
    responses=error_responses(
        status.HTTP_400_BAD_REQUEST, status.HTTP_401_UNAUTHORIZED
    ),
)
async def wait_for_phone_step_up(
    body: PollIn,
    request: Request,
    response: Response,
    user: Annotated[AbstractBaseUser, SIGNED_IN],
) -> WaitOut:
    """Hold until the phone answers this session's step-up (``/wait``'s rules)."""
    _no_store(response)
    _, digest = _session_of(request)
    approved = await wait(
        body.request, seconds=_wait_seconds(), kind=STEP_UP, credential_digest=digest
    )
    return _waited(approved)


@router.post(
    "/app-sign-in/transactions/wait",
    responses=error_responses(
        status.HTTP_400_BAD_REQUEST, status.HTTP_401_UNAUTHORIZED
    ),
)
async def wait_for_phone_transaction(  # Before /{challenge_id}, which "wait" matches.
    body: PollIn,
    request: Request,
    response: Response,
    user: Annotated[AbstractBaseUser, SIGNED_IN],
) -> WaitOut:
    """Hold until the phone answers this session's transaction (``/wait``'s rules)."""
    _no_store(response)
    _, digest = _session_of(request)
    approved = await wait(
        body.request,
        seconds=_wait_seconds(),
        kind=TRANSACTION,
        credential_digest=digest,
    )
    return _waited(approved)


@router.post(
    "/app-sign-in/step-up/token",
    response_model_exclude_none=True,
    dependencies=[Depends(FREE_TEXT_STEP_UP)],
    responses=error_responses(
        status.HTTP_400_BAD_REQUEST, status.HTTP_401_UNAUTHORIZED
    ),
)
async def redeem_phone_step_up(
    body: PollIn,
    request: Request,
    response: Response,
    user: Annotated[AbstractBaseUser, SIGNED_IN],
) -> StepUpOut:
    """Step this session up, ``hwk``, once the phone approved; until then, why not."""
    _no_store(response)
    credentials, digest = _session_of(request)
    approved = await _redeemed(body, digest, STEP_UP)
    proof = Proof(approved.approved_at or approved.created_at, HWK)
    async with DevicesUnitOfWork() as uow:
        await record_step_up(uow, credentials, user, proof)
        await uow.commit()
    return step_up_answer(credentials, proof)


@router.post(
    "/app-sign-in/transactions/cancel",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=error_responses(
        status.HTTP_400_BAD_REQUEST, status.HTTP_401_UNAUTHORIZED
    ),
)
async def cancel_phone_transaction(  # Before /{challenge_id}, which "cancel" matches.
    body: PollIn,
    request: Request,
    user: Annotated[AbstractBaseUser, SIGNED_IN],
) -> None:
    """Withdraw what this session asked the phone of a transaction (``/cancel``).

    The transaction stays as it is: ask the phone again, or confirm it
    another way.
    """
    _, digest = _session_of(request)
    await cancel(
        body.request, client_id=None, kind=TRANSACTION, credential_digest=digest
    )


@router.post(
    "/app-sign-in/transactions/token",
    responses=error_responses(
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
    ),
)
async def redeem_phone_transaction(  # Before /{challenge_id}, which "token" matches.
    body: PollIn,
    request: Request,
    response: Response,
    user: Annotated[AbstractBaseUser, SIGNED_IN],
) -> AuthorizationOut:
    """The transaction's authorization, once the phone confirmed it."""
    _no_store(response)
    _, digest = _session_of(request)
    approved = await _redeemed(body, digest, TRANSACTION)
    assert approved.sca_challenge_id is not None  # noqa: S101 - a transaction's.
    challenge = await sca_transaction(approved.sca_challenge_id, user.id)
    authorization = await confirm_transaction_with(challenge, HWK)
    return AuthorizationOut(
        authorization=authorization, expires_at=challenge.expires_at
    )


@router.post(
    "/app-sign-in/transactions/{challenge_id}",
    response_model_exclude_none=True,
    responses=error_responses(
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
        status.HTTP_429_TOO_MANY_REQUESTS,
    ),
)
async def ask_phone_transaction(
    challenge_id: UUID,
    request: Request,
    response: Response,
    user: Annotated[AbstractBaseUser, SIGNED_IN],
) -> StartedOut:
    """Ask the user's phone to confirm one of their transactions."""
    _no_store(response)
    _, digest = _session_of(request)
    challenge = await sca_transaction(challenge_id, user.id)
    if set(auth_settings().SCA.METHODS) - {"otp"}:  # hwk stands for otp alone.
        raise TransactionNeedsOtherFactors()
    if (
        challenge.confirmed_at is not None
        or challenge.consumed_at is not None
        or challenge.expired()
    ):
        raise TransactionClosed()
    return await _ask(
        request, user, digest, kind=TRANSACTION, sca_challenge_id=challenge.id
    )


def _asks(authorization: ActionAuthorization) -> frozenset[str]:
    """What confirming ``authorization`` must prove: its action's, or the setting's."""
    action = action_for(authorization.purpose)
    if action is not None:
        return action.required()
    return frozenset(auth_settings().AUTHORIZATIONS.METHODS)


@router.post(
    "/app-sign-in/authorizations/wait",
    responses=error_responses(
        status.HTTP_400_BAD_REQUEST, status.HTTP_401_UNAUTHORIZED
    ),
)
async def wait_for_phone_authorization(  # Before /{authorization_id}: "wait" matches.
    body: PollIn,
    request: Request,
    response: Response,
    user: Annotated[AbstractBaseUser, SIGNED_IN],
) -> WaitOut:
    """Hold until the phone answers this session's act (``/wait``'s rules).

    It never hands over the authorization: ``…/authorizations/token`` does.
    """
    _no_store(response)
    _, digest = _session_of(request)
    approved = await wait(
        body.request,
        seconds=_wait_seconds(),
        kind=AUTHORIZATION,
        credential_digest=digest,
    )
    return _waited(approved)


@router.post(
    "/app-sign-in/authorizations/cancel",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=error_responses(
        status.HTTP_400_BAD_REQUEST, status.HTTP_401_UNAUTHORIZED
    ),
)
async def cancel_phone_authorization(  # Before /{authorization_id}: "cancel" matches.
    body: PollIn,
    request: Request,
    user: Annotated[AbstractBaseUser, SIGNED_IN],
) -> None:
    """Withdraw what this session asked the phone of an act (``/cancel``).

    The act's authorization stays as it is: ask the phone again, or
    confirm it another way.
    """
    _, digest = _session_of(request)
    await cancel(
        body.request, client_id=None, kind=AUTHORIZATION, credential_digest=digest
    )


@router.post(
    "/app-sign-in/authorizations/token",
    responses=error_responses(
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
    ),
)
async def redeem_phone_authorization(  # Before /{authorization_id}: "token" matches.
    body: PollIn,
    request: Request,
    response: Response,
    user: Annotated[AbstractBaseUser, SIGNED_IN],
) -> AuthorizationOut:
    """The act's authorization, ``<id>.<code>``, once the phone confirmed it.

    Only the session that asked redeems it, once; until then, a poll's
    answers.
    """
    _no_store(response)
    _, digest = _session_of(request)
    approved = await _redeemed(body, digest, AUTHORIZATION)
    authorization = await linked_action_authorization(
        approved.id, user.id, login=digest
    )
    issued = await confirmed_action_authorization(
        authorization.id, user.id, login=digest
    )
    return AuthorizationOut(authorization=issued, expires_at=authorization.expires_at)


@router.post(
    "/app-sign-in/authorizations/{authorization_id}",
    response_model_exclude_none=True,
    responses=error_responses(
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
        status.HTTP_429_TOO_MANY_REQUESTS,
    ),
)
async def ask_phone_authorization(
    authorization_id: UUID,
    request: Request,
    response: Response,
    user: Annotated[AbstractBaseUser, SIGNED_IN],
) -> StartedOut:
    """Ask the user's phone to confirm one of this session's action authorizations.

    Counted and bounded as a step-up is: ``START_RATE`` per user,
    ``MAX_PENDING``, the number under ``NUMBER_MATCHING``. The
    authorization is linked to the request first, so the phone reads the
    act from auth's record.
    """
    _no_store(response)
    _, digest = _session_of(request)
    authorization = await action_authorization(authorization_id, user.id, login=digest)
    if _asks(authorization) - PHONE_COVERS:
        raise AuthorizationNeedsOtherFactors
    await count_start(request, user=user)
    request_id = uuid4()
    await link_action_authorization(
        authorization.id, user_id=user.id, sign_in_request_id=request_id
    )
    async with CibaUnitOfWork() as uow:
        started = await ask_signed_in(
            uow,
            user,
            kind=AUTHORIZATION,
            credential_digest=digest,
            requester=_requester(request),
            match=ciba_settings().NUMBER_MATCHING,
            request_id=request_id,
        )
        await uow.commit()
    return _started(started)
