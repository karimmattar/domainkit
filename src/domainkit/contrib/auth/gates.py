"""Login gates: checks every login passes before it is issued (ADR 0128).

A domain registers a gate in ``DomainConfig.ready()``. The gate takes the
request and the user, and raises to refuse the login::

    from domainkit.contrib.auth.gates import on_login_gate

    async def approved_from_the_phone(request: Request, user: AbstractBaseUser) -> None:
        if not await approved(request, user):
            raise PhoneApprovalRequired()

    class SignInConfig(DomainConfig):
        def ready(self) -> None:
            on_login_gate(approved_from_the_phone)

Every auth route that issues a login calls ``pass_login_gates`` with its
request and the user before issuing. These are ``/login``, ``/session``,
``/mfa/login``, ``/session/mfa``, the passkey and device logins, and the
identity providers' logins. Gates run in ``INSTALLED_DOMAINS`` order, and
the first that raises refuses the login.

Gates fail closed. While a gate is registered, ``held_login`` refuses a
login whose user did not pass them in the current context
(``LoginNotGated``), so a route that forgets the call cannot issue.
Code that issues a login on the server side, with no request for the
gates to judge, says so with ``gates_passed(user_id)``. Examples are a
sign-in approved from a phone and a test's session.
"""

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Final
from uuid import UUID

from fastapi import Depends
from starlette.requests import Request

from domainkit.apps.hooks import HookPoint
from domainkit.exceptions import DomainkitError
from domainkit.http.access import NotAuthenticated

from .models import AbstractBaseUser

__all__ = [
    "MANAGES_LOGINS",
    "LoginGate",
    "LoginNotGated",
    "LoginsCheck",
    "check_gates_passed",
    "check_manage_logins",
    "gates_passed",
    "manages_logins",
    "on_login_gate",
    "on_manage_logins",
    "pass_login_gates",
]

type LoginGate = Callable[[Request, AbstractBaseUser], Awaitable[None]]
"""A check of a login: return to let it through, raise to refuse it."""

on_login_gate: Final[HookPoint[LoginGate]] = HookPoint(
    "login gate", arguments=2, awaited=True
)
"""Register a gate every login passes (``on_login_gate(gate)`` in ``ready()``)."""

type LoginsCheck = Callable[[Request, AbstractBaseUser], Awaitable[None]]
"""A check of a request that manages the user's other logins: raise to refuse."""

on_manage_logins: Final[HookPoint[LoginsCheck]] = HookPoint(
    "manage logins", arguments=2, awaited=True
)
"""Register a check of requests that manage logins (ADR 0130, 0131, 0132).

It runs before ``GET /tokens``, before ``DELETE /tokens/{id}`` ends a token
that is not the request's own login, before ``/logout`` ends
``everywhere`` or the ``others``, before a credential change that asks to
end the devices' logins, and before the requests that change the factors
logins rest on: the authenticator app's, the recovery codes, passkeys,
unbinding a device. Logging out of the request's own login is never
checked.
"""

_passed: ContextVar[Any] = ContextVar("login_gates_passed", default=None)
"""The id of the user whose login passed the gates in this context."""


class LoginNotGated(RuntimeError, DomainkitError):
    """A login was issued without passing the gates: a route forgot them (500)."""


async def pass_login_gates(request: Request, user: AbstractBaseUser) -> None:
    """Run every gate on the login of ``user``; record that it passed.

    Raises:
        Exception: Whatever the first refusing gate raises.
    """
    for hook in on_login_gate:
        await hook.call(request, user)
    _passed.set(user.id)


@contextmanager
def gates_passed(user_id: UUID | object) -> Iterator[None]:
    """Let the login of ``user_id`` through, in the block, with no gate run.

    For code that issues a login on the server side: a sign-in approved
    from the user's phone, or a test's session. Never wrap a login a
    request asked for in it: run the gates on that request instead.
    """
    token = _passed.set(user_id)
    try:
        yield
    finally:
        _passed.reset(token)


async def check_manage_logins(request: Request, user: AbstractBaseUser) -> None:
    """Run every check of ``on_manage_logins`` on ``request``.

    Raises:
        Exception: Whatever the first refusing check raises.
    """
    for hook in on_manage_logins:
        await hook.call(request, user)


async def manages_logins(request: Request) -> None:
    """A route's dependency: the request may manage its user's logins.

    Declare it on a route that adds or removes a way into the account (a
    device, a factor, a token, a linked identity), next to the rule that
    asks for the user::

        @router.post("/keys", dependencies=[MANAGES_LOGINS])

    It runs the ``on_manage_logins`` checks before the body is read, so
    the route answers their refusal whatever was sent (ADR 0136).

    Raises:
        NotAuthenticated: The request has no user.
        Exception: Whatever the first refusing check raises.
    """
    credentials = request.scope.get("auth")
    if credentials is None:
        raise NotAuthenticated
    await check_manage_logins(request, credentials.user)


MANAGES_LOGINS: Final = Depends(manages_logins)
"""``dependencies=[MANAGES_LOGINS]``: the route passes ``on_manage_logins``."""


def check_gates_passed(user: AbstractBaseUser) -> None:
    """Refuse the login of ``user`` if gates exist and it did not pass them.

    Raises:
        LoginNotGated: A gate is registered and the login skipped them.
    """
    if on_login_gate.hooks() and _passed.get() != user.id:
        raise LoginNotGated(
            "A login was issued without pass_login_gates(request, user): "
            "every route that issues a login runs the login gates first."
        )
