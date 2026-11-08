"""The login gate of ``CIBA.APPROVE_LOGINS``: the mobile app is the identity.

With the setting on, a login of a user who bound a device works only:

1. from one of those devices, which signs the login request
   (``DeviceSigned``): the app itself logs in, by password, passkey, or
   provider;
2. or once approved from one (``/app-sign-in``), which issues the login
   on the server side, past the gates.

A user with no bound device logs in as before: the app's first login has
to bind it. Anything else is refused with ``403 phone_approval_required``.

With ``"always"``, a user with no bound device does not log in at all
(``403 phone_enrollment_required``): the app enrolls its key first
(``/devices/enroll``), and that request, signed by the key it binds, is
the user's first login.
"""

from typing import Final

from starlette.requests import Request

from domainkit.contrib.auth import AbstractBaseUser
from domainkit.contrib.devices import DevicesUnitOfWork
from domainkit.contrib.devices.binding import signing_device

from .exceptions import PhoneApprovalRequired, PhoneEnrollmentRequired
from .services import ciba_settings

__all__ = ["approved_from_the_phone", "mark_approved"]

APPROVED: Final = "domainkit.ciba.approved_user"
"""Where a request that redeemed an approved sign-in keeps its user's id."""


def mark_approved(request: Request, user: AbstractBaseUser) -> None:
    """Say that ``request`` redeemed a sign-in ``user``'s phone approved."""
    request.scope[APPROVED] = user.id


async def approved_from_the_phone(request: Request, user: AbstractBaseUser) -> None:
    """Let the login of ``user`` through if the app is not their identity, or signs it.

    A request that redeemed a sign-in the user's phone approved passes
    (``mark_approved``).

    Raises:
        PhoneApprovalRequired: ``APPROVE_LOGINS`` is on, the user bound a device,
            none of theirs signed this request, and no phone approved it.
        PhoneEnrollmentRequired: ``APPROVE_LOGINS`` is ``"always"``, and the
            user has no bound device.
    """
    mode = ciba_settings().APPROVE_LOGINS
    if not mode or request.scope.get(APPROVED) == user.id:
        return
    async with DevicesUnitOfWork() as uow:
        bound = await uow.devices.count(user.id)
    if not bound:
        if mode == "always":
            raise PhoneEnrollmentRequired()
        return
    if await signing_device(request, user) is None:
        raise PhoneApprovalRequired()
