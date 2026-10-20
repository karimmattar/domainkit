"""Object permissions: one grant of one permission on one object, as an aggregate.

A grant names its object by content type (``catalog.product``) and id, and
whom it is for: a user or a group, never both. The id is kept in the
column of its type (``object_uuid``, ``object_int``, or ``object_text``),
so the database compares it with the object's own id column as it is.
"""

from dataclasses import field
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


class ObjectPermissionGranted(DomainEvent):
    """A user or a group may now do something to one object."""

    grant_id: UUID
    content_type: str
    object_id: str
    permission: str
    user_id: UUID | None
    group_id: UUID | None
    by: UUID | None


class ObjectPermissionRevoked(DomainEvent):
    """A grant on one object was taken back."""

    grant_id: UUID
    content_type: str
    object_id: str
    permission: str
    user_id: UUID | None
    group_id: UUID | None
    by: UUID | None


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
        )
        setattr(grant, object_column(type(object_id)), object_id)
        grant.record(
            ObjectPermissionGranted(
                grant_id=grant.id,
                content_type=content_type,
                object_id=str(object_id),
                permission=permission,
                user_id=user_id,
                group_id=group_id,
                by=by,
            )
        )
        return grant

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
            )
        )


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
