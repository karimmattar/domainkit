"""Enrollment: the first device of a user who has none, with no login first.

A phone app that holds a new key enrolls it in two steps (ADR 0129):

1. ``POST /devices/enroll/code`` ``{"identifier"}``: the worker sends a code
   to the account's email address or mobile number. The answer is always
   ``202``: it, and how long it takes, say nothing about the account.
   Nothing is sent to an unknown, inactive, or passwordless account, nor
   to one that has a bound device already.
2. ``POST /devices/enroll`` ``{"identifier", "code", "password", "name",
   "public_key"}``, signed by the new key (``X-Device-Time``,
   ``X-Device-Signature``, as ``DeviceSigned`` reads them, with no device id
   yet): the key is bound, and the user logged in from it.

The code and the password are two factors; the signature proves the caller
holds the key it binds. A user who turned a second factor on sends a code
of it too (``otp``), and one whose password staff set chooses a new one
(``new_password``), as a login would have them. A user with a bound device
enrolls no other: the old phone binds the new one's key (``POST
/devices``, signed, ADR 0134), and one who lost every phone goes to
support (``unbinddevices``).
"""

from typing import Final, NoReturn, cast

from starlette.requests import Request

from domainkit.contrib.auth import AbstractBaseUser
from domainkit.contrib.auth.backends import authenticate
from domainkit.contrib.auth.integration import (
    account_key,
    address_key,
    attempt,
    audit_refusal,
    auth_settings,
    first_use_after_code,
    needs_second_factor,
    second_factor_holds,
    send_code,
    target_of,
)
from domainkit.contrib.auth.models import UserId

from .binding import mark_signed_by, proves_key
from .events import DeviceBound
from .exceptions import DeviceAlreadyBound, EnrollmentRefused, SecondFactorRequired
from .stores import BoundDevice, DevicesUnitOfWork

__all__ = [
    "ENROLLMENT",
    "enroll",
    "send_enrollment_code",
]

ENROLLMENT: Final = "device_enrollment"
"""The purpose of the codes that enroll a first device."""
ENROLL_CODE_TASK: Final = "domainkit.devices.send_enrollment_code"
TEMPLATE: Final = "devices/enrollment_code"
THROTTLED: Final = "Too many attempts; try again later."


async def send_enrollment_code(identifier: str) -> None:
    """Send an enrollment code to the account ``identifier`` names, if it may enroll.

    The ``domainkit.devices.send_enrollment_code`` task, so that answering
    the request takes as long whoever it names.
    """
    async with DevicesUnitOfWork() as uow:
        user = await uow.users.find_by_identifier(identifier)
        bound = 0 if user is None else await uow.devices.count(user.id)
    if user is None or bound or not (user.is_active and user.has_usable_password()):
        return
    await send_code(
        user,
        identifier,
        purpose=ENROLLMENT,
        wording="enroll your phone",
        template=TEMPLATE,
    )


def register_tasks() -> None:
    """Register the task that sends enrollment codes (needs messaging)."""
    from domainkit.contrib.messaging import task

    task(name=ENROLL_CODE_TASK)(send_enrollment_code)


async def enroll(
    request: Request,
    *,
    identifier: str,
    code: str,
    password: str,
    otp: str | None = None,
    new_password: str | None = None,
    name: str,
    algorithm: int,
    public_key: bytes,
) -> tuple[AbstractBaseUser, BoundDevice]:
    """Bind the user's first device, proven by a code, the password, and its key.

    The proofs are checked in this order, so that a refusal never spends
    what the caller still needs: the key's signature and the password; a
    user with a second factor then needs ``otp`` (a TOTP or recovery code)
    before the code is checked; then the code (spent once it matches);
    then ``otp``. A password staff set is changed to ``new_password``
    (``AUTH.PASSWORD_CHANGE_ON_FIRST_USE``), as at ``/mfa/login``.

    Refused attempts count against ``AUTH.LOGIN_RATE`` for the client's
    address and for the account. On success the request counts as signed
    by the device bound (``signing_device``), so the login it goes on to
    issue passes the login gates as the phone's own logins do.

    Raises:
        EnrollmentRefused: The key did not sign the request, or the
            password, the code, or ``otp`` is wrong (one answer for all).
        SecondFactorRequired: The rest held so far, and the user has a
            second factor: send ``otp`` too.
        DeviceAlreadyBound: Every proof held, and the user has a bound device.
        APIError: 403 ``password_change_required`` without ``new_password``;
            429 past ``AUTH.LOGIN_RATE`` or ``AUTH.MFA.RATE``.
    """
    rate = auth_settings().LOGIN_RATE
    counted = await attempt(
        request,
        [
            (address_key(request, "device-enroll"), rate),
            (account_key("device-enroll", identifier), rate),
        ],
        action="devices.enroll",
        detail=THROTTLED,
    )
    signed = await proves_key(request, algorithm, public_key)
    user = await authenticate(
        identifier=identifier, username=identifier, password=password
    )
    if not signed or user is None:
        await _refuse()
    factor = await needs_second_factor(user)
    if factor and otp is None:
        await counted.succeeded()  # The password held: as /login, ask for a code.
        raise SecondFactorRequired()
    async with DevicesUnitOfWork() as uow:
        proven = await uow.verification_codes.check(
            ENROLLMENT, target_of(identifier), code
        )
        await uow.commit()  # A wrong try stays counted.
    if proven is None or proven.user_id != user.id:
        await _refuse()
    if factor and not await second_factor_holds(request, user, cast(str, otp)):
        await _refuse()
    await counted.succeeded()
    user = await first_use_after_code(user, new_password)
    device = await _bind_first(user, name, algorithm, public_key)
    mark_signed_by(request, device)
    return user, device


async def _refuse() -> NoReturn:
    """Audit the refusal, and refuse: never which proof failed.

    Raises:
        EnrollmentRefused: Always.
    """
    await audit_refusal("devices.enroll", "enrollment_refused")
    raise EnrollmentRefused()


async def _bind_first(
    user: AbstractBaseUser, name: str, algorithm: int, public_key: bytes
) -> BoundDevice:
    """Bind the device, under the user's lock, if the user has none yet.

    Raises:
        DeviceAlreadyBound: A device of the user is bound.
    """
    async with DevicesUnitOfWork() as uow:
        await uow.tokens.lock_user(user.id)
        if await uow.devices.count(user.id):
            raise DeviceAlreadyBound(
                "A device of yours is bound already: approve this one from it."
            )
        device = await uow.devices.add(user.id, name, algorithm, public_key)
        stored = await uow.users.get(UserId(user.id))
        stored.record(DeviceBound(user_id=user.id, device_id=device.id))
        await uow.commit()
    return device
