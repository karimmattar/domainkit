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
(``authentication``), ``access`` decides what routes allow, and ``api``
has the endpoints to log in and out.
"""

from .access import (
    AllowAny,
    AuthenticatedUser,
    BasePermission,
    CurrentUser,
    HasObjectPermissions,
    HasPermissions,
    IsAdminUser,
    IsAuthenticated,
    IsAuthenticatedOrReadOnly,
    IsSuperuser,
    ModelPermissions,
    NotAuthenticated,
    Public,
    Require,
    check_object_permissions,
    current_user,
    mark_public,
    open_routes,
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
    "AllowAny",
    "AnonymousUser",
    "AnyUser",
    "AuthSettings",
    "AuthToken",
    "AuthUnitOfWork",
    "AuthenticatedUser",
    "AuthenticationFailed",
    "BaseAuthentication",
    "BaseBackend",
    "BasePermission",
    "Credentials",
    "CurrentUser",
    "Group",
    "GroupId",
    "GroupNameTaken",
    "GroupNotFound",
    "HasObjectPermissions",
    "HasPermissions",
    "InvalidGroupName",
    "InvalidPassword",
    "InvalidUserField",
    "InvalidUsername",
    "IsAdminUser",
    "IsAuthenticated",
    "IsAuthenticatedOrReadOnly",
    "IsSuperuser",
    "IssuedTokens",
    "JWTAuthentication",
    "JWTSettings",
    "ModelBackend",
    "ModelPermissions",
    "NotAuthenticated",
    "PasswordHash",
    "Permission",
    "Public",
    "Require",
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
    "amake_password",
    "authenticate",
    "check_object_permissions",
    "check_password",
    "current_user",
    "get_all_permissions",
    "get_backends",
    "get_user",
    "get_user_model",
    "has_perm",
    "has_perms",
    "make_password",
    "mark_public",
    "open_routes",
    "password_help_texts",
    "validate_password",
]
