"""A member: an email instead of a username, and no staff flag."""

from typing import ClassVar

from domainkit.contrib.auth import AbstractBaseUser


class Member(AbstractBaseUser):
    """Logs in with an email address."""

    USERNAME_FIELD: ClassVar[str] = "email"
    EMAIL_FIELD: ClassVar[str | None] = "email"

    email: str
