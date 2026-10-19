"""What happens to users and groups; the audit trail records each one.

Events carry ids, names, and permission keys, never passwords or hashes.
Usernames are personal data: the audit trail keeps them as pseudonyms.
"""

from typing import Annotated
from uuid import UUID

from domainkit.ddd import DomainEvent, Personal


class UserCreated(DomainEvent):
    """A user was created."""

    user_id: UUID
    username: Annotated[str, Personal(of="user_id")]


class PasswordChanged(DomainEvent):
    """A user's password was set, changed, or made unusable."""

    user_id: UUID


class UserLoggedIn(DomainEvent):
    """A user logged in and got tokens."""

    user_id: UUID


class UserLoggedOut(DomainEvent):
    """A user logged out: of this session, or ``everywhere``."""

    user_id: UUID
    everywhere: bool


class RefreshTokenReused(DomainEvent):
    """A used refresh token came back; the login was ended, as it may be stolen."""

    user_id: UUID
    family: UUID


class TokenCreated(DomainEvent):
    """A long-lived token was made for a user (``createtoken``)."""

    user_id: UUID
    name: str


class UserUpdated(DomainEvent):
    """Fields of a user changed (names only: values may be personal data)."""

    user_id: UUID
    fields: tuple[str, ...]


class UserDeleted(DomainEvent):
    """A user was deleted."""

    user_id: UUID
    username: Annotated[str, Personal(of="user_id")]


class UserStatusChanged(DomainEvent):
    """``is_active``, ``is_staff``, or ``is_superuser`` changed."""

    user_id: UUID
    field: str
    value: bool


class UserJoinedGroup(DomainEvent):
    """A user became a member of a group."""

    user_id: UUID
    group_id: UUID
    group: str


class UserLeftGroup(DomainEvent):
    """A user stopped being a member of a group."""

    user_id: UUID
    group_id: UUID
    group: str


class UserPermissionGranted(DomainEvent):
    """A permission was given to a user directly."""

    user_id: UUID
    permission: str


class UserPermissionRevoked(DomainEvent):
    """A permission given to a user directly was taken back."""

    user_id: UUID
    permission: str


class GroupCreated(DomainEvent):
    """A group was created."""

    group_id: UUID
    name: str


class GroupDeleted(DomainEvent):
    """A group was deleted; its members lost its permissions."""

    group_id: UUID
    name: str


class GroupRenamed(DomainEvent):
    """A group got a new name."""

    group_id: UUID
    name: str


class GroupPermissionGranted(DomainEvent):
    """A permission was given to a group (and so to its members)."""

    group_id: UUID
    permission: str


class GroupPermissionRevoked(DomainEvent):
    """A permission was taken back from a group."""

    group_id: UUID
    permission: str
