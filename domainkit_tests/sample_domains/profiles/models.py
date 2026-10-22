"""A profile: a user with addresses that load with it, and notes that do not.

They are mapped onto the profile by this domain (``tables``), not declared
as its fields: the auth app maps each field of a user onto a column.
"""

from uuid import UUID

from domainkit.contrib.auth import AbstractUser
from domainkit.ddd import Entity


class Address(Entity[UUID]):
    """Where the profile's owner lives."""

    profile_id: UUID
    city: str


class Note(Entity[UUID]):
    """Something staff wrote about the profile."""

    profile_id: UUID
    text: str


class Profile(AbstractUser):
    """The project's own user, with relationships of its own."""
