"""Who a user is, and what they may do (Django's authentication backends).

``AUTH.BACKENDS`` lists backend classes, asked in order:

- ``authenticate`` returns the first user a backend accepts;
- ``has_perm`` is true when any backend grants the permission;
- a backend that raises ``PermissionDenied`` ends either search with "no".

``ModelBackend`` checks passwords against the user table and grants what
users and their groups were given. Write a backend to check credentials
elsewhere (a directory service), or to grant object permissions::

    class OwnerBackend(BaseBackend):
        async def has_perm(self, user, perm, obj=None):
            return perm == "billing.change_invoice" and obj.owner_id == user.id

A backend that stores grants on single objects also says which it holds
for a user or a group (``object_grants``), so the admin API bounds staff
by them as by model permissions: staff change no one, and join no group,
holding a grant they lack (AUTH-14).
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cache
from uuid import UUID

from domainkit.ddd import PermissionDenied
from domainkit.exceptions import ImproperlyConfigured
from domainkit.utils.module_loading import import_string

from .hashers import amake_password, averify_password
from .models import AbstractBaseUser, AnyUser, Group, PasswordHash, UserId
from .settings import auth_settings
from .uow import AuthUnitOfWork
from .users import get_user_model

__all__ = [
    "BaseBackend",
    "ModelBackend",
    "ObjectGrant",
    "authenticate",
    "backends_without_object_grants",
    "get_all_permissions",
    "get_user",
    "has_perm",
    "has_perms",
    "object_grants",
]


@dataclass(frozen=True, slots=True)
class ObjectGrant:
    """``permission`` granted on one object: its content type and its id."""

    permission: str
    """A permission key: ``shop.change_product``."""
    content_type: str
    """The object's content type: ``shop.product``."""
    object_id: object
    """The object's id: a UUID, an integer, or text."""

    def __str__(self) -> str:
        """``shop.change_product on shop.product 42``."""
        return f"{self.permission} on {self.content_type} {self.object_id}"


class BaseBackend:
    """A backend that knows no one and grants nothing; override what you need."""

    async def authenticate(
        self, credentials: Mapping[str, object]
    ) -> AbstractBaseUser | None:
        """The user ``credentials`` prove, or None."""
        return None

    async def get_user(self, user_id: UUID) -> AbstractBaseUser | None:
        """The user with ``user_id`` this backend vouches for, or None."""
        return None

    async def get_all_permissions(
        self, user: AnyUser, obj: object | None = None
    ) -> frozenset[str]:
        """Keys of the permissions this backend grants ``user`` (on ``obj``)."""
        return frozenset()

    async def has_perm(
        self, user: AnyUser, perm: str, obj: object | None = None
    ) -> bool:
        """Whether this backend grants ``perm`` to ``user`` (on ``obj``)."""
        return perm in await self.get_all_permissions(user, obj)

    async def object_grants(
        self, holder: AbstractBaseUser | Group
    ) -> frozenset[ObjectGrant]:
        """What this backend grants ``holder`` on single objects.

        For a user, their own grants and their groups', whether the user is
        active or not (what they would hold once active); for a group, its
        own. A backend that grants on objects must answer it: the admin
        API bounds staff by it (AUTH-14). A backend that grants on no
        object answers it too, with an empty set: one that leaves this
        default lets only superusers change users and groups there
        (``backends_without_object_grants``, ``auth.W007``).
        """
        return frozenset()


class ModelBackend(BaseBackend):
    """Passwords from the user table; permissions of users and their groups.

    Inactive users neither log in nor have permissions. Object permissions
    (``obj``) are left to other backends.
    """

    async def authenticate(
        self, credentials: Mapping[str, object]
    ) -> AbstractBaseUser | None:
        """Check ``username`` (or the ``USERNAME_FIELD``) and ``password``.

        An unknown username costs the same time as a wrong password, so
        response times do not reveal which usernames exist.
        """
        model = get_user_model()
        username = credentials.get(model.USERNAME_FIELD, credentials.get("username"))
        password = credentials.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            return None
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get_by_natural_key(username)
        if user is None:
            await amake_password(password)
            return None
        check = await averify_password(password, user.password.value)
        if not check.correct or not self.user_can_authenticate(user):
            return None
        if check.rehashed is not None:
            await self._rehash(user.id, user.password, check.rehashed)
            # The login is held to the hash it checked (``api.held_login``).
            user.rehash_password(PasswordHash(check.rehashed))
        return user

    def user_can_authenticate(self, user: AbstractBaseUser) -> bool:
        """Whether ``user`` may log in at all: only active users may."""
        return user.is_active

    async def get_user(self, user_id: UUID) -> AbstractBaseUser | None:
        """The active user with ``user_id``."""
        async with AuthUnitOfWork() as uow:
            user = await uow.users.find(UserId(user_id))
        return user if user is not None and self.user_can_authenticate(user) else None

    async def get_all_permissions(
        self, user: AnyUser, obj: object | None = None
    ) -> frozenset[str]:
        """What an active user was granted; every permission for a superuser."""
        if obj is not None or not user.is_active:
            return frozenset()
        if user.is_superuser:
            async with AuthUnitOfWork() as uow:
                return await uow.permissions.keys()
        return user.get_all_permissions()

    async def has_perm(
        self, user: AnyUser, perm: str, obj: object | None = None
    ) -> bool:
        """Answer from the loaded user, without a query."""
        return obj is None and user.has_perm(perm)

    async def _rehash(
        self, user_id: UserId, verified: PasswordHash, rehashed: str
    ) -> None:
        """Store a password hash made with current settings.

        On its own, without the user's ``version``: logins that rehash the
        same password at once must not refuse each other. Only while the
        ``verified`` hash is still stored, so a new password is never undone.
        """
        async with AuthUnitOfWork() as uow:
            await uow.users.store_rehash(user_id, verified, PasswordHash(rehashed))
            await uow.commit()


@cache
def load_backends(paths: tuple[str, ...]) -> tuple[BaseBackend, ...]:
    """Instantiate the backend classes named in ``paths``."""
    backends = []
    for path in paths:
        try:
            backend = import_string(path)
        except ImportError as exc:
            raise ImproperlyConfigured(f"AUTH.BACKENDS: {exc}") from exc
        if not (isinstance(backend, type) and issubclass(backend, BaseBackend)):
            raise ImproperlyConfigured(f"AUTH.BACKENDS: {path!r} is not a BaseBackend.")
        backends.append(backend())
    return tuple(backends)


def get_backends() -> tuple[BaseBackend, ...]:
    """The configured backends, in order."""
    return load_backends(auth_settings().BACKENDS)


async def authenticate(**credentials: object) -> AbstractBaseUser | None:
    """The user the credentials prove, by the first backend that accepts them."""
    for backend in get_backends():
        try:
            user = await backend.authenticate(credentials)
        except PermissionDenied:
            return None
        if user is not None:
            return user
    return None


async def get_user(user_id: UUID) -> AbstractBaseUser | None:
    """The user with ``user_id``, from the first backend that has it."""
    for backend in get_backends():
        if (user := await backend.get_user(user_id)) is not None:
            return user
    return None


async def get_all_permissions(
    user: AnyUser, obj: object | None = None
) -> frozenset[str]:
    """Keys of every permission any backend grants ``user`` (on ``obj``)."""
    granted: set[str] = set()
    for backend in get_backends():
        granted |= await backend.get_all_permissions(user, obj)
    return frozenset(granted)


def backends_without_object_grants() -> list[str]:
    """The configured backends that do not say what they grant on objects.

    Those that keep ``BaseBackend.object_grants``, which answers nothing,
    but ``ModelBackend``, which grants on no object. Such a backend may
    grant on objects all the same (``has_perm(user, perm, obj)``), so the
    admin API lets only superusers change users and groups while one is
    configured (SEC-16a-5); one that grants on no object says so by
    answering ``object_grants`` with an empty set.
    """
    return [
        f"{type(backend).__module__}.{type(backend).__qualname__}"
        for backend in get_backends()
        if not isinstance(backend, ModelBackend)
        and type(backend).object_grants is BaseBackend.object_grants
    ]


async def object_grants(holder: AbstractBaseUser | Group) -> frozenset[ObjectGrant]:
    """What every backend grants ``holder`` (a user or a group) on single objects."""
    granted: set[ObjectGrant] = set()
    for backend in get_backends():
        granted |= await backend.object_grants(holder)
    return frozenset(granted)


async def has_perm(user: AnyUser, perm: str, obj: object | None = None) -> bool:
    """Whether ``user`` has ``perm`` (on ``obj``).

    Inactive users have nothing; active superusers have everything.
    """
    if not user.is_active:
        return False
    if user.is_superuser:
        return True
    for backend in get_backends():
        try:
            if await backend.has_perm(user, perm, obj):
                return True
        except PermissionDenied:
            return False
    return False


async def has_perms(
    user: AnyUser, perms: Iterable[str], obj: object | None = None
) -> bool:
    """Whether ``user`` has every one of ``perms`` (on ``obj``)."""
    for perm in perms:
        if not await has_perm(user, perm, obj):
            return False
    return True
