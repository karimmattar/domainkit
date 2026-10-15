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
"""

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
    InvalidGroupName,
    InvalidPassword,
    InvalidUsername,
    UnknownPermission,
    UsernameTaken,
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
from .settings import AuthSettings
from .uow import AuthUnitOfWork
from .users import get_user_model
from .validators import password_help_texts, validate_password

__all__ = [
    "AbstractBaseUser",
    "AbstractUser",
    "AnonymousUser",
    "AnyUser",
    "AuthSettings",
    "AuthUnitOfWork",
    "BaseBackend",
    "Group",
    "GroupId",
    "GroupNameTaken",
    "InvalidGroupName",
    "InvalidPassword",
    "InvalidUsername",
    "ModelBackend",
    "PasswordHash",
    "Permission",
    "UnknownPermission",
    "User",
    "UserId",
    "UsernameTaken",
    "acheck_password",
    "amake_password",
    "authenticate",
    "check_password",
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
