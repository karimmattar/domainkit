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

A gate sees how the login is made with ``login_origin(request)``: its
method, what it proved (``amr``), when, and the bound device that signed
it (ADR 0146). The route names them to ``pass_login_gates``.

An ``on_session_limits`` hook gives a login shorter limits than the
settings, when it is made: an idle timeout, and a lifetime. The login
keeps them.

A gate that needs no password registers with ``on_password_login``: it
runs before ``/login`` and ``/session`` look at the password, given the
request and the identifier, and refuses the same way whatever account the
identifier names. A refusal there confirms no password, says nothing of
the account, and spends none of its count of wrong passwords.

Gates fail closed. While a gate is registered, ``held_login`` refuses a
login whose user did not pass them in the current context
(``LoginNotGated``), so a route that forgets the call cannot issue.
Code that issues a login on the server side, with no request for the
gates to judge, says so with ``gates_passed(user_id)``. Examples are a
sign-in approved from a phone and a test's session.
"""

from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Literal
from uuid import UUID

from fastapi import Depends
from starlette.requests import HTTPConnection, Request

from domainkit.apps.hooks import HookPoint
from domainkit.exceptions import DomainkitError
from domainkit.http.access import NotAuthenticated

from .models import AbstractBaseUser, Group
from .tokens import LoginOrigin, SessionLimits, noted, utc_now

if TYPE_CHECKING:
    from .backends import ObjectGrant

__all__ = [
    "MANAGES_LOGINS",
    "AddedGroup",
    "AdminChange",
    "GroupChange",
    "LoginGate",
    "LoginNotGated",
    "LoginOrigin",
    "LoginsCheck",
    "PasswordGate",
    "SessionLimiter",
    "SessionLimits",
    "StaffCheck",
    "SuperuserCheck",
    "check_gates_passed",
    "check_manage_logins",
    "gates_passed",
    "login_origin",
    "manages_logins",
    "on_admin_change",
    "on_admin_group_change",
    "on_create_staff",
    "on_login_gate",
    "on_make_superuser",
    "on_manage_logins",
    "on_password_login",
    "on_session_limits",
    "pass_login_gates",
    "pass_password_gates",
]

type LoginGate = Callable[[Request, AbstractBaseUser], Awaitable[None]]
"""A check of a login: return to let it through, raise to refuse it."""

on_login_gate: Final[HookPoint[LoginGate]] = HookPoint(
    "login gate", arguments=2, awaited=True
)
"""Register a gate every login passes (``on_login_gate(gate)`` in ``ready()``)."""

LOGIN_ORIGIN: Final = "domainkit.auth.login_origin"
"""Where a request that makes a login keeps how it is made, for its gates."""

type SessionLimiter = Callable[
    [AbstractBaseUser, LoginOrigin], Awaitable[SessionLimits | None]
]
"""Shorter limits for a login being made, from its user and how it is made."""

on_session_limits: Final[HookPoint[SessionLimiter]] = HookPoint(
    "session limits", arguments=2, awaited=True
)
"""Register what gives some logins shorter limits (ADR 0146).

It runs when a login is issued, past the gates, with the user and how the
login is made (``LoginOrigin``), and returns ``SessionLimits``, or None for
the settings'. Of several hooks, the shortest of each limit holds, and no
limit outlasts its setting. The login keeps its limits: a hook that
changes later changes the logins made after.
"""

type PasswordGate = Callable[[Request, str], Awaitable[None]]
"""A check of a password login before the password is looked at: raise to refuse."""

on_password_login: Final[HookPoint[PasswordGate]] = HookPoint(
    "password login", arguments=2, awaited=True
)
"""Register a gate of password logins that runs before the password does.

It takes the request and the identifier, and must answer the same whatever
account the identifier names, or none: ``CIBA.APPROVE_LOGINS = "always"``
refuses every password login no bound device of that account signed.
"""

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

type SuperuserCheck = Callable[[], None]
"""A check before the admin API makes a user a superuser: raise to refuse."""

on_make_superuser: Final[HookPoint[SuperuserCheck]] = HookPoint(
    "make superuser", arguments=0, awaited=False
)
"""Register a check of making a superuser in the admin API.

It runs before the admin API creates a superuser or makes a user one.
``domainkit.contrib.approvals`` registers its guard: where it holds the
grant for approval, no one makes a superuser here.
"""

type StaffCheck = Callable[[], None]
"""A check before the admin API creates a user who is staff: raise to refuse."""

on_create_staff: Final[HookPoint[StaffCheck]] = HookPoint(
    "create staff", arguments=0, awaited=False
)
"""Register a check of creating a user who is staff in the admin API.

It runs with the admin API's own checks of a creation, before the
``on_admin_change`` checks, and in the creation's twin too, so asking for
an authorization of a creation it refuses is refused (since 2.5).
``domainkit.contrib.approvals`` registers one while ``ADMIN_APPROVALS``
holds making staff: create the user, then make them staff.
"""

type AdminKind = Literal[
    "create",
    "update",
    "delete",
    "password",
    "logout",
    "impersonate",
    "groups",
    "permissions",
]


@dataclass(frozen=True, slots=True)
class AddedGroup:
    """A group a change adds a user to, as the admin API read it (since 2.7)."""

    id: UUID
    name: str
    permissions: frozenset[str]
    """The keys of the permissions the group grants."""
    object_grants: "frozenset[ObjectGrant]" = frozenset()
    """What every backend grants the group on single objects (``object_grants``)."""


@dataclass(frozen=True, slots=True)
class AdminChange:
    """What a staff member is about to do to a user through the admin API."""

    kind: AdminKind
    fields: frozenset[str] = frozenset()
    """The fields and flags it sets (``create``, ``update``): ``is_staff``,
    ``is_superuser``, ``is_active``, or the model's own fields."""
    added: frozenset[str] = frozenset()
    """The groups (``groups``), or permissions (``permissions``), it adds."""
    removed: frozenset[str] = frozenset()
    """Those it takes away."""
    values: Mapping[str, object] = field(default_factory=dict, hash=False)
    """What it sets: each of ``fields`` with the value it stores (``is_staff``
    true or false), and the ``reason`` of an ``impersonate`` (since 2.4).
    Never a password."""
    groups: tuple[AddedGroup, ...] = field(default=(), compare=False)
    """The groups it adds (``groups``), by id, with the permissions each
    grants as it was read: ``added`` names them (since 2.7)."""
    asking: bool = False
    """True at the change's twin, which only asks for an authorization of
    it: nothing is changed, and a check that writes (approvals' hold)
    writes nothing (since 2.7)."""
    last_check: Callable[[], Awaitable[None]] | None = field(
        default=None, compare=False
    )
    """The admin API's own check that only the change itself makes, run
    once, when the change is about to be made (since 2.7): a password among
    the user's last ones. A check that lets the change go on by spending
    something, as approvals' hold spends an approval it found, awaits it
    first; else the admin API awaits it once every check passed. None:
    there is none."""


type AdminCheck = Callable[[Any, AbstractBaseUser | None, AdminChange], Awaitable[None]]
"""A check of a change the admin API makes: ``(actor, target, change)``."""

on_admin_change: Final[HookPoint[AdminCheck]] = HookPoint(
    "admin change", arguments=3, awaited=True
)
"""Register a check of every change the admin API makes to a user.

It runs once the admin API's own checks passed, right before the change,
with the staff member, the user (None for ``create``), and the
``AdminChange``; raise to refuse it, as ``PermissionDenied`` ("nobody
edits their own account here", "making staff needs another
permission"). User report A9. With ``AUTH.ADMIN_ACTION`` on, the
change's twin runs it too, with ``asking`` true (since 2.7).
``domainkit.contrib.approvals`` registers one with
``APPROVALS.ADMIN_APPROVALS``, at ``order=100``, after the checks
registered without an order: it holds the changes that setting names
for approval, and may spend an approval.
"""

type GroupKind = Literal[
    "group_create", "group_rename", "group_permissions", "group_delete"
]


@dataclass(frozen=True, slots=True)
class GroupChange:
    """What a staff member is about to do to a group through the admin API.

    Since 2.7 (user report A19).
    """

    kind: GroupKind
    name: str | None = None
    """The name it gives the group (``group_create``, ``group_rename``)."""
    added: frozenset[str] = frozenset()
    """The permissions it grants the group (``group_create``,
    ``group_permissions``): every member gains them."""
    removed: frozenset[str] = frozenset()
    """Those it takes away (``group_permissions``)."""
    asking: bool = False
    """True at the change's twin: nothing is changed, and a check that
    writes writes nothing."""


type GroupCheck = Callable[[Any, Group | None, GroupChange], Awaitable[None]]
"""A check of a change the admin API makes to a group: ``(actor, group, change)``."""

on_admin_group_change: Final[HookPoint[GroupCheck]] = HookPoint(
    "admin group change", arguments=3, awaited=True
)
"""Register a check of every change the admin API makes to a group (since 2.7).

It runs as ``on_admin_change`` runs for a user's: once the admin API's own
checks passed, right before the change, and at its twin with ``asking``
true. It is called with the staff member, the group as stored (None for
``group_create``), and the ``GroupChange``; raise to refuse it.
``domainkit.contrib.approvals`` registers one with
``APPROVALS.ADMIN_APPROVALS``, at ``order=100``: it holds granting a
group permissions (``auth.set_group_permissions``).
"""

_passed: ContextVar[Any] = ContextVar("login_gates_passed", default=None)
"""The id of the user whose login passed the gates in this context."""


class LoginNotGated(RuntimeError, DomainkitError):
    """A login was issued without passing the gates: a route forgot them (500)."""


async def pass_password_gates(request: Request, identifier: str) -> None:
    """Run every gate of ``on_password_login`` before a password is checked.

    Raises:
        Exception: Whatever the first refusing gate raises.
    """
    for hook in on_password_login:
        await hook.call(request, identifier)


async def pass_login_gates(
    request: Request,
    user: AbstractBaseUser,
    *,
    method: str | None = None,
    amr: Iterable[str] = (),
    device_id: UUID | None = None,
) -> None:
    """Run every gate on the login of ``user``; record that it passed.

    ``method`` (a ``LoginMethod``), ``amr`` (RFC 8176's names of what the
    login proved) and ``device_id`` say how it is made: the gates read them
    with ``login_origin(request)``. Name what ``issue_login`` is given.

    Raises:
        Exception: Whatever the first refusing gate raises.
    """
    request.scope[LOGIN_ORIGIN] = LoginOrigin(
        method, device_id, frozenset(amr), utc_now()
    )
    for hook in on_login_gate:
        await hook.call(request, user)
    _passed.set(user.id)


def login_origin(request: HTTPConnection) -> LoginOrigin | None:
    """How the login ``request`` makes is made, for its gates; None: it makes none.

    Its method, what it proved, and when, as its route named them to
    ``pass_login_gates``, and the bound device that signed the request,
    once the devices app checked the signature (``signing_device``, which
    ``CIBA.APPROVE_LOGINS``'s gate calls). A device's signature adds
    ``hwk``. A sign-in approved on the phone names the request it redeems,
    and its binding message (``note_login_sign_in``).
    """
    origin = request.scope.get(LOGIN_ORIGIN)
    if not isinstance(origin, LoginOrigin):
        return None
    return noted(origin, request.scope)


async def session_limits(user: AbstractBaseUser, origin: LoginOrigin) -> SessionLimits:
    """The limits every ``on_session_limits`` hook gives this login: the shortest.

    Empty (the settings' limits) with no hook, or none that gives one.
    """
    limits = SessionLimits()
    for hook in on_session_limits:
        given = await hook.call(user, origin)
        if given is not None:
            limits = limits.within(given)
    return limits


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
