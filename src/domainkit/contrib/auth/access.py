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

The rule names that moved to the core in 0.10 were removed from here, and
from ``domainkit.contrib.auth``, in 0.11: import them from
``domainkit.http.access``.
"""

from collections.abc import Awaitable, Callable, Collection
from datetime import timedelta
from typing import Annotated, ClassVar, Final, cast

from fastapi import Depends, status
from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import HTTPConnection

from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import access
from domainkit.http.errors import APIError

from .authentication import Credentials, Proof, get_authenticators
from .models import AbstractBaseUser, AnyUser
from .settings import MAX_STEP_UP_AGE, Factor, auth_settings, with_a_factor
from .tokens import utc_now

PROOF: Final = "domainkit.auth.proof"
"""Where a request keeps its credentials' step-up, once read."""
STEPPED_UP: Final = "domainkit.auth.stepped_up"
"""Where a request keeps the ``StepUp`` rules it passed (an open connection's
re-checks pass them)."""
INSUFFICIENT: Final = "insufficient_user_authentication"
"""The ``WWW-Authenticate`` error that asks for a step-up (RFC 9470)."""

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
    _: Annotated[HTTPAuthorizationCredentials | None, Depends(access.bearer)],
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
    if they say so. A login is not a step-up, with a second factor or
    without. A refusal answers ``401 step_up_required``
    (``StepUpRequired``). An open WebSocket or event stream keeps what its
    start proved: its re-checks pass this rule.
    """

    message: ClassVar[str] = "Confirm it is you: authenticate again to do this."

    def __init__(
        self, max_age: timedelta, *, methods: Collection[Factor] | None = None
    ) -> None:
        """Require a step-up within ``max_age`` (a day at most).

        Raises:
            ImproperlyConfigured: ``max_age`` is not positive or exceeds a
                day, or ``methods`` lacks ``otp`` or repeats one.
        """
        if not timedelta(0) < max_age <= MAX_STEP_UP_AGE:
            raise ImproperlyConfigured(
                "StepUp(max_age=...) must be more than 0 and a day at most."
            )
        if methods is not None:
            try:
                methods = with_a_factor(tuple(methods))
            except ValueError as exc:
                raise ImproperlyConfigured(f"StepUp(methods=...): {exc}.") from None
        self.max_age = max_age
        self.methods = None if methods is None else frozenset(methods)

    def required(self) -> frozenset[str]:
        """What the step-up must have proved: ``methods``, or the setting's."""
        if self.methods is not None:
            return self.methods
        return frozenset(auth_settings().STEP_UP.METHODS)

    async def has_permission(self, request: HTTPConnection, user: AnyUser) -> bool:
        """The credentials stepped up within ``max_age``, proving ``required()``."""
        if not user.is_authenticated:
            return False
        passed: set[StepUp] = request.scope.setdefault(STEPPED_UP, set())
        if self in passed:
            return True
        proof = await proof_of(request)
        if (
            proof is None
            or not self.required() <= proof.methods
            or utc_now() - proof.auth_time > self.max_age
        ):
            return False
        passed.add(self)
        return True

    async def refusal(self, request: HTTPConnection, user: AnyUser) -> StepUpRequired:
        """``401 step_up_required``, naming the age and the methods."""
        return StepUpRequired(self.max_age, self.required())


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
