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

The names that moved still import from here, and from
``domainkit.contrib.auth``, until 0.11; each use warns
(``DomainkitDeprecationWarning``).
"""

from collections.abc import Awaitable, Callable
from typing import Annotated, ClassVar, Final, cast

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import HTTPConnection

from domainkit.deprecation import moved
from domainkit.http import access

from .authentication import get_authenticators
from .models import AbstractBaseUser, AnyUser

MOVED: Final = frozenset(
    {
        "MODEL_ACTIONS",
        "SAFE_METHODS",
        "AllOf",
        "AllowAny",
        "AnyOf",
        "BasePermission",
        "HasObjectPermissions",
        "HasPermissions",
        "HasScope",
        "IsAuthenticated",
        "IsAuthenticatedOrReadOnly",
        "IsUnscoped",
        "ModelPermissions",
        "Not",
        "NotAuthenticated",
        "Public",
        "Require",
        "Rule",
        "bearer",
        "check_object_permissions",
        "mark_public",
        "open_routes",
        "public",
        "routes_need_users",
        "scopes_of",
        "unguarded_routes",
    }
)
"""Names that moved to ``domainkit.http.access`` in 0.10; removed here in 0.11."""
CORE_ACCESS: Final = "domainkit.http.access"


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

__getattr__ = moved(__name__, dict.fromkeys(MOVED, CORE_ACCESS), "0.11")
