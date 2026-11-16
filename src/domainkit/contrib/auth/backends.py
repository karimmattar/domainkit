"""Who a user is, and what they may do (Django's authentication backends).

``AUTH.BACKENDS`` lists backend classes, asked in order:

- ``authenticate`` returns the first user a backend accepts;
- ``has_perm`` is true when any backend grants the permission;
- a backend that raises ``PermissionDenied`` ends either search with "no";
- an active superuser has every permission, on every object, before any
  backend is asked: ``has_perm`` says yes, and ``get_all_permissions``
  lists every one.

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

``MembershipBackend`` grants what a user holds as a member of the tenant
being served (``TENANCY.MEMBER_PERMISSIONS``, ADR 0150). Such grants hold
there only (``tenant_scoped``): ``has_perm`` counts them, but
``get_all_permissions``, which every ceiling reads, leaves them out.

Within a request, a backend that reads its grants from the database
answers each user and object once (``memoised``): ``AuthenticationMiddleware``
opens the memo for each HTTP request with credentials, and
``memoised_permissions()`` opens one for a block of other work. A commit
in it drops what it kept, so a grant or a revocation made there is seen
at once.
"""

from collections.abc import Awaitable, Callable, Hashable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from functools import cache
from typing import Any, ClassVar, Final, cast
from uuid import UUID

from domainkit.conf import settings
from domainkit.ddd import PermissionDenied
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.live import LONG_LIVED
from domainkit.tenancy import member_permissions
from domainkit.utils.module_loading import import_string

from .hashers import amake_password, averify_password
from .models import AbstractBaseUser, AnyUser, Group, PasswordHash, UserId
from .settings import auth_settings
from .uow import AuthUnitOfWork
from .users import get_user_model

__all__ = [
    "BaseBackend",
    "MembershipBackend",
    "ModelBackend",
    "ObjectGrant",
    "authenticate",
    "backends_without_object_grants",
    "forget_permissions",
    "get_all_permissions",
    "get_user",
    "has_perm",
    "has_perms",
    "memoised",
    "memoised_permissions",
    "object_grants",
]

MAX_MEMOISED: Final = 1024
"""Answers one memo keeps; past it, the rest are worked out each time."""


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

    tenant_scoped: ClassVar[bool] = False
    """Whether what it grants holds only in the tenant being served.

    ``has_perm`` counts such grants there. ``get_all_permissions``, which
    every ceiling reads (what staff may grant, change, or touch through the
    admin API; the scopes a token may carry), leaves them out, so they never
    reach beyond the tenant (ADR 0150).
    """

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
        """Check an ``identifier`` (or ``username``) and ``password``.

        The identifier is any field of ``AUTH.IDENTIFIERS``, such as an
        email address or a mobile number (``find_by_identifier``); the
        ``USERNAME_FIELD`` and ``username`` are read too, as before 1.8.
        An unknown identifier costs the same time as a wrong password, so
        response times do not reveal which accounts exist.
        """
        model = get_user_model()
        identifier = credentials.get("identifier")
        if identifier is None:
            identifier = credentials.get(
                model.USERNAME_FIELD, credentials.get("username")
            )
        password = credentials.get("password")
        if not isinstance(identifier, str) or not isinstance(password, str):
            return None
        async with AuthUnitOfWork() as uow:
            user = await uow.users.find_by_identifier(identifier)
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


class MembershipBackend(BaseBackend):
    """What a user holds as a member of the tenant being served.

    ``TENANCY.MEMBER_PERMISSIONS`` names the keys, such as a role's, for
    the user and the current tenant; the request asked it once, when it
    was admitted (``domainkit.tenancy.member_permissions``). With no
    tenant current, the backend grants nothing. Inactive users have
    nothing. Object permissions (``obj``) are left to other backends, as
    ``ModelBackend`` leaves them, and so it grants on no object.

    Its grants hold in the tenant only (``tenant_scoped``): ``has_perm``
    counts them, and no ceiling does, so a member hands out none of them,
    and scopes no token with them, beyond what they hold everywhere.
    """

    tenant_scoped: ClassVar[bool] = True

    def __init__(self) -> None:
        """Refuse to be configured where nothing names a member's permissions.

        Raises:
            ImproperlyConfigured: ``TENANCY.MEMBER_PERMISSIONS`` is not set.
        """
        config = settings.TENANCY
        if config is None or config.MEMBER_PERMISSIONS is None:
            raise ImproperlyConfigured(
                "MembershipBackend grants what TENANCY.MEMBER_PERMISSIONS "
                "names: set it to the function that says what a member holds."
            )

    async def get_all_permissions(
        self, user: AnyUser, obj: object | None = None
    ) -> frozenset[str]:
        """The keys an active ``user`` holds in the current tenant (none on ``obj``)."""
        if obj is not None or not user.is_active:
            return frozenset()
        return await member_permissions(user)

    async def object_grants(
        self, holder: AbstractBaseUser | Group
    ) -> frozenset[ObjectGrant]:
        """None: a member's keys are not grants on single objects."""
        return frozenset()


class PermissionMemo:
    """What backends answered within one request, or one block (``memoised``).

    Current from when it is made (``open_memo``) until it is closed
    (``close_memo``), once the request or the block ends: a task that
    inherited it and outlives them works its answers out again.
    """

    __slots__ = ("answers", "scope", "token")

    def __init__(self, scope: Mapping[str, Any] | None = None) -> None:
        """Keep nothing yet, and be current (``scope``: the request's)."""
        self.answers: dict[Hashable, object] | None = {}
        """The answers, by key; None once closed."""
        self.scope = scope
        """The ASGI scope of the request it belongs to, if any."""
        self.token: Token[PermissionMemo | None] = _memo.set(self)
        """What makes the memo current before this one current again."""

    def kept(self) -> dict[Hashable, object] | None:
        """The answers to read and add to; None once closed or long-lived.

        An event stream (``LONG_LIVED``) outlasts any request: what it asks
        while it streams is worked out each time, as before 2.4.
        """
        scope = self.scope
        if scope is not None and scope.get(LONG_LIVED):
            return None
        return self.answers


_memo: ContextVar[PermissionMemo | None] = ContextVar("permission_memo", default=None)


def open_memo(scope: Mapping[str, Any] | None = None) -> PermissionMemo:
    """Memoise backends' answers until ``close_memo`` closes the memo it gives.

    ``AuthenticationMiddleware`` calls it for each HTTP request with
    credentials, with the request's ``scope``. Five calls open and close
    one: every such request pays them (``benchmarks``, rung ``me``).
    """
    return PermissionMemo(scope)


def close_memo(memo: PermissionMemo) -> None:
    """Drop what ``memo`` kept, and keep none; make the one before it current."""
    memo.answers = None
    _memo.reset(memo.token)


@contextmanager
def memoised_permissions() -> Iterator[None]:
    """Answer each question about a user and an object once, inside the block.

    For work outside a request: a task, a command. Requests with
    credentials get one from ``AuthenticationMiddleware``. A commit inside
    drops what was kept (``forget_permissions``).
    """
    memo = open_memo()
    try:
        yield
    finally:
        close_memo(memo)


def forget_permissions() -> None:
    """Drop what the current request or block kept; it keeps answers again.

    Auth calls it after each unit of work commits, so a grant or a
    revocation is seen at once. Call it after changing grants some other way.
    """
    memo = _memo.get()
    if memo is not None and memo.answers:
        memo.answers.clear()


async def memoised[T](key: Hashable, compute: Callable[[], Awaitable[T]]) -> T:
    """``compute()``'s answer for ``key``, once per request or block.

    Outside one, ``compute()`` runs each time, as before 2.4. ``key`` names
    everything the answer depends on, starting with something of the
    caller's own (its class), so two backends never read each other's
    answers. For answers that depend only on stored rows, not on the
    object's fields: those may change within a request. ``MAX_MEMOISED``
    answers are kept at most.
    """
    memo = _memo.get()
    answers = None if memo is None else memo.kept()
    if answers is None:
        return await compute()
    if key in answers:
        return cast(T, answers[key])
    answer = await compute()
    if len(answers) < MAX_MEMOISED:
        answers[key] = answer
    return answer


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
    user: AnyUser, obj: object | None = None, *, tenant_scoped: bool = False
) -> frozenset[str]:
    """Keys of every permission any backend grants ``user`` (on ``obj``).

    What the user holds beyond any one tenant, which every ceiling reads:
    what staff may grant, change, or touch through the admin API, and the
    scopes a personal token or a sign-in approval may carry. A backend
    whose grants hold only in the tenant being served (``tenant_scoped``,
    such as ``MembershipBackend``) is left out, so a member's role never
    raises a ceiling (ADR 0150). ``tenant_scoped=True`` adds its keys, as
    ``has_perm`` counts them, for a listing of what the user may do in the
    current request (``/auth/me``).

    For an active superuser they are every permission, on an object too,
    as ``has_perm`` answers before any backend is asked, and whatever the
    backends add (user report A11).
    """
    granted: set[str] = set()
    if user.is_active and user.is_superuser:
        async with AuthUnitOfWork() as uow:
            granted |= await uow.permissions.keys()
    for backend in get_backends():
        if backend.tenant_scoped and not tenant_scoped:
            continue
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

    Inactive users have nothing; active superusers have everything, on
    every object, before any backend is asked: no backend narrows them.
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
