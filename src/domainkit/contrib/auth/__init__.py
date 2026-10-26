"""Users, groups, and permissions for REST APIs (Django's ``contrib.auth``).

Install it with ``domainkit.contrib.contenttypes``, before it or after::

    INSTALLED_DOMAINS = [
        "domainkit.contrib.contenttypes",
        "domainkit.contrib.auth",
        ...
    ]

then ``makemigrations``, ``migrate`` (which creates the permissions), and
``createsuperuser``. In code::

    user = await authenticate(username="alice", password=password)
    if user is not None and await has_perm(user, "billing.add_invoice"):
        ...

    async with AuthUnitOfWork() as uow:
        user = await uow.users.create_user("bob", password, email="b@x.org")
        editors = await uow.groups.create("editors")
        editors.grant(await uow.permissions.get("billing.change_invoice"))
        user.join(editors)
        await uow.commit()

``AUTH.USER_MODEL`` swaps the user for a project's own (see ``tables``).

Requests: ``AuthenticationMiddleware`` finds the user from a token
(``authentication``), and ``api`` has the endpoints to log in and out. What
routes allow is the core's ``domainkit.http.access``, for any
authentication (ADR 0080); ``access`` adds the rules about this app's users
(``IsAdminUser``, ``IsSuperuser``, ``AuthenticatedUser``, and ``CurrentUser``
typed as this app's users). The rule names
that moved there still import from here until 0.11, with a
``DomainkitDeprecationWarning``.
"""

from typing import Final

from domainkit.deprecation import moved

from .access import (
    AuthenticatedUser,
    CurrentUser,
    IsAdminUser,
    IsSuperuser,
    acurrent_user,
    current_user,
)
from .authentication import (
    AuthenticationFailed,
    BaseAuthentication,
    Credentials,
    IssuedTokens,
    JWTAuthentication,
    TokenAuthentication,
)
from .backends import (
    BaseBackend,
    ModelBackend,
    authenticate,
    get_all_permissions,
    get_backends,
    get_user,
    has_perm,
    has_perms,
)
from .exceptions import (
    GroupNameTaken,
    GroupNotFound,
    InvalidEmail,
    InvalidGroupName,
    InvalidPassword,
    InvalidUserField,
    InvalidUsername,
    UnknownGroup,
    UnknownPermission,
    UnknownUserField,
    UsernameTaken,
    UserNotFound,
)
from .hashers import (
    acheck_password,
    amake_password,
    check_password,
    make_password,
)
from .models import (
    AbstractBaseUser,
    AbstractUser,
    AnonymousUser,
    AnyUser,
    Group,
    GroupId,
    PasswordHash,
    Permission,
    User,
    UserId,
)
from .settings import AuthSettings, JWTSettings
from .tokens import AuthToken, TokenKind
from .uow import AuthUnitOfWork
from .users import get_user_model
from .validators import password_help_texts, validate_password

__all__ = [
    "AbstractBaseUser",
    "AbstractUser",
    "AnonymousUser",
    "AnyUser",
    "AuthSettings",
    "AuthToken",
    "AuthUnitOfWork",
    "AuthenticatedUser",
    "AuthenticationFailed",
    "BaseAuthentication",
    "BaseBackend",
    "Credentials",
    "CurrentUser",
    "Group",
    "GroupId",
    "GroupNameTaken",
    "GroupNotFound",
    "InvalidEmail",
    "InvalidGroupName",
    "InvalidPassword",
    "InvalidUserField",
    "InvalidUsername",
    "IsAdminUser",
    "IsSuperuser",
    "IssuedTokens",
    "JWTAuthentication",
    "JWTSettings",
    "ModelBackend",
    "PasswordHash",
    "Permission",
    "TokenAuthentication",
    "TokenKind",
    "UnknownGroup",
    "UnknownPermission",
    "UnknownUserField",
    "User",
    "UserId",
    "UserNotFound",
    "UsernameTaken",
    "acheck_password",
    "acurrent_user",
    "amake_password",
    "authenticate",
    "check_password",
    "current_user",
    "get_all_permissions",
    "get_backends",
    "get_user",
    "get_user_model",
    "has_perm",
    "has_perms",
    "make_password",
    "password_help_texts",
    "validate_password",
]

MOVED: Final = frozenset(
    {
        "AllowAny",
        "BasePermission",
        "HasObjectPermissions",
        "HasPermissions",
        "HasScope",
        "IsAuthenticated",
        "IsAuthenticatedOrReadOnly",
        "IsUnscoped",
        "ModelPermissions",
        "NotAuthenticated",
        "Public",
        "Require",
        "check_object_permissions",
        "mark_public",
        "open_routes",
    }
)
"""Names that moved to ``domainkit.http.access`` in 0.10; removed here in 0.11."""

__getattr__ = moved(__name__, dict.fromkeys(MOVED, "domainkit.http.access"), "0.11")
