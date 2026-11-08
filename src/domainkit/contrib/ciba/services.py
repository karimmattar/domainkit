"""Start a sign-in request, let the phone answer it, and poll it (ADR 0128).

A request starts on one of two channels:

- ``start_push``: the initiator names the user; their apps get a push.
- ``start_qr``: the initiator names nobody; its page shows a QR code, and
  the phone that scans it claims the request (``claim``).

``ask_signed_in`` asks the phone of a user who is signed in already: to
approve an action (a step-up), or one of their transactions (ADR 0129).
Only the session that asked redeems it, and a handle redeems as its kind
alone: a step-up is never a login.

The phone answers with ``approve`` or ``deny``; the initiator polls with
``poll`` until it is approved (the request is redeemed, once), denied, or
expired. Each of these but ``poll`` runs in a ``CibaUnitOfWork`` the
caller opens and commits::

    async with CibaUnitOfWork() as uow:
        started = await start_push(uow, user, requester=requester)
        await uow.commit()  # the push leaves through the outbox

    approved = await poll(started.handle, client_id=None)

The initiator gets the poll handle (``auth_req_id``) and nobody else: it is
never in the QR code or the push. A push to a user who is unknown, has no
bound device, has ``MAX_PENDING`` requests waiting, or reported one within
``REPORT_COOLDOWN`` is a decoy: it answers like any other, and nobody can
approve it, so its initiator learns nothing about the user.
"""

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID, uuid4

from domainkit.contrib.auth import AbstractBaseUser
from domainkit.contrib.auth.integration import utc_now
from domainkit.contrib.auth.models import UserId

from .events import SignInApproved, SignInDenied, SignInReported
from .exceptions import (
    AccessDenied,
    AuthorizationPending,
    DifferentNetwork,
    PhoneUnavailable,
    QrCodeExpired,
    SignInExpired,
    SignInNotFound,
    SignInUnknown,
    SlowDown,
)
from .settings import CibaSettings
from .stores import PUSH, QR, SIGN_IN, CibaUnitOfWork, SignInRequest

__all__ = [
    "PUSH_KIND",
    "Requester",
    "Started",
    "approve",
    "ask_signed_in",
    "ciba_settings",
    "claim",
    "deny",
    "poll",
    "start_push",
    "start_qr",
]

SECRET_BYTES: Final = 32
"""Random bytes in a poll handle and a QR code: 256 bits, CIBA asks 128."""
SLOW_DOWN_STEP: Final = 5
"""Seconds a poll that came too soon adds to the interval (CIBA 11)."""
PUSH_KIND: Final = "domainkit.sign_in"
"""The ``type`` of a push's data: the app fetches the request it names."""
AGENT_LENGTH: Final = 200
"""Characters of the initiator's ``User-Agent`` kept, to show on the phone."""


def ciba_settings() -> CibaSettings:
    """``CIBA``, validated (the defaults when it is unset)."""
    from domainkit.apps import registry

    return registry.settings(CibaSettings)


@dataclass(frozen=True, slots=True)
class Requester:
    """Who started a sign-in, as the phone is shown it: an address, a browser."""

    address: str | None
    agent: str | None


@dataclass(frozen=True, slots=True, repr=False)
class Started:
    """A request started: what its initiator keeps, and shows on its page."""

    handle: str
    """The poll handle (``auth_req_id``): only the initiator holds it."""
    request_id: UUID
    expires_in: int
    interval: int
    match_code: str | None = None
    """The two digits the page shows, for the phone to type."""
    qr: str | None = None
    """What the page renders as a QR code."""
    qr_expires_in: int | None = None
    """Seconds the QR code can be scanned; then the page shows a new one.
    Once scanned, the request waits ``REQUEST_TTL`` for the phone's answer:
    ``expires_in`` covers both."""

    def __repr__(self) -> str:
        """The request's id: the handle and the codes stay out of logs."""
        return f"Started(request_id={self.request_id!r})"


def secret_digest(secret: str) -> str:
    """What the table keeps of a handle or a QR code: its SHA-256, in hex.

    The secrets are 256 random bits: a plain hash keeps them out of reach.
    """
    return hashlib.sha256(secret.encode()).hexdigest()


def _request(
    *,
    channel: str,
    handle: str,
    now: datetime,
    ttl: timedelta,
    requester: Requester,
    client_id: str | None,
    scope: str,
    binding_message: str | None,
    user_id: UUID | None = None,
    decoy: bool = False,
    qr_digest: str | None = None,
    match_code: str | None = None,
    kind: str = SIGN_IN,
    credential_digest: str | None = None,
    sca_challenge_id: UUID | None = None,
) -> SignInRequest:
    """A new request, as its row keeps it."""
    agent = requester.agent[:AGENT_LENGTH] if requester.agent else None
    return SignInRequest(
        id=uuid4(),
        handle_digest=secret_digest(handle),
        qr_digest=qr_digest,
        client_id=client_id,
        user_id=user_id,
        channel=channel,
        scope=scope,
        binding_message=binding_message,
        match_code=match_code,
        requester_ip=requester.address,
        requester_agent=agent,
        decoy=decoy,
        created_at=now,
        expires_at=now + ttl,
        interval=ciba_settings().INTERVAL,
        last_polled_at=None,
        claimed_at=None,
        claimed_ip=None,
        approved_at=None,
        device_id=None,
        denied_at=None,
        reported=False,
        redeemed_at=None,
        kind=kind,
        credential_digest=credential_digest,
        sca_challenge_id=sca_challenge_id,
    )


async def start_push(
    uow: CibaUnitOfWork,
    user: AbstractBaseUser | None,
    *,
    requester: Requester,
    client_id: str | None = None,
    scope: str = "",
    binding_message: str | None = None,
    match: bool = False,
) -> Started:
    """Ask ``user``'s phone to approve a sign-in; queue the push to their apps.

    ``user`` is None when the initiator named nobody known: the request is
    a decoy, as for a user who cannot be asked now. With ``match``, the
    phone must type the two digits the page shows (``NUMBER_MATCHING``).
    The push leaves with ``uow``'s commit.
    """
    config = ciba_settings()
    now = utc_now()
    asked = user if user is not None and await _askable(uow, user, now) else None
    handle = secrets.token_urlsafe(SECRET_BYTES)
    match_code = f"{secrets.randbelow(100):02d}" if match else None
    request = await uow.requests.add(
        _request(
            channel=PUSH,
            handle=handle,
            now=now,
            ttl=config.REQUEST_TTL,
            requester=requester,
            client_id=client_id,
            scope=scope,
            binding_message=binding_message,
            user_id=None if asked is None else asked.id,
            decoy=asked is None,
            match_code=match_code,
        )
    )
    if asked is not None:
        await _notify(uow, asked.id, request.id, config)
    return Started(
        handle=handle,
        request_id=request.id,
        expires_in=_seconds(config.REQUEST_TTL),
        interval=request.interval,
        match_code=match_code,
    )


async def ask_signed_in(
    uow: CibaUnitOfWork,
    user: AbstractBaseUser,
    *,
    kind: str,
    credential_digest: str,
    requester: Requester,
    message: str | None = None,
    sca_challenge_id: UUID | None = None,
    match: bool = False,
) -> Started:
    """Ask the phone of ``user``, who is signed in, to approve ``kind``.

    ``kind`` is ``step_up`` (``message`` says what for) or ``transaction``
    (``sca_challenge_id``). Only the credentials ``credential_digest``
    names redeem it. The push leaves with ``uow``'s commit.

    Raises:
        PhoneUnavailable: No phone of the user can be asked now: none is
            bound, ``MAX_PENDING`` requests wait, or the user reported one
            within ``REPORT_COOLDOWN``.
    """
    config = ciba_settings()
    now = utc_now()
    if not await _askable(uow, user, now):
        raise PhoneUnavailable()
    handle = secrets.token_urlsafe(SECRET_BYTES)
    match_code = f"{secrets.randbelow(100):02d}" if match else None
    request = await uow.requests.add(
        _request(
            channel=PUSH,
            handle=handle,
            now=now,
            ttl=config.REQUEST_TTL,
            requester=requester,
            client_id=None,
            scope="",
            binding_message=message,
            user_id=user.id,
            match_code=match_code,
            kind=kind,
            credential_digest=credential_digest,
            sca_challenge_id=sca_challenge_id,
        )
    )
    await _notify(uow, user.id, request.id, config)
    return Started(
        handle=handle,
        request_id=request.id,
        expires_in=_seconds(config.REQUEST_TTL),
        interval=request.interval,
        match_code=match_code,
    )


async def _askable(uow: CibaUnitOfWork, user: AbstractBaseUser, now: datetime) -> bool:
    """Whether a push may reach ``user`` now: active, bound, not flooded, not wary."""
    config = ciba_settings()
    if not user.is_active or not await uow.devices.count(user.id):
        return False
    if await uow.requests.live_pushes(user.id, now) >= config.MAX_PENDING:
        return False
    return not (
        config.REPORT_COOLDOWN
        and await uow.requests.reported_since(user.id, now - config.REPORT_COOLDOWN)
    )


async def _notify(
    uow: CibaUnitOfWork, user_id: UUID, request_id: UUID, config: CibaSettings
) -> None:
    """Queue a push to each of the user's apps, naming the request and no one."""
    from domainkit.contrib.notifications import PushMessage

    for token in await uow.push_tokens.of_user(user_id):
        await PushMessage(
            token=token.token,
            title=config.PUSH_TITLE,
            body=config.PUSH_BODY,
            data={"type": PUSH_KIND, "request_id": str(request_id)},
            platform=token.platform,
        ).send(uow)


async def start_qr(
    uow: CibaUnitOfWork,
    *,
    requester: Requester,
    client_id: str | None = None,
    scope: str = "",
    binding_message: str | None = None,
) -> Started:
    """A request for whichever phone scans its QR code, within ``QR_TTL``.

    The code can be scanned for ``QR_TTL``; the phone that scans it then has
    ``REQUEST_TTL`` to answer, as for a push.
    """
    config = ciba_settings()
    now = utc_now()
    handle = secrets.token_urlsafe(SECRET_BYTES)
    code = secrets.token_urlsafe(SECRET_BYTES)
    request = await uow.requests.add(
        _request(
            channel=QR,
            handle=handle,
            now=now,
            requester=requester,
            client_id=client_id,
            scope=scope,
            binding_message=binding_message,
            qr_digest=secret_digest(code),
            ttl=config.QR_TTL + config.REQUEST_TTL,
        )
    )
    return Started(
        handle=handle,
        request_id=request.id,
        expires_in=_seconds(config.QR_TTL + config.REQUEST_TTL),
        interval=request.interval,
        qr=config.QR_FORMAT.replace("{code}", code),
        qr_expires_in=_seconds(config.QR_TTL),
    )


def unscanned_too_long(request: SignInRequest, now: datetime) -> bool:
    """Whether ``request`` is a QR request no phone scanned within ``QR_TTL``."""
    return (
        request.channel == QR
        and request.claimed_at is None
        and now >= request.created_at + ciba_settings().QR_TTL
    )


async def claim(
    uow: CibaUnitOfWork, user_id: UUID, code: str, *, address: str | None
) -> SignInRequest:
    """Make the QR request whose code a phone of ``user_id`` scanned theirs.

    ``code`` is what the QR code holds, or its ``{code}`` part. The code
    works once.

    Raises:
        QrCodeExpired: No request has the code: unknown, claimed, or expired.
        DifferentNetwork: ``QR_SAME_NETWORK = "require"``, and the phone's
            ``address`` is not the page's. A CIBA client's QR request has
            no page address, and is not compared.
    """
    now = utc_now()
    request = await uow.requests.by_qr(secret_digest(_code_in(code)))
    if request is None or request.expired(now) or unscanned_too_long(request, now):
        raise QrCodeExpired()
    if (
        ciba_settings().QR_SAME_NETWORK == "require"
        and request.requester_ip is not None  # A client's: no page to compare.
        and address != request.requester_ip
    ):
        raise DifferentNetwork()
    if not await uow.requests.claim(request.id, user_id, address, now):
        raise QrCodeExpired()
    claimed = await uow.requests.get(request.id)
    assert claimed is not None  # noqa: S101 - claimed in this transaction.
    return claimed


def _code_in(scanned: str) -> str:
    """The secret in what a QR code holds: the text ``QR_FORMAT`` put around it."""
    before, _, after = ciba_settings().QR_FORMAT.partition("{code}")
    if (
        scanned.startswith(before)
        and scanned.endswith(after)
        and len(scanned) > len(before) + len(after)
    ):
        return scanned[len(before) : len(scanned) - len(after)]
    return scanned


async def waiting(
    uow: CibaUnitOfWork, user_id: UUID, request_id: UUID
) -> SignInRequest:
    """The user's request ``request_id``, if their phone may still answer it.

    Raises:
        SignInNotFound: The user has no such request, or it was answered,
            or it expired.
    """
    request = await uow.requests.get(request_id)
    if (
        request is None
        or request.user_id != user_id
        or request.expired(utc_now())
        or request.state not in {"pending", "claimed"}
    ):
        raise SignInNotFound()
    return request


async def approve(
    uow: CibaUnitOfWork,
    user_id: UUID,
    request_id: UUID,
    device_id: UUID,
    *,
    match_code: str | None = None,
) -> SignInRequest | None:
    """Approve the user's request from their device ``device_id``.

    None when the request asks for a number and ``match_code`` is not it:
    the request is denied, so commit ``uow`` before answering
    (``WrongMatchCode``). One wrong number ends it: guessing gets one try.

    Raises:
        SignInNotFound: The user has no such request waiting.
    """
    request = await waiting(uow, user_id, request_id)
    now = utc_now()
    if request.match_code is not None and not hmac.compare_digest(
        (match_code or "").strip(), request.match_code
    ):
        await _answer_no(uow, request, user_id, now, reported=False)
        return None
    if not await uow.requests.approve(request.id, user_id, device_id, now):
        raise SignInNotFound()
    user = await uow.users.get(UserId(user_id))
    user.record(
        SignInApproved(
            user_id=user_id,
            request_id=request.id,
            client_id=request.client_id,
            device_id=device_id,
        )
    )
    return await uow.requests.get(request.id)


async def deny(
    uow: CibaUnitOfWork, user_id: UUID, request_id: UUID, *, report: bool = False
) -> None:
    """Deny the user's request; with ``report``, every other one waiting too.

    A report says the user did not start it: someone who knows them did.
    It is audited (``SignInReported``), and no push reaches the user for
    ``REPORT_COOLDOWN``.

    Raises:
        SignInNotFound: The user has no such request waiting.
    """
    request = await waiting(uow, user_id, request_id)
    await _answer_no(uow, request, user_id, utc_now(), reported=report)


async def _answer_no(
    uow: CibaUnitOfWork,
    request: SignInRequest,
    user_id: UUID,
    now: datetime,
    *,
    reported: bool,
) -> None:
    """Deny ``request``, and with ``reported`` the user's others; record it."""
    if not await uow.requests.deny(request.id, user_id, now, reported=reported):
        raise SignInNotFound()
    if reported:
        await uow.requests.deny_waiting(user_id, now)
    event = SignInReported if reported else SignInDenied
    user = await uow.users.get(UserId(user_id))
    user.record(
        event(user_id=user_id, request_id=request.id, client_id=request.client_id)
    )


async def poll(
    handle: str,
    *,
    client_id: str | None,
    kind: str = SIGN_IN,
    credential_digest: str | None = None,
) -> SignInRequest:
    """The approved request ``handle`` polls for, redeemed now; it works once.

    ``client_id`` is the initiator: a CIBA client's id, or None for the
    project's own pages; a handle polls only for its initiator, as its
    ``kind``, and for a step-up or a transaction only from the credentials
    that asked (``credential_digest``). Commits what the poll changed (its
    time, the interval, the redemption) before it answers.

    Raises:
        SignInUnknown: No request of the initiator, of ``kind``, and of
            these credentials has the handle, or it was redeemed.
        AccessDenied: The phone denied it.
        SignInExpired: Its time is up, or its QR code was not scanned in
            ``QR_TTL``.
        SlowDown: The poll came within the interval, which grows by 5 s.
        AuthorizationPending: The phone has not answered yet.
    """
    now = utc_now()
    async with CibaUnitOfWork() as uow:
        request = await uow.requests.by_handle(secret_digest(handle))
        if (
            request is None
            or request.client_id != client_id
            or request.kind != kind
            or request.credential_digest != credential_digest
            or request.redeemed_at is not None
        ):
            raise SignInUnknown()
        if request.denied_at is not None:
            raise AccessDenied()
        if request.expired(now) or unscanned_too_long(request, now):
            raise SignInExpired()
        if not await uow.requests.polled(request.id, now, request.interval):
            await uow.requests.slow_down(request.id, now, SLOW_DOWN_STEP)
            await uow.commit()
            raise SlowDown()
        if request.approved_at is not None:
            redeemed = await uow.requests.redeem(request.id, now)
            await uow.commit()
            if not redeemed:  # Another poll redeemed it first.
                raise SignInUnknown()
            return request
        await uow.commit()
    raise AuthorizationPending()


def _seconds(span: timedelta) -> int:
    """Whole seconds in ``span``."""
    return int(span.total_seconds())
