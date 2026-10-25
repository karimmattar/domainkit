"""A user that deleting marks deleted and keeps (``SoftDeletable``)."""

from domainkit.contrib.auth import AbstractUser
from domainkit.ddd import SoftDeletable


class ArchivedUser(SoftDeletable, AbstractUser):
    """Kept when deleted, for the records; it must not log in again."""
