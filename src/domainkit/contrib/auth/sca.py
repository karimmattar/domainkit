"""Transactions authorized for one amount and one payee (PSD2's dynamic linking).

Strong customer authentication of a payment must be bound to its amount
and its payee: the code the user's authentication produces is specific to
both, and any change to either makes it fail (PSD2 RTS, Article 5). A use
case asks for an authorization, the user confirms it with a second
factor, and the use case spends it on that very transaction::

    from domainkit.contrib.auth.sca import consume_authorization, request_authorization

    challenge = await request_authorization(
        user.id, Money.of("250.00", "EUR"), "DE89370400440532013000", purpose="transfer"
    )
    # The client shows the amount and payee (GET /sca/{id}), and the user
    # confirms with a code: POST /sca/{id}/confirm answers the authorization.

    await consume_authorization(
        authorization, user_id=user.id, amount=amount, payee=payee, purpose="transfer"
    )  # Refused (403 transaction_not_authorized) for any other amount or payee.

How it is bound (ADR 0122): the request is a stored row (the user, the
amount and currency, the payee, the purpose, the expiry). Confirming it
returns ``<id>.<code>``, the code an HMAC-SHA256, under a key derived from
``SECRET_KEY``, of the canonical encoding of the row's id (a random nonce),
user, currency, amount, payee, and purpose. Spending it recomputes the code
from the values the use case is about to act on and checks it, then marks
the row used in one statement, so it works once, by one caller, within
``AUTH.SCA.TTL``. A substituted amount or payee fails the code, and voids
the authorization; a replay finds the row used; a row edited after its
confirmation fails the code too.

Spend the authorization before the transaction acts, in a transaction of
its own: a transfer that fails after it needs a new authorization, never
a second use of the old one.
"""

import base64
import hashlib
import hmac
import json
import re
from datetime import datetime
from typing import Final
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from domainkit.ddd import BusinessRuleViolation, NotFound, PermissionDenied
from domainkit.http.access import IsUnscoped, Require
from domainkit.http.errors import APIError
from domainkit.money import Money
from domainkit.signing import derive_keys

from .access import AuthenticatedUser
from .api import PASSWORD_MAX_LENGTH, _errors, _no_store
from .events import TransactionAuthorizationUsed, TransactionAuthorized
from .models import UserId
from .repositories import ScaChallenge
from .settings import auth_settings
from .stepup import verify_factors
from .tables import PAYEE_MAX_LENGTH, PURPOSE_MAX_LENGTH
from .throttling import audit_refusal
from .tokens import utc_now
from .uow import AuthUnitOfWork

__all__ = [
    "InvalidTransaction",
    "ScaChallenge",
    "TransactionNotAuthorized",
    "consume_authorization",
    "request_authorization",
    "router",
]

KEY_PURPOSE: Final = "auth.sca"
"""What the key of authentication codes is derived for."""
ENCODING: Final = "domainkit.sca.v1"
"""Starts every canonical encoding: a later one never matches it."""
PURPOSE: Final = re.compile(r"[a-z][a-z0-9_.:-]*")
"""A purpose: a lower-case name, such as ``transfer`` or ``payments.payout``."""
MAX_PLACES: Final = 4
"""The most places an amount may have: the table's scale."""
ACTION: Final = "auth.sca"
"""The audit trail's action for refused authorizations."""

router = APIRouter(tags=["auth"])
UNSCOPED: Final = [Depends(Require(IsUnscoped()))]


class TransactionNotAuthorized(PermissionDenied):
    """No authorization holds for this transaction: get one, then try again (403).

    The authorization is unknown, another user's, unconfirmed, expired,
    used, or for another amount, payee, or purpose.
    """

    code = "transaction_not_authorized"

    def __init__(self) -> None:
        """Say what to do, and nothing of why."""
        super().__init__(
            "This transaction is not authorized: confirm its amount and payee, "
            "then try again."
        )


class InvalidTransaction(BusinessRuleViolation):
    """The amount, payee, or purpose cannot be authorized as given."""


class TransactionNotFound(NotFound):
    """No transaction of the user waits under that id (404)."""


async def request_authorization(
    user_id: UUID, amount: Money, payee: str, *, purpose: str
) -> ScaChallenge:
    """Store a transaction for ``user_id`` to confirm, for ``AUTH.SCA.TTL``.

    Show the user what they confirm (``GET /sca/{id}``): its amount and
    payee, as the use case will act on them.

    Raises:
        InvalidTransaction: The amount is not positive, or its currency has
            more than 4 places (the table's scale); the payee is empty, too
            long, or holds control characters; or the purpose is not a
            lower-case name.
    """
    _check(amount, payee, purpose)
    async with AuthUnitOfWork() as uow:
        challenge = await uow.sca.add(
            user_id, amount, payee, purpose, ttl=auth_settings().SCA.TTL
        )
        await uow.commit()
    return challenge


async def consume_authorization(
    authorization: str,
    *,
    user_id: UUID,
    amount: Money,
    payee: str,
    purpose: str,
) -> ScaChallenge:
    """Spend ``authorization`` on this transaction, once; return what it was for.

    It holds only for ``user_id``, the exact ``amount`` (currency
    included), ``payee``, and ``purpose`` its user confirmed, before it
    expires, and once. An authorization used for anything else is voided:
    any change asks for a new one. Call it before the transaction acts.

    Raises:
        TransactionNotAuthorized: It does not hold for this transaction.
    """
    challenge_id, code = _parsed(authorization)
    now = utc_now()
    async with AuthUnitOfWork() as uow:
        challenge = None if challenge_id is None else await uow.sca.get(challenge_id)
        reason = _unusable(challenge, code, user_id, now)
        if challenge is not None and reason is None:
            reason = await _spend(
                uow, challenge, code, user_id, amount, payee, purpose, now
            )
            await uow.commit()
    if challenge is None or reason is not None:
        await audit_refusal(ACTION, reason or "invalid_authorization")
        raise TransactionNotAuthorized
    return challenge


def _unusable(
    challenge: ScaChallenge | None, code: str, user_id: UUID, now: datetime
) -> str | None:
    """Why ``challenge`` cannot be spent by ``user_id`` with ``code``; None if it can.

    Unknown, another user's, unconfirmed, used, expired, or changed since
    its confirmation (the code no longer covers the row).
    """
    if (
        challenge is None
        or challenge.user_id != user_id
        or challenge.confirmed_at is None
        or challenge.consumed_at is not None
        or challenge.expired(now)
        or not _matches(code, challenge)
    ):
        return "invalid_authorization"
    return None


async def _spend(
    uow: AuthUnitOfWork,
    challenge: ScaChallenge,
    code: str,
    user_id: UUID,
    amount: Money,
    payee: str,
    purpose: str,
    now: datetime,
) -> str | None:
    """Spend ``challenge`` on these values; why it was refused, or None.

    It is spent either way: values other than those confirmed void it.
    """
    bound = _matches(
        code,
        ScaChallenge(
            id=challenge.id,
            user_id=user_id,
            amount=amount,
            payee=payee,
            purpose=purpose,
            created_at=challenge.created_at,
            expires_at=challenge.expires_at,
        ),
    )
    if not await uow.sca.consume(challenge.id, now):
        return "used"
    if not bound:
        return "changed"
    stored = await uow.users.get(UserId(user_id))
    stored.record(
        TransactionAuthorizationUsed(
            user_id=user_id, challenge_id=challenge.id, purpose=purpose
        )
    )
    return None


class ScaConfirmIn(BaseModel):
    """A code of the authenticator app, and the password where it is asked."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=64, repr=False)
    password: str | None = Field(
        default=None, min_length=1, max_length=PASSWORD_MAX_LENGTH, repr=False
    )
    """Needed with ``AUTH.SCA.METHODS = ("pwd", "otp")``; ignored otherwise."""


class ScaChallengeOut(BaseModel):
    """What the user is asked to confirm."""

    id: UUID
    amount: Money
    payee: str
    purpose: str
    expires_at: datetime
    confirmed: bool


class AuthorizationOut(BaseModel):
    """The authorization: hand it to the use case that acts, once."""

    authorization: str = Field(repr=False)
    expires_at: datetime


@router.get(
    "/sca/{challenge_id}",
    dependencies=UNSCOPED,
    responses=_errors(status.HTTP_401_UNAUTHORIZED, status.HTTP_404_NOT_FOUND),
)
async def show_transaction(
    challenge_id: UUID, user: AuthenticatedUser
) -> ScaChallengeOut:
    """The amount, payee, and purpose of one of the user's transactions."""
    challenge = await _own(challenge_id, user.id)
    return ScaChallengeOut(
        id=challenge.id,
        amount=challenge.amount,
        payee=challenge.payee,
        purpose=challenge.purpose,
        expires_at=challenge.expires_at,
        confirmed=challenge.confirmed_at is not None,
    )


@router.post(
    "/sca/{challenge_id}/confirm",
    dependencies=UNSCOPED,
    responses=_errors(
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
        status.HTTP_429_TOO_MANY_REQUESTS,
    ),
)
async def confirm_transaction(
    challenge_id: UUID,
    body: ScaConfirmIn,
    request: Request,
    response: Response,
    user: AuthenticatedUser,
) -> AuthorizationOut:
    """Confirm a transaction with a second factor; return its authorization."""
    _no_store(response)
    challenge = await _own(challenge_id, user.id)
    if challenge.confirmed_at is not None or challenge.consumed_at is not None:
        raise APIError(
            status.HTTP_409_CONFLICT,
            "This transaction was confirmed already.",
            code="transaction_confirmed",
        )
    methods = tuple(sorted(auth_settings().SCA.METHODS))

    async def confirm(uow: AuthUnitOfWork) -> bool:
        if not await uow.sca.confirm(challenge.id, utc_now(), methods):
            return False
        stored = await uow.users.get(UserId(user.id))
        stored.record(
            TransactionAuthorized(
                user_id=user.id,
                challenge_id=challenge.id,
                purpose=challenge.purpose,
                methods=methods,
            )
        )
        return True

    if challenge.expired() or not await verify_factors(
        request,
        user,
        methods,
        code=body.code,
        password=body.password,
        action=ACTION,
        then=confirm,
    ):
        raise APIError(
            status.HTTP_409_CONFLICT,
            "This transaction can no longer be confirmed; start it again.",
            code="transaction_expired",
        )
    return AuthorizationOut(
        authorization=f"{challenge.id}.{authentication_code(challenge)}",
        expires_at=challenge.expires_at,
    )


def authentication_code(challenge: ScaChallenge, key: bytes | None = None) -> str:
    """The code that binds an authorization to ``challenge``'s values.

    HMAC-SHA256 of ``canonical(challenge)`` under a key derived from
    ``SECRET_KEY`` (or ``key``), in base64url without padding.
    """
    secret = derive_keys(KEY_PURPOSE)[0] if key is None else key
    digest = hmac.new(secret, canonical(challenge), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def canonical(challenge: ScaChallenge) -> bytes:
    """The bytes an authentication code covers: one encoding per transaction.

    A JSON array of the encoding's name, the id (the nonce), the user, the
    currency's code, the amount in fixed point with the currency's places,
    the payee exactly as given, and the purpose.
    """
    return json.dumps(
        [
            ENCODING,
            str(challenge.id),
            str(challenge.user_id),
            challenge.amount.currency.code,
            format(challenge.amount.amount, "f"),
            challenge.payee,
            challenge.purpose,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()


def _parsed(authorization: str) -> tuple[UUID | None, str]:
    """The challenge's id and the code of ``<id>.<code>``; None for a malformed id."""
    head, _, code = authorization.partition(".")
    try:
        return UUID(head), code
    except ValueError:
        return None, code


def _matches(code: str, challenge: ScaChallenge) -> bool:
    """Whether ``code`` is the authentication code of ``challenge``, under any key."""
    return any(
        hmac.compare_digest(code.encode(), authentication_code(challenge, key).encode())
        for key in derive_keys(KEY_PURPOSE)
    )


async def _own(challenge_id: UUID, user_id: UUID) -> ScaChallenge:
    """The user's transaction ``challenge_id``.

    Raises:
        TransactionNotFound: There is none, or it is another user's.
    """
    async with AuthUnitOfWork() as uow:
        challenge = await uow.sca.get(challenge_id)
    if challenge is None or challenge.user_id != user_id:
        raise TransactionNotFound("No such transaction.")
    return challenge


def _check(amount: Money, payee: str, purpose: str) -> None:
    """Refuse what cannot be authorized as given.

    Raises:
        InvalidTransaction: See ``request_authorization``.
    """
    if amount.amount <= 0:
        raise InvalidTransaction("A transaction's amount must be more than zero.")
    if amount.currency.minor_units > MAX_PLACES:
        raise InvalidTransaction(
            f"A transaction's currency may have {MAX_PLACES} places at most."
        )
    if not 0 < len(payee) <= PAYEE_MAX_LENGTH or not payee.isprintable():
        raise InvalidTransaction(
            f"A payee is 1 to {PAYEE_MAX_LENGTH} printable characters."
        )
    if len(purpose) > PURPOSE_MAX_LENGTH or not PURPOSE.fullmatch(purpose):
        raise InvalidTransaction(
            "A purpose is a lower-case name, such as transfer or payments.payout."
        )
