"""A user with a phone number, and a risk flag staff never see."""

from domainkit.contrib.auth import AbstractUser


class Account(AbstractUser):
    """The project's own user."""

    phone: str = ""
    risk_flag: bool = False

    REQUIRED_FIELDS = ("email", "phone")
    EDITABLE_FIELDS = AbstractUser.EDITABLE_FIELDS | {"phone"}
