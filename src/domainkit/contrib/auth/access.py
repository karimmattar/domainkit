"""Access rules about auth's own users: staff, superusers, and the user model.

The rule algebra (``BasePermission``, ``Require``, ``Public``, ...) is the
core's, for any authentication: ``domainkit.http.access`` (ADR 0080). What
stays here reads what only this app's users have, and ``current_user``,
``acurrent_user``, and ``CurrentUser`` are the core's, typed as this app's
users (``AnyUser``)::

    @router.get("/reports", dependencies=[Depends(Require(IsAdminUser()))])
    async def reports(...): ...

    @router.get("/me")
    async def me(user: AuthenticatedUser) -> UserOut: ...

``IsAdminUser`` and ``IsSuperuser`` refuse credentials limited to scopes:
scopes name permissions, and staff and superusers hold more.

``StepUp`` asks for a step-up: the user authenticated again, with a code of
their authenticator (and the password, as ``AUTH.STEP_UP.METHODS`` says),
within ``max_age``. It is a rule like any other, for routes, actions of a
``ResourceRouter``, and WebSockets; refused, it answers ``401
step_up_required``, and ``POST /step-up`` (``auth.stepup``) steps up::

    @router.post(
        "/payees",
        dependencies=[Depends(Require(StepUp(max_age=timedelta(minutes=5))))],
    )
    async def add_payee(...): ...

``StepUp(for_=...)`` asks for an authorization of one act instead
(``auth.actions``, ADR 0144): the request must hold a confirmed
authorization of that purpose, in ``Action-Authorization``, and the act's
service spends it, once::

    memberships.command_route(
        EndMembership,
        service=end_membership,
        method="DELETE",
        rule=HasPermissions("organizations.end_membership")
        & StepUp(for_="organizations.end_membership"),
    )

The rule names that moved to the core in 0.10 were removed from here, and
from ``domainkit.contrib.auth``, in 0.11: import them from
``domainkit.http.access``.
"""

import re
import warnings
from collections.abc import Awaitable, Callable, Collection
from datetime import timedelta
from typing import TYPE_CHECKING, Annotated, ClassVar, Final, cast

from fastapi import Depends, Request, status
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel
from starlette.requests import HTTPConnection

from domainkit.ddd import UseCase
from domainkit.deprecation import DomainkitDeprecationWarning
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import access
from domainkit.http.errors import APIError

from .authentication import Credentials, Proof, get_authenticators
from .models import AbstractBaseUser, AnyUser
from .settings import MAX_STEP_UP_AGE, Factor, auth_settings, with_a_factor
from .tokens import utc_now

if TYPE_CHECKING:
    from domainkit.ddd import UnitOfWork

    from .actions import Action

PROOF: Final = "domainkit.auth.proof"
"""Where a request keeps its credentials' step-up, once read."""
INSUFFICIENT: Final = "insufficient_user_authentication"
"""The ``WWW-Authenticate`` error that asks for a step-up (RFC 9470)."""
PURPOSE: Final = re.compile(r"[a-z][a-z0-9_.:-]*")
"""An act's purpose: a lower-case name (``auth.actions``)."""

current_user = cast("Callable[[HTTPConnection], AnyUser]", access.current_user)
"""The core's ``current_user``, typed as this app's users: the same function."""
acurrent_user = cast(
    "Callable[[HTTPConnection], Awaitable[AnyUser]]", access.acurrent_user
)
"""The core's ``acurrent_user``, typed as this app's users: the same function."""
CurrentUser = Annotated[AnyUser, Depends(access.acurrent_user)]
"""The request's user, or ``AnonymousUser``: the core's dependency, typed so."""


class IsAdminUser(access.BasePermission):
    """Staff users (``is_staff``), with credentials that are not limited to scopes."""

    bounds_scopes: ClassVar[bool] = True

    async def has_permission(self, request: HTTPConnection, user: AnyUser) -> bool:
        """The user is staff; scopes name permissions, and staff hold more."""
        return (
            user.is_authenticated
            and getattr(user, "is_staff", False) is True
            and access.scopes_of(request) is None
        )


class IsSuperuser(access.BasePermission):
    """Active superusers, with credentials that are not limited to scopes."""

    bounds_scopes: ClassVar[bool] = True

    async def has_permission(self, request: HTTPConnection, user: AnyUser) -> bool:
        """The user is an active superuser, and holds every permission here."""
        return (
            user.is_active and user.is_superuser and access.scopes_of(request) is None
        )


@access.reads_principal
async def authenticated_user(
    request: HTTPConnection,
    _: Annotated[
        HTTPAuthorizationCredentials | None, Depends(access.documenting_bearer)
    ] = None,
) -> AbstractBaseUser:
    """The user who logged in.

    Credentials limited to scopes need a rule that checks them on the route
    (``check_scopes_bounded``).

    Raises:
        NotAuthenticated: The request named no user of this app.
        PermissionDenied: With ``SCOPED_CREDENTIALS = "refuse"``, scoped
            credentials on a route that declares no rule that checks scopes.
    """
    await access.check_scopes_bounded(request)
    user = current_user(request)
    if not isinstance(user, AbstractBaseUser):
        raise access.NotAuthenticated(get_authenticators()[0].challenge())
    return user


AuthenticatedUser = Annotated[AbstractBaseUser, Depends(authenticated_user)]
"""The request's user; anonymous callers get 401."""


class StepUpRequired(APIError):
    """The rule needs a step-up the credentials do not hold (401).

    The code ``step_up_required`` tells the client to step up (``POST
    /step-up``) and send the request again; ``WWW-Authenticate`` says so as
    RFC 9470 does (``error="insufficient_user_authentication"``, and the
    ``max_age`` in seconds).
    """

    def __init__(self, max_age: timedelta, methods: Collection[str]) -> None:
        """Use status 401 and the code ``step_up_required``."""
        challenge = get_authenticators()[0].challenge(INSUFFICIENT)
        seconds = int(max_age.total_seconds())
        super().__init__(
            status.HTTP_401_UNAUTHORIZED,
            "Confirm it is you: authenticate again to do this.",
            code="step_up_required",
            headers={"WWW-Authenticate": f'{challenge}, max_age="{seconds}"'},
        )
        self.max_age = max_age
        self.methods = frozenset(methods)


class StepUp(access.BasePermission):
    """Users who authenticated again, with a second factor, within ``max_age``.

    A step-up (``POST /step-up``) proves ``AUTH.STEP_UP.METHODS`` (or
    ``methods``): a code of the authenticator app, and the password too
    if they say so. With ``domainkit.contrib.devices``, a passkey proves
    them all and a bound device's key the code (``STANDS_FOR``). A login
    is not a step-up, with a second factor or without. A refusal answers
    ``401 step_up_required`` (``StepUpRequired``). It is asked about an object too
    (``check_object_permissions``). An open WebSocket or event stream is
    asked again on each re-check (``LIVE_REAUTH_INTERVAL``), so it closes
    once its step-up is older than ``max_age``.

    ``StepUp(for_=...)``, given an ``Action`` or its purpose in place of
    ``max_age``, asks for an authorization of that one act instead
    (``ActionStepUp``, ADR 0144).
    """

    message: ClassVar[str] = "Confirm it is you: authenticate again to do this."

    def __new__(
        cls,
        max_age: timedelta | None = None,
        *,
        methods: Collection[Factor] | None = None,
        for_: "str | Action | None" = None,
    ) -> "StepUp":
        """A ``StepUp``, or with ``for_`` an ``ActionStepUp``."""
        made = ActionStepUp if for_ is not None and cls is StepUp else cls
        return super().__new__(made)

    def __init__(
        self,
        max_age: timedelta | None = None,
        *,
        methods: Collection[Factor] | None = None,
        for_: "str | Action | None" = None,
    ) -> None:
        """Require a step-up within ``max_age`` (a day at most), or an act's one.

        Raises:
            ImproperlyConfigured: Neither ``max_age`` nor ``for_`` is given,
                or both; ``max_age`` is not positive or exceeds a day;
                ``methods`` lacks ``otp`` or repeats one, or comes with
                ``for_`` (the action names its methods); or ``for_`` is not
                an ``Action`` or a purpose.
        """
        if (max_age is None) == (for_ is None):
            raise ImproperlyConfigured(
                "StepUp takes max_age=... (a step-up) or for_=... (one act's "
                "authorization): one of the two."
            )
        self.purpose: str | None = None
        """The act whose authorization it asks for (``for_``); None: a step-up."""
        self.action: Action | None = None
        self.max_age = max_age
        self.methods: frozenset[str] | None = None
        if for_ is not None:
            self._ask_for(for_, methods)
            return
        if not timedelta(0) < cast(timedelta, max_age) <= MAX_STEP_UP_AGE:
            raise ImproperlyConfigured(
                "StepUp(max_age=...) must be more than 0 and a day at most."
            )
        if methods is not None:
            try:
                methods = with_a_factor(tuple(methods))
            except ValueError as exc:
                raise ImproperlyConfigured(f"StepUp(methods=...): {exc}.") from None
            self.methods = frozenset(methods)

    def _ask_for(
        self, for_: "str | Action", methods: Collection[Factor] | None
    ) -> None:
        """Ask for an authorization of ``for_``: an ``Action``, or its purpose.

        Raises:
            ImproperlyConfigured: ``methods`` is given, or ``for_`` names no
                purpose.
        """
        if methods is not None:
            raise ImproperlyConfigured(
                "StepUp(for_=...) takes no methods: its Action names them."
            )
        purpose = for_ if isinstance(for_, str) else getattr(for_, "purpose", None)
        if not isinstance(purpose, str) or not PURPOSE.fullmatch(purpose):
            raise ImproperlyConfigured(
                "StepUp(for_=...) takes an Action, or its purpose: a lower-case "
                "name, such as organizations.end_membership."
            )
        self.purpose = purpose
        self.action = None if isinstance(for_, str) else for_

    def required(self) -> frozenset[str]:
        """What the step-up must have proved: ``methods``, or the setting's."""
        if self.methods is not None:
            return self.methods
        return frozenset(auth_settings().STEP_UP.METHODS)

    def unprovable(self) -> frozenset[str]:
        """What it asks for that no step-up proves (beyond ``STEP_UP.METHODS``).

        Not empty, the rule refuses everyone: ``auth.E106`` names it.
        """
        return self.required() - frozenset(auth_settings().STEP_UP.METHODS)

    async def has_permission(self, request: HTTPConnection, user: AnyUser) -> bool:
        """The credentials stepped up within ``max_age``, proving ``required()``.

        Never while staff act as the user (an impersonation): the acts a
        step-up guards are the user's own.
        """
        if not user.is_authenticated or impersonated(request):
            return False
        proof = await proof_of(request)
        return (
            proof is not None
            and self.max_age is not None
            and self.required() <= proven(proof.methods)
            and utc_now() - proof.auth_time <= self.max_age
        )

    async def has_object_permission(
        self, request: HTTPConnection, user: AnyUser, obj: object
    ) -> bool:
        """As ``has_permission``: given only to ``check_object_permissions``, too."""
        return await self.has_permission(request, user)

    async def refusal(self, request: HTTPConnection, user: AnyUser) -> Exception:
        """``401 step_up_required``, naming the age and the methods."""
        return StepUpRequired(cast(timedelta, self.max_age), self.required())


class ActionStepUp(StepUp):
    """``StepUp(for_=...)``: the request holds an authorization of one act (ADR 0144).

    The ``Action-Authorization`` header must hold a confirmed authorization
    of the purpose, of the user and of the request's login, unspent and
    live, whose code covers its row: one query by id. The rule writes
    nothing (a rule may be asked more than once, beside others): it keeps
    the authorization on the request, and the act's decorated service
    spends it (``requires_authorization``). Refused: ``401
    authorization_required`` without the header (``WWW-Authenticate`` as
    RFC 9470 says), ``403 action_not_authorized`` with one that does not
    hold, which never says why. Never while staff act as the user, nor on a
    WebSocket (``auth.E111`` refuses it there, and on an event stream).

    It asks for authorizations too (``domainkit.http.access.AsksAuthorization``):
    a ``ResourceRouter`` gives its route a twin, ``POST <path>/authorization``.
    """

    def required(self) -> frozenset[str]:
        """What confirming the act must prove: its action's, or the setting's."""
        action = self._action()
        if action is not None:
            return action.required()
        return frozenset(auth_settings().AUTHORIZATIONS.METHODS)

    def unprovable(self) -> frozenset[str]:
        """Nothing: what confirming an act proves is ``auth.E108``'s to check."""
        return frozenset()

    async def has_permission(self, request: HTTPConnection, user: AnyUser) -> bool:
        """The request holds a confirmed authorization of the act, kept for it."""
        from .actions import accepted_authorization

        if (
            not isinstance(user, AbstractBaseUser)
            or impersonated(request)
            or request.scope.get("type") != "http"
        ):
            return False
        purpose = cast(str, self.purpose)
        return await accepted_authorization(request, user, purpose) is not None

    async def refusal(self, request: HTTPConnection, user: AnyUser) -> Exception:
        """``401 authorization_required`` without the header; else ``403``."""
        from .actions import authorization_refusal

        return authorization_refusal(request, cast(str, self.purpose))

    def authorization_model(self) -> type[BaseModel]:
        """``{"id", "purpose", "shown", "expires_at", "methods"}``: the twin's body."""
        from .actions import ActionAuthorizationOut

        return ActionAuthorizationOut

    async def ask_authorization(
        self, request: Request, command: object, uow: "UnitOfWork | None" = None
    ) -> BaseModel:
        """Store an authorization of ``command`` for the request's user and login.

        ``uow`` is the unit of work the act runs in, not yet open, which a
        ``show(command, uow)`` reads in: the twin gives its route's.

        Raises:
            ImproperlyConfigured: No action is registered for the purpose.
            See ``request_action_authorization`` for the refusals.
        """
        from .actions import ActionAuthorizationOut, request_action_authorization

        action = self._action()
        if action is None:
            raise ImproperlyConfigured(
                f"StepUp(for_={self.purpose!r}): no registered action has that "
                "purpose; register_action(...) it in ready()."
            )
        authorization = await request_action_authorization(
            request,
            action,
            command,
            unit_of_work=None if uow is None else lambda: uow,
        )
        return ActionAuthorizationOut.of(authorization)

    def service_problem(self, service: object) -> str | None:
        """Why ``service``, the route's, would not spend what this rule asks for.

        A ``ResourceRouter`` asks when the route is declared, and refuses
        the route (ADR 0161): one that runs a use case itself, which spends
        nothing, or a service that spends another act's authorization,
        would run the act with arguments nobody confirmed. A service
        without the mark of ``requires_authorization`` may call one that
        has it, as in 2.4: it warns (``DomainkitDeprecationWarning``), and
        3.0 refuses it. None: it spends the authorization of this purpose.
        """
        from .actions import SPENDS

        spent = getattr(service, SPENDS, None)
        if spent == self.purpose:
            return None
        owner = getattr(service, "__self__", None)
        if isinstance(owner, UseCase):
            return (
                f"runs {owner} itself, which spends nothing: give use_case the "
                f"copy requires_authorization(...) makes of it for {self.purpose!r}"
            )
        named = getattr(service, "__qualname__", service)
        if spent is not None:
            return f"runs {named}, which spends the authorization of {spent!r}"
        warnings.warn(
            f"A route that asks StepUp(for_={self.purpose!r}) runs {named}, which "
            "is not decorated with requires_authorization: decorate the route's "
            "service itself. domainkit 3.0 refuses such a route.",
            DomainkitDeprecationWarning,
            stacklevel=2,
        )
        return None

    def _action(self) -> "Action | None":
        """The act's ``Action``: the one given, or the one registered."""
        from .actions import action_for

        return self.action or action_for(cast(str, self.purpose))


STANDS_FOR: Final[dict[str, frozenset[str]]] = {
    "hwk": frozenset({"otp"}),
    "mfa": frozenset({"pwd", "otp", "hwk"}),
}
"""What a method proves beyond its name: a key a device holds (``hwk``)
is a possession factor, as an authenticator app is; a passkey verified
with its user (``mfa``) is two factors on its own (NIST SP 800-63B)."""


def proven(methods: frozenset[str]) -> frozenset[str]:
    """What ``methods`` prove, with what each stands for (``STANDS_FOR``)."""
    return methods.union(*(STANDS_FOR.get(method, ()) for method in methods))


def impersonated(connection: HTTPConnection) -> bool:
    """Whether staff act as the user on ``connection`` (an impersonation)."""
    credentials = connection.scope.get("auth")
    return isinstance(credentials, Credentials) and credentials.impersonator is not None


async def proof_of(connection: HTTPConnection) -> Proof | None:
    """The step-up of the connection's credentials, read once a request."""
    scope = connection.scope
    if PROOF not in scope:
        credentials = scope.get("auth")
        scope[PROOF] = (
            await credentials.proof() if isinstance(credentials, Credentials) else None
        )
    proof: Proof | None = scope[PROOF]
    return proof
