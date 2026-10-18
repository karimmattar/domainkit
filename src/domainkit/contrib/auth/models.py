"""Users, groups, and permissions: the domain model of ``domainkit.contrib.auth``.

Plain classes, mapped onto tables in ``tables``. A user holds a
``PasswordHash`` made by ``hashers``, never a raw password. A permission
is named by its key, ``<domain>.<codename>`` (``billing.add_invoice``).

Swap the user for one of your own by subclassing ``AbstractUser`` (or
``AbstractBaseUser``) and naming it in ``AUTH.USER_MODEL``.
"""

import re
import secrets
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar, Final, NewType, Self
from uuid import UUID, uuid4

from domainkit.contrib.contenttypes.models import ContentType
from domainkit.ddd import AggregateRoot, Entity, ValueObject

from .events import (
    GroupCreated,
    GroupDeleted,
    GroupPermissionGranted,
    GroupPermissionRevoked,
    GroupRenamed,
    PasswordChanged,
    UserCreated,
    UserDeleted,
    UserJoinedGroup,
    UserLeftGroup,
    UserPermissionGranted,
    UserPermissionRevoked,
    UserStatusChanged,
    UserUpdated,
)
from .exceptions import InvalidGroupName, InvalidUsername, UnknownUserField

UserId = NewType("UserId", UUID)
GroupId = NewType("GroupId", UUID)

UNUSABLE_PASSWORD_PREFIX: Final = "!"  # noqa: S105 - a marker, not a password.
"""Starts every unusable password; no hasher's output starts with it."""
USERNAME_MAX_LENGTH: Final = 150
GROUP_NAME_MAX_LENGTH: Final = 150
_USERNAME: Final = re.compile(r"[\w.@+-]+")
_HASH: Final = re.compile(r"[a-z][a-z0-9_]*\$.+")


def _now() -> datetime:
    """Return the current time in UTC."""
    return datetime.now(UTC)


class PasswordHash(ValueObject):
    """A stored password: a hasher's ``<algorithm>$...``, or unusable (``!...``).

    Its own type keeps raw passwords out of users: only ``hashers`` make one.
    The value never appears in ``repr``, so hashes stay out of logs.
    """

    value: str

    def __post_init__(self) -> None:
        """Refuse anything that is neither a hash nor unusable."""
        if not (
            self.value.startswith(UNUSABLE_PASSWORD_PREFIX)
            or _HASH.fullmatch(self.value)
        ):
            raise ValueError(
                "A PasswordHash holds hashers.make_password output, never a raw "
                "password."
            )

    def __repr__(self) -> str:
        """Hide the hash."""
        return "PasswordHash(...)"

    @classmethod
    def unusable(cls) -> Self:
        """A password that never matches (an account that logs in otherwise)."""
        return cls(UNUSABLE_PASSWORD_PREFIX + secrets.token_urlsafe(30))

    @property
    def usable(self) -> bool:
        """Whether some password can match it."""
        return not self.value.startswith(UNUSABLE_PASSWORD_PREFIX)


class Permission(Entity[int]):
    """Permission to do something to one content type: ``<domain>.<codename>``.

    ``migrate`` creates them: ``add``, ``change``, ``delete``, and ``view``
    for every content type, and those its class declares in ``Meta``.
    """

    codename: str
    name: str
    content_type: ContentType

    @property
    def key(self) -> str:
        """``<domain>.<codename>``, as checked by ``has_perm``."""
        return f"{self.content_type.domain}.{self.codename}"

    def __str__(self) -> str:
        """The permission's key."""
        return self.key


class Group(AggregateRoot[GroupId]):
    """A named set of permissions; every member has them all."""

    name: str
    permissions: set[Permission] = field(default_factory=set)

    @classmethod
    def create(cls, name: str) -> Self:
        """Start a group with no permissions."""
        group = cls(id=GroupId(uuid4()), name=_group_name(name))
        group.record(GroupCreated(group_id=group.id, name=group.name))
        return group

    def rename(self, name: str) -> None:
        """Give the group a new name."""
        name = _group_name(name)
        if name != self.name:
            self.name = name
            self.record(GroupRenamed(group_id=self.id, name=name))

    def grant(self, *permissions: Permission) -> None:
        """Give the group (and its members) ``permissions``."""
        for permission in permissions:
            if permission not in self.permissions:
                self.permissions.add(permission)
                self.record(
                    GroupPermissionGranted(group_id=self.id, permission=permission.key)
                )

    def revoke(self, *permissions: Permission) -> None:
        """Take ``permissions`` back from the group."""
        for permission in permissions:
            if permission in self.permissions:
                self.permissions.discard(permission)
                self.record(
                    GroupPermissionRevoked(group_id=self.id, permission=permission.key)
                )

    def permission_keys(self) -> frozenset[str]:
        """The keys of the group's permissions."""
        return frozenset(permission.key for permission in self.permissions)

    def delete(self) -> None:
        """Record ``GroupDeleted``; the repository removes the group."""
        self.record(GroupDeleted(group_id=self.id, name=self.name))

    def __str__(self) -> str:
        """The group's name."""
        return self.name


class AbstractBaseUser(AggregateRoot[UserId]):
    """Credentials, status, groups, and permissions of a user.

    Subclass it (or ``AbstractUser``) for a custom user: name the login
    field in ``USERNAME_FIELD``, the email field in ``EMAIL_FIELD``, and
    the fields ``createsuperuser`` asks for in ``REQUIRED_FIELDS``.
    """

    USERNAME_FIELD: ClassVar[str]
    EMAIL_FIELD: ClassVar[str | None] = None
    REQUIRED_FIELDS: ClassVar[tuple[str, ...]] = ()
    PROTECTED_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "id",
            "version",
            "password",
            "last_login",
            "date_joined",
            "is_active",
            "is_staff",
            "is_superuser",
            "groups",
            "user_permissions",
        }
    )
    """Fields that change through their own methods, never ``update``."""
    EDITABLE_FIELDS: ClassVar[frozenset[str]] = frozenset()
    """Fields staff may change in the admin API (``update``).

    Name each one: a field left out, such as a custom ``risk_flag``, is
    neither shown to staff nor changed by them.
    """
    READ_ONLY_FIELDS: ClassVar[frozenset[str]] = frozenset()
    """Fields the admin API shows to staff but never changes."""
    is_authenticated: ClassVar[bool] = True
    is_anonymous: ClassVar[bool] = False

    password: PasswordHash
    last_login: datetime | None = None
    is_active: bool = True
    is_superuser: bool = False
    groups: set[Group] = field(default_factory=set)
    user_permissions: set[Permission] = field(default_factory=set)

    @classmethod
    def create(cls, *, password: PasswordHash, **fields: Any) -> Self:  # noqa: ANN401 - the fields of each user model.
        """Start a user and record ``UserCreated``."""
        user = cls(id=UserId(uuid4()), password=password, **fields)
        user.record(UserCreated(user_id=user.id, username=user.get_username()))
        return user

    @classmethod
    def normalize_username(cls, username: str) -> str:
        """NFKC-normalize, so look-alike characters cannot make a second account."""
        return unicodedata.normalize("NFKC", username)

    def get_username(self) -> str:
        """The value of ``USERNAME_FIELD``."""
        return str(getattr(self, self.USERNAME_FIELD))

    def set_password(self, password: PasswordHash) -> None:
        """Store a new password's hash."""
        self.password = password
        self.record(PasswordChanged(user_id=self.id))

    def set_unusable_password(self) -> None:
        """Make every password fail, as for an account that logs in otherwise."""
        self.set_password(PasswordHash.unusable())

    def has_usable_password(self) -> bool:
        """Whether some password can log this user in."""
        return self.password.usable

    def rehash_password(self, password: PasswordHash) -> None:
        """Store the same password hashed with current settings (no event)."""
        self.password = password

    def record_login(self, when: datetime | None = None) -> None:
        """Remember when the user last logged in."""
        self.last_login = when or _now()

    def set_active(self, value: bool) -> None:
        """Allow (or stop) the user from logging in and using permissions."""
        self._set_status("is_active", value)

    def set_superuser(self, value: bool) -> None:
        """Give (or take) every permission at once."""
        self._set_status("is_superuser", value)

    def join(self, group: Group) -> None:
        """Make the user a member of ``group``."""
        if group not in self.groups:
            self.groups.add(group)
            self.record(
                UserJoinedGroup(user_id=self.id, group_id=group.id, group=group.name)
            )

    def leave(self, group: Group) -> None:
        """End the user's membership of ``group``."""
        if group in self.groups:
            self.groups.discard(group)
            self.record(
                UserLeftGroup(user_id=self.id, group_id=group.id, group=group.name)
            )

    def grant(self, *permissions: Permission) -> None:
        """Give the user ``permissions`` directly."""
        for permission in permissions:
            if permission not in self.user_permissions:
                self.user_permissions.add(permission)
                self.record(
                    UserPermissionGranted(user_id=self.id, permission=permission.key)
                )

    def revoke(self, *permissions: Permission) -> None:
        """Take back permissions given to the user directly."""
        for permission in permissions:
            if permission in self.user_permissions:
                self.user_permissions.discard(permission)
                self.record(
                    UserPermissionRevoked(user_id=self.id, permission=permission.key)
                )

    def get_user_permissions(self) -> frozenset[str]:
        """Keys of permissions given directly; none while inactive."""
        if not self.is_active:
            return frozenset()
        return frozenset(permission.key for permission in self.user_permissions)

    def get_group_permissions(self) -> frozenset[str]:
        """Keys of permissions the user's groups give; none while inactive."""
        if not self.is_active:
            return frozenset()
        return frozenset().union(*(group.permission_keys() for group in self.groups))

    def get_all_permissions(self) -> frozenset[str]:
        """Keys of every permission granted, directly or through groups.

        A superuser has every permission without grants; ``has_perm`` knows,
        and ``backends.get_all_permissions`` lists them from the database.
        """
        return self.get_user_permissions() | self.get_group_permissions()

    def has_perm(self, perm: str) -> bool:
        """Whether an active user has ``perm``; superusers have them all.

        Object permissions (``obj``) come from ``backends.has_perm``.
        """
        if not self.is_active:
            return False
        return self.is_superuser or perm in self.get_all_permissions()

    def has_perms(self, perms: Iterable[str]) -> bool:
        """Whether the user has every one of ``perms``."""
        return all(self.has_perm(perm) for perm in perms)

    def has_domain_perms(self, domain: str) -> bool:
        """Whether the user has any permission in ``domain`` (``has_module_perms``)."""
        if not self.is_active:
            return False
        prefix = f"{domain}."
        return self.is_superuser or any(
            perm.startswith(prefix) for perm in self.get_all_permissions()
        )

    def _set_status(self, name: str, value: bool) -> None:
        """Change one status flag and record it."""
        if getattr(self, name) != value:
            setattr(self, name, value)
            self.record(UserStatusChanged(user_id=self.id, field=name, value=value))

    @classmethod
    def editable_fields(cls) -> frozenset[str]:
        """``EDITABLE_FIELDS``, never the username, credentials, or grants."""
        return cls.EDITABLE_FIELDS - cls.PROTECTED_FIELDS - {cls.USERNAME_FIELD}

    @classmethod
    def admin_fields(cls) -> frozenset[str]:
        """Fields the admin API shows: the editable and the read-only ones."""
        return cls.editable_fields() | cls.READ_ONLY_FIELDS

    @classmethod
    def check_editable(cls, names: Iterable[str]) -> None:
        """Refuse names outside ``editable_fields()``.

        Raises:
            UnknownUserField: Names the error lists.
        """
        if unknown := sorted(set(names) - cls.editable_fields()):
            raise UnknownUserField(
                f"These user fields cannot be changed here: {', '.join(unknown)}."
            )

    def update(self, **fields: object) -> None:
        """Change editable fields; record ``UserUpdated`` with their names.

        Raises:
            UnknownUserField: A field is not one of ``editable_fields()``.
        """
        self.check_editable(fields)
        changed = []
        for name, value in sorted(fields.items()):
            cleaned = self.clean_field(name, value)
            if getattr(self, name) != cleaned:
                setattr(self, name, cleaned)
                changed.append(name)
        if changed:
            self.record(UserUpdated(user_id=self.id, fields=tuple(changed)))

    def clean_field(self, name: str, value: object) -> object:
        """Tidy a value ``update`` stores; subclasses normalize their fields."""
        return value

    def delete(self) -> None:
        """Record ``UserDeleted``; the repository removes the user."""
        self.record(UserDeleted(user_id=self.id, username=self.get_username()))

    def __str__(self) -> str:
        """The username."""
        return self.get_username()


class AbstractUser(AbstractBaseUser):
    """A user with a username, an email, names, and a staff flag (Django's).

    Usernames hold letters, digits, and ``@ . + - _``, at most 150 of them.
    ``is_staff`` lets a user into the admin API (with the permissions too).
    """

    USERNAME_FIELD: ClassVar[str] = "username"
    EMAIL_FIELD: ClassVar[str | None] = "email"
    REQUIRED_FIELDS: ClassVar[tuple[str, ...]] = ("email",)
    EDITABLE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"email", "first_name", "last_name"}
    )
    READ_ONLY_FIELDS: ClassVar[frozenset[str]] = frozenset({"date_joined"})

    username: str
    email: str = ""
    first_name: str = ""
    last_name: str = ""
    is_staff: bool = False
    date_joined: datetime = field(default_factory=_now)

    @classmethod
    def create(cls, *, password: PasswordHash, **fields: Any) -> Self:  # noqa: ANN401 - the fields of each user model.
        """Check the username and tidy the email, then start the user."""
        fields["username"] = validate_username(
            cls.normalize_username(str(fields.get("username", "")))
        )
        fields["email"] = normalize_email(str(fields.get("email", "")))
        return super().create(password=password, **fields)

    def set_staff(self, value: bool) -> None:
        """Let the user into the admin API (or stop it)."""
        self._set_status("is_staff", value)

    def clean_field(self, name: str, value: object) -> object:
        """Lower-case the email's domain, as on creation."""
        return normalize_email(str(value)) if name == "email" else value

    def get_full_name(self) -> str:
        """First and last name, separated by a space."""
        return f"{self.first_name} {self.last_name}".strip()

    def get_short_name(self) -> str:
        """The first name."""
        return self.first_name


class User(AbstractUser):
    """The default user; ``AUTH.USER_MODEL`` swaps in a project's own."""


@dataclass(frozen=True, slots=True)
class AnonymousUser:
    """Whoever sends a request without credentials: no id, no permissions.

    Every instance equals every other.
    """

    id: ClassVar[None] = None
    is_active: ClassVar[bool] = False
    is_staff: ClassVar[bool] = False
    is_superuser: ClassVar[bool] = False
    is_authenticated: ClassVar[bool] = False
    is_anonymous: ClassVar[bool] = True

    def get_username(self) -> str:
        """Anonymous users have no username."""
        return ""

    def get_user_permissions(self) -> frozenset[str]:
        """None."""
        return frozenset()

    def get_group_permissions(self) -> frozenset[str]:
        """None."""
        return frozenset()

    def get_all_permissions(self) -> frozenset[str]:
        """None."""
        return frozenset()

    def has_perm(self, perm: str) -> bool:
        """Never."""
        return False

    def has_perms(self, perms: Iterable[str]) -> bool:
        """Never."""
        return False

    def has_domain_perms(self, domain: str) -> bool:
        """Never."""
        return False

    def __str__(self) -> str:
        """``AnonymousUser``."""
        return "AnonymousUser"


type AnyUser = AbstractBaseUser | AnonymousUser
"""A user that logged in, or the anonymous one."""


def validate_username(username: str) -> str:
    """Return ``username`` if it is 1 to 150 of letters, digits, and ``@.+-_``."""
    if len(username) > USERNAME_MAX_LENGTH or not _USERNAME.fullmatch(username):
        raise InvalidUsername(
            f"A username has 1 to {USERNAME_MAX_LENGTH} letters, digits, and "
            "@ . + - _ characters."
        )
    return username


def normalize_email(email: str) -> str:
    """Lower-case the domain part, which is case-insensitive."""
    local, at, domain = email.strip().rpartition("@")
    return f"{local}@{domain.lower()}" if at else email.strip()


def _group_name(name: str) -> str:
    """Return the trimmed name if it has 1 to 150 characters."""
    name = name.strip()
    if not 0 < len(name) <= GROUP_NAME_MAX_LENGTH:
        raise InvalidGroupName(
            f"A group name has 1 to {GROUP_NAME_MAX_LENGTH} characters."
        )
    return name
