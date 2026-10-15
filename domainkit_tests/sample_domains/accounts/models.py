"""A user with a phone number."""

from domainkit.contrib.auth import AbstractUser


class Account(AbstractUser):
    """The project's own user."""

    phone: str = ""

    REQUIRED_FIELDS = ("email", "phone")
