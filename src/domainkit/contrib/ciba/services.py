"""Start a sign-in request, let the phone answer it, and poll it (ADR 0128).

A request starts on one of two channels:

- ``start_push``: the initiator names the user; their apps get a push.
- ``start_qr``: the initiator names nobody; its page shows a QR code, and
  the phone that scans it claims the request (``claim``).

``ask_signed_in`` asks the phone of a user who is signed in already: to
approve an action (a step-up), one of their transactions (ADR 0129), or
one act's authorization (ADR 0144). Only the session that asked redeems
it, and a handle redeems as its kind alone: a step-up is never a login.

The phone answers with ``approve`` or ``deny``; the initiator polls with
``poll`` until it is approved (the request is redeemed, once), denied, or
expired, or withdraws it with ``cancel``. Each of these but ``poll`` and
``cancel`` runs in a ``CibaUnitOfWork`` the caller opens and commits::

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

import asyncio
import hashlib
import hmac
import secrets
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal, TypeGuard, cast
from uuid import UUID, uuid4

from domainkit.contrib.auth import AbstractBaseUser
from domainkit.contrib.auth.integration import language_of, utc_now
from domainkit.contrib.auth.models import UserId
from domainkit.ddd import event_bus
from domainkit.i18n import gettext, override

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
from .settings import CibaSettings, PushKind
from .stores import (
    AUTHORIZATION,
    PUSH,
    QR,
    SIGN_IN,
    STEP_UP,
    TRANSACTION,
    CibaUnitOfWork,
    SignInRequest,
)

__all__ = [
    "PUSH_KIND",
    "Requester",
    "Started",
    "approve",
    "ask_signed_in",
    "cancel",
    "ciba_settings",
    "claim",
    "deny",
    "poll",
    "start_push",
    "start_qr",
    "wait",
]

SECRET_BYTES: Final = 32
"""Random bytes in a poll handle and a QR code: 256 bits, CIBA asks 128."""
SLOW_DOWN_STEP: Final = 5
"""Seconds a poll that came too soon adds to the interval (CIBA 11)."""
WAIT_STEP: Final = 1.0
"""Seconds between the reads of a request a page waits for (``wait``)."""
PUSH_KIND: Final = "domainkit.sign_in"
"""The ``type`` of a push's data: the app fetches the request it names."""
AGENT_LENGTH: Final = 200
"""Characters of the initiator's ``User-Agent`` kept, to show on the phone."""
PHONE_CONFIRMS: Final = frozenset({"hwk", "phone"})
"""What the phone's approval of an act proves: a key in its hardware (RFC
8176's ``hwk``), on the bound device's own screen (``phone``, ADR 0144)."""
ASKS_PER_PENDING: Final = 2
"""A user's unanswered asks within ``REQUEST_TTL``, withdrawn ones too, are
at most this many times ``MAX_PENDING``: a session that asks and withdraws
in a loop is stopped, however ``START_RATE`` is set."""
SESSION_KINDS: Final = frozenset({STEP_UP, TRANSACTION, AUTHORIZATION})
"""What a signed-in session asks the phone, which it may withdraw."""
PUSH_DEFAULTS: Final = {
    SIGN_IN: ("Approve sign-in?", "Open the app to approve or deny it."),
    STEP_UP: (
        "Confirm it's you?",
        "Open the app to see what for, and approve or deny it.",
    ),
    TRANSACTION: (
        "Approve a transaction?",
        "Open the app to see its amount and payee, and approve or deny it.",
    ),
    AUTHORIZATION: (
        "Approve an action?",
        "Open the app to see what it is, and approve or deny it.",
    ),
}
"""Each kind's notification, title and body, where the settings give none:
domainkit's catalogs translate them (user report I8)."""


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


def _match_code(match: bool) -> str | None:
    """Two random digits for the page to show and the phone to type, if ``match``."""
    return f"{secrets.randbelow(100):02d}" if match else None


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
    request_id: UUID | None = None,
    same_network: str | None = None,
) -> SignInRequest:
    """A new request, as its row keeps it; its id ``request_id``, or a new one."""
    agent = requester.agent[:AGENT_LENGTH] if requester.agent else None
    return SignInRequest(
        id=request_id or uuid4(),
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
        same_network=same_network,
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
    match_code = _match_code(match)
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
        await _notify(uow, asked, request.id, SIGN_IN, config)
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
    request_id: UUID | None = None,
) -> Started:
    """Ask the phone of ``user``, who is signed in, to approve ``kind``.

    ``kind`` is ``step_up`` (``message`` says what for), ``transaction``
    (``sca_challenge_id``), or ``authorization``: auth's action
    authorization names the request, by the id ``request_id`` it was
    linked with first. Only the credentials ``credential_digest`` names
    redeem it. The push leaves with ``uow``'s commit.

    Raises:
        PhoneUnavailable: No phone of the user can be asked now: none is
            bound, ``MAX_PENDING`` requests wait, the user's sessions
            asked twice as many within ``REQUEST_TTL`` that the phone has
            not answered (withdrawn ones too), or the user reported one
            within ``REPORT_COOLDOWN``.
    """
    config = ciba_settings()
    now = utc_now()
    if not await _askable(uow, user, now) or (
        await uow.requests.unanswered_asks(
            user.id, SESSION_KINDS, now - config.REQUEST_TTL
        )
        >= ASKS_PER_PENDING * config.MAX_PENDING
    ):
        raise PhoneUnavailable()
    handle = secrets.token_urlsafe(SECRET_BYTES)
    match_code = _match_code(match)
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
            request_id=request_id,
        )
    )
    await _notify(uow, user, request.id, kind, config)
    return Started(
        handle=handle,
        request_id=request.id,
        expires_in=_seconds(config.REQUEST_TTL),
        interval=request.interval,
        match_code=match_code,
    )


async def bound_phone(user_id: UUID) -> bool:
    """Whether the user has a bound device: a phone their acts can be confirmed on."""
    async with CibaUnitOfWork() as uow:
        return bool(await uow.devices.count(user_id))


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


def _push_text(kind: str, config: CibaSettings) -> tuple[str, str]:
    """The title and body of the push that asks about a request of ``kind``.

    Each comes from ``PUSH_TEXTS[kind]``, else ``PUSH_TITLE`` or
    ``PUSH_BODY``, else the kind's own text (``PUSH_DEFAULTS``; a kind of
    a project's own, the sign-in's).
    """
    own = config.PUSH_TEXTS.get(cast("PushKind", kind))
    title, body = PUSH_DEFAULTS.get(kind, PUSH_DEFAULTS[SIGN_IN])
    return (
        (own and own.TITLE) or config.PUSH_TITLE or title,
        (own and own.BODY) or config.PUSH_BODY or body,
    )


async def _notify(
    uow: CibaUnitOfWork,
    user: AbstractBaseUser,
    request_id: UUID,
    kind: str,
    config: CibaSettings,
) -> None:
    """Queue a push to each of the user's apps, naming the request and no one.

    Its text says what kind of request asks (``_push_text``), translated
    into the user's language, or the request's (``language_of``):
    domainkit's catalogs translate the defaults, a project's its own.
    """
    from domainkit.contrib.notifications import PushMessage

    title, body = _push_text(kind, config)
    with override(language_of(user)):
        title, body = gettext(title), gettext(body)
    for token in await uow.push_tokens.of_user(user.id):
        await PushMessage(
            token=token.token,
            title=title,
            body=body,
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
    kind: str = SIGN_IN,
    credential_digest: str | None = None,
    match: bool = False,
    same_network: Literal["warn", "require"] | None = None,
) -> Started:
    """A request for whichever phone scans its QR code, within ``QR_TTL``.

    The code can be scanned for ``QR_TTL``; the phone that scans it then has
    ``REQUEST_TTL`` to answer, as for a push. ``kind`` is ``sign_in``, or
    ``device`` for a new phone that shows the code to be bound (ADR 0137):
    only what ``credential_digest`` names (its key) redeems it. With
    ``match``, the phone must type the two digits the page shows too.
    ``same_network`` is what a phone on another address than the page's
    gets when it claims the code (ADR 0155); None: ``QR_SAME_NETWORK``.
    """
    config = ciba_settings()
    now = utc_now()
    handle = secrets.token_urlsafe(SECRET_BYTES)
    code = secrets.token_urlsafe(SECRET_BYTES)
    match_code = _match_code(match)
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
            kind=kind,
            credential_digest=credential_digest,
            match_code=match_code,
            same_network=same_network,
        )
    )
    return Started(
        handle=handle,
        request_id=request.id,
        expires_in=_seconds(config.QR_TTL + config.REQUEST_TTL),
        interval=request.interval,
        match_code=match_code,
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
        DifferentNetwork: The request's page, or else ``QR_SAME_NETWORK``,
            asks ``"require"``, and the phone's ``address`` is not the
            page's. A CIBA client's QR request has no page address, and is
            not compared.
    """
    now = utc_now()
    request = await uow.requests.by_qr(secret_digest(_code_in(code)))
    if request is None or request.expired(now) or unscanned_too_long(request, now):
        raise QrCodeExpired()
    if (
        (request.same_network or ciba_settings().QR_SAME_NETWORK) == "require"
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
    if not matches(request, match_code):
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


def matches(request: SignInRequest, match_code: str | None) -> bool:
    """Whether ``match_code`` is the number ``request`` asks, if it asks one."""
    return request.match_code is None or hmac.compare_digest(
        (match_code or "").strip(), request.match_code
    )


async def deny(
    uow: CibaUnitOfWork, user_id: UUID, request_id: UUID, *, report: bool = False
) -> None:
    """Deny the user's request; with ``report``, every other one waiting too.

    A report says the user did not start it: someone who knows them did.
    It is audited (``SignInReported``), and no push reaches the user for
    ``REPORT_COOLDOWN``. A push its session withdrew unanswered is
    reported too, until ``REQUEST_TTL`` after it was asked: the push
    named it, and the user may not have started it.

    Raises:
        SignInNotFound: The user has no such request waiting, nor, for a
            report, withdrawn within ``REQUEST_TTL``.
    """
    if not report:
        await _answer_no(
            uow,
            await waiting(uow, user_id, request_id),
            user_id,
            utc_now(),
            reported=False,
        )
        return
    now = utc_now()
    since = now - ciba_settings().REQUEST_TTL
    found = await uow.requests.get(request_id)
    if (
        found is None
        or found.user_id != user_id
        or found.state not in {"pending", "claimed"}
        or not (
            not found.expired(now)
            or (found.channel == PUSH and found.created_at > since)
        )
    ):
        raise SignInNotFound()
    await _answer_no(uow, found, user_id, now, reported=True, since=since)


async def _answer_no(
    uow: CibaUnitOfWork,
    request: SignInRequest,
    user_id: UUID,
    now: datetime,
    *,
    reported: bool,
    since: datetime | None = None,
) -> None:
    """Deny ``request``, and with ``reported`` the user's others; record it.

    ``since``: a request created after it is denied even if it ended (a
    withdrawn push, reported).
    """
    if not await uow.requests.deny(
        request.id, user_id, now, reported=reported, since=since
    ):
        raise SignInNotFound()
    if reported:
        await uow.requests.deny_waiting(user_id, now)
    event = SignInReported if reported else SignInDenied
    user = await uow.users.get(UserId(user_id))
    user.record(
        event(user_id=user_id, request_id=request.id, client_id=request.client_id)
    )


def _initiated(
    request: SignInRequest | None,
    client_id: str | None,
    kind: str,
    credential_digest: str | None,
) -> TypeGuard[SignInRequest]:
    """Whether a handle's ``request`` may be polled, waited for, or cancelled.

    ``client_id`` started it, as ``kind``, ``credential_digest`` names the
    credentials that asked, and it was not redeemed.
    """
    return (
        request is not None
        and request.client_id == client_id
        and request.kind == kind
        and request.credential_digest == credential_digest
        and request.redeemed_at is None
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
    ``kind``, and for a step-up, a transaction or an act only from the
    credentials that asked (``credential_digest``). Commits what the poll
    changed (its time, the interval, the redemption) before it answers. A
    request the phone decided answers at once, however soon after the last
    poll: the interval holds back polls of a request still waiting, not its
    answer.

    Raises:
        SignInUnknown: No request of the initiator, of ``kind``, and of
            these credentials has the handle, or it was redeemed.
        AccessDenied: The phone denied it.
        SignInExpired: Its time is up, or its QR code was not scanned in
            ``QR_TTL``.
        SlowDown: The request still waits, and the poll came within the
            interval, which grows by 5 s.
        AuthorizationPending: The phone has not answered yet.
    """
    now = utc_now()
    async with CibaUnitOfWork() as uow:
        request = await uow.requests.by_handle(secret_digest(handle))
        if not _initiated(request, client_id, kind, credential_digest):
            raise SignInUnknown()
        if request.denied_at is not None:
            raise AccessDenied()
        if request.expired(now) or unscanned_too_long(request, now):
            raise SignInExpired()
        if request.approved_at is not None:  # Decided: answered, however early.
            redeemed = await uow.requests.redeem(request.id, utc_now())
            await uow.commit()
            if not redeemed:  # Another poll redeemed it first.
                raise SignInUnknown()
            return request
        if not await uow.requests.polled(request.id, now, request.interval):
            await uow.requests.slow_down(request.id, now, SLOW_DOWN_STEP)
            await uow.commit()
            raise SlowDown()
        await uow.commit()
    raise AuthorizationPending()


async def cancel(
    handle: str,
    *,
    client_id: str | None,
    kind: str = SIGN_IN,
    credential_digest: str | None = None,
) -> None:
    """Withdraw the request ``handle`` polls for, if unanswered (user report A16).

    For the initiator that no longer wants the answer (the page closed its
    dialog): a QR sign-in, whose code no phone can claim then, or what a
    signed-in session asked (an act, a transaction), which no longer
    counts against ``MAX_PENDING``. It ends now: the phone no longer lists
    it, nor can answer it, and nobody redeems it. Nothing is reported. A
    request the phone answered, or that ended, stays as it is: an approved
    one is still redeemed.

    A push sign-in is never withdrawn. Whoever knows an identifier starts
    one, so it stays on the user's phone, counted against ``MAX_PENDING``
    and open to a report, until it expires: ADR 0128's bounds on push
    bombing hold.

    A handle cancels only as it polls: for its initiator, as its ``kind``,
    and from the credentials that asked (``credential_digest``). Commits.

    Raises:
        SignInUnknown: No request of the initiator, of ``kind``, and of
            these credentials has the handle, or it was redeemed, or it
            is a push sign-in.
    """
    now = utc_now()
    async with CibaUnitOfWork() as uow:
        request = await uow.requests.by_handle(secret_digest(handle))
        if not _initiated(request, client_id, kind, credential_digest) or (
            request.kind == SIGN_IN and request.channel == PUSH
        ):
            raise SignInUnknown()
        await uow.requests.end(request.id, now)
        await uow.commit()


def _seconds(span: timedelta) -> int:
    """Whole seconds in ``span``."""
    return int(span.total_seconds())


async def wait(
    handle: str,
    *,
    seconds: float,
    client_id: str | None = None,
    kind: str = SIGN_IN,
    credential_digest: str | None = None,
) -> bool:
    """Wait up to ``seconds`` for the phone to answer request ``handle``; approved?

    For the project's own pages (user report L2): they hold this instead of
    polling. It reads the request every ``WAIT_STEP``, and at once when an
    approval or a denial commits in this process; it never redeems the
    request, nor counts as a poll, so a page that waits is never slowed
    down. The page then redeems an approved request as after a poll. A
    handle waits only as it polls: for its initiator, as its ``kind``,
    and from the credentials that asked (``credential_digest``).

    Raises:
        SignInUnknown: No request of the initiator, of ``kind``, and of
            these credentials has the handle, or it was redeemed.
        AccessDenied: The phone denied it.
        SignInExpired: Its time is up, or its QR code was not scanned in
            ``QR_TTL``.
    """
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    answered = asyncio.Event()
    watched: list[UUID] = []

    async def heard(event: SignInApproved | SignInDenied) -> None:
        if event.request_id not in watched:
            return
        if asyncio.get_running_loop() is loop:
            answered.set()
        else:  # Committed by a request of another event loop.
            with suppress(RuntimeError):  # The waiter's loop has closed.
                loop.call_soon_threadsafe(answered.set)

    event_bus.subscribe(SignInApproved, heard)
    event_bus.subscribe(SignInDenied, heard)
    try:
        while True:
            answered.clear()
            now = utc_now()
            async with CibaUnitOfWork() as uow:
                request = await uow.requests.by_handle(secret_digest(handle))
            if not _initiated(request, client_id, kind, credential_digest):
                raise SignInUnknown()
            if request.denied_at is not None:
                raise AccessDenied()
            if request.expired(now) or unscanned_too_long(request, now):
                raise SignInExpired()  # As a poll would: before an approval.
            if request.approved_at is not None:
                return True
            watched[:] = [request.id]
            left = end - loop.time()
            if left <= 0:
                return False
            with suppress(TimeoutError):
                async with asyncio.timeout(min(WAIT_STEP, left)):
                    await answered.wait()
    finally:
        event_bus.unsubscribe(SignInApproved, heard)
        event_bus.unsubscribe(SignInDenied, heard)
