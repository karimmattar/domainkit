"""The login gate of ``CIBA.APPROVE_LOGINS``: the mobile app is the identity.

With the setting on, a login of a user who bound a device works only:

1. from one of those devices, which signs the login request
   (``DeviceSigned``): the app itself logs in, by password, passkey, or
   provider;
2. or once approved from one (``/app-sign-in``), which issues the login
   on the server side, past the gates.

A user with no bound device logs in as before: the app's first login has
to bind it. Anything else is refused with ``403 approval_required``.
"""

from starlette.requests import Request

from domainkit.contrib.auth import AbstractBaseUser
from domainkit.contrib.devices import DevicesUnitOfWork
from domainkit.contrib.devices.binding import signing_device

from .exceptions import ApprovalRequired
from .services import ciba_settings

__all__ = ["approved_from_the_phone"]


async def approved_from_the_phone(request: Request, user: AbstractBaseUser) -> None:
    """Let the login of ``user`` through if the app is not their identity, or signs it.

    Raises:
        ApprovalRequired: ``APPROVE_LOGINS`` is on, the user bound a device,
            and none of theirs signed this request.
    """
    if not ciba_settings().APPROVE_LOGINS:
        return
    async with DevicesUnitOfWork() as uow:
        bound = await uow.devices.count(user.id)
    if not bound or await signing_device(request, user) is not None:
        return
    raise ApprovalRequired()
