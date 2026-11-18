"""A user whose address the table keeps as bytes."""

from domainkit.contrib.auth import AbstractUser


class Sealed(AbstractUser):
    """The project's own user."""
