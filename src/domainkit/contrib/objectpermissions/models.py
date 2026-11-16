"""Object permissions: one grant of one permission on one object, as an aggregate.

A grant names its object by content type (``catalog.product``) and id, and
whom it is for: a user or a group, never both. The id is kept in the
column of its type (``object_uuid``, ``object_int``, or ``object_text``),
so the database compares it with the object's own id column as it is.

A grant may expire (``expires_at``): from that moment it no longer counts,
wherever grants are read. It may also name the role it came with
(``role``): an ``ObjectRole`` is a named set of permissions granted and
revoked together, one grant per permission.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Final, NewType, Self
from uuid import UUID, uuid4

from domainkit.clock import now
from domainkit.ddd import AggregateRoot, DomainEvent, MaxLength

ObjectPermissionId = NewType("ObjectPermissionId", UUID)
KEY_LENGTH: Final = 255
"""The longest content type, permission key, or text id."""

type ObjectId = UUID | int | str
"""What objects' ids may be."""

PERMISSION_KEY: Final = re.compile(r"\w+\.\w+")
"""A permission's key: a domain's label and a codename (``catalog.view_product``)."""


class ObjectPermissionGranted(DomainEvent):
    """A user or a group may now do something to one object."""

    grant_id: UUID
    content_type: str
    object_id: str
    permission: str
    user_id: UUID | None
    group_id: UUID | None
    by: UUID | None
    expires_at: datetime | None = None
    """When the grant stops counting; None: never. Granting again with
    another expiry records the same grant (``grant_id``) granted again."""
    role: str | None = None
    """The ``ObjectRole`` it was granted with; None for a permission alone."""


class ObjectPermissionRevoked(DomainEvent):
    """A grant on one object was taken back."""

    grant_id: UUID
    content_type: str
    object_id: str
    permission: str
    user_id: UUID | None
    group_id: UUID | None
    by: UUID | None
    role: str | None = None
    """The ``ObjectRole`` it was granted with; None for a permission alone."""


class ObjectPermission(AggregateRoot[ObjectPermissionId]):
    """``permission`` on one object, for one user or one group."""

    content_type: Annotated[str, MaxLength(KEY_LENGTH)]
    permission: Annotated[str, MaxLength(KEY_LENGTH)]
    object_uuid: UUID | None = None
    object_int: int | None = None
    object_text: Annotated[str, MaxLength(KEY_LENGTH)] | None = None
    user_id: UUID | None = None
    group_id: UUID | None = None
    granted_by: UUID | None = None
    granted_at: datetime = field(default_factory=now)
    expires_at: datetime | None = None
    """When it stops counting (from that moment on); None: never."""
    role: Annotated[str, MaxLength(KEY_LENGTH)] | None = None
    """The ``ObjectRole`` it was granted with; None for a permission alone."""

    class Meta:
        """Django's four permissions; ``add`` and ``delete`` grant and revoke."""

        verbose_name = "object permission"

    @classmethod
    def create(
        cls,
        *,
        content_type: str,
        permission: str,
        object_id: ObjectId,
        user_id: UUID | None,
        group_id: UUID | None,
        by: UUID | None,
        expires_at: datetime | None = None,
        role: str | None = None,
    ) -> Self:
        """A new grant, recorded as granted.

        Raises:
            ValueError: Not exactly one of ``user_id`` and ``group_id``.
            TypeError: The id is not a UUID, an integer, or text.
        """
        if (user_id is None) == (group_id is None):
            raise ValueError("A grant is for a user or for a group.")
        grant = cls(
            id=ObjectPermissionId(uuid4()),
            content_type=content_type,
            permission=permission,
            user_id=user_id,
            group_id=group_id,
            granted_by=by,
            expires_at=expires_at,
            role=role,
        )
        setattr(grant, object_column(type(object_id)), object_id)
        grant.record(grant._granted(by))
        return grant

    def renew(self, expires_at: datetime | None, by: UUID | None) -> None:
        """Make the grant last until ``expires_at`` (None: no end), granted again."""
        self.expires_at = expires_at
        self.record(self._granted(by))

    def expired(self, at: datetime | None = None) -> bool:
        """Whether it no longer counts at ``at`` (default: now)."""
        return self.expires_at is not None and (at or now()) >= self.expires_at

    def _granted(self, by: UUID | None) -> ObjectPermissionGranted:
        """The event of this grant as it stands, granted by ``by``."""
        return ObjectPermissionGranted(
            grant_id=self.id,
            content_type=self.content_type,
            object_id=str(self.object_id),
            permission=self.permission,
            user_id=self.user_id,
            group_id=self.group_id,
            by=by,
            expires_at=self.expires_at,
            role=self.role,
        )

    @property
    def object_id(self) -> ObjectId:
        """The object's id, whichever column holds it."""
        if self.object_uuid is not None:
            return self.object_uuid
        if self.object_int is not None:
            return self.object_int
        return self.object_text or ""

    def revoke(self, by: UUID | None) -> None:
        """Record that the grant is taken back; the repository removes it."""
        self.record(
            ObjectPermissionRevoked(
                grant_id=self.id,
                content_type=self.content_type,
                object_id=str(self.object_id),
                permission=self.permission,
                user_id=self.user_id,
                group_id=self.group_id,
                by=by,
                role=self.role,
            )
        )


@dataclass(frozen=True, slots=True, init=False)
class ObjectRole:
    """A named set of permissions on objects, granted and revoked together.

    ``ObjectRole("editor", ["catalog.view_product", "catalog.change_product"])``:
    ``grant_role`` grants each permission on one object, as one grant each
    named after the role, and ``revoke_role`` takes back those grants.
    """

    name: str
    permissions: tuple[str, ...]
    """The permission keys, sorted, each once."""

    def __init__(self, name: str, permissions: Iterable[str]) -> None:
        """Name the role and its permissions.

        Raises:
            ValueError: The name is empty or longer than 255 characters, a
                key is not ``domain.codename``, or there is no permission.
        """
        keys = tuple(sorted(set(permissions)))
        if not 1 <= len(name) <= KEY_LENGTH:
            raise ValueError(f"A role's name has 1 to {KEY_LENGTH} characters.")
        if not keys:
            raise ValueError(f"Role {name!r} has no permission.")
        for key in keys:
            if not PERMISSION_KEY.fullmatch(key):
                raise ValueError(
                    f"Role {name!r} names {key!r}, not a permission key "
                    "(domain.codename)."
                )
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "permissions", keys)


def object_column(kind: type) -> str:
    """The column that holds ids of type ``kind``.

    Raises:
        TypeError: It is not a UUID, an integer, or text.
    """
    if issubclass(kind, UUID):
        return "object_uuid"
    if issubclass(kind, int) and not issubclass(kind, bool):
        return "object_int"
    if issubclass(kind, str):
        return "object_text"
    raise TypeError(
        f"Object permissions take UUID, integer, or text ids, not {kind.__name__}."
    )
