"""Rules for new passwords (Django's ``password_validation``).

``AUTH.PASSWORD_VALIDATORS`` lists them; ``validate_password`` runs every
one and raises ``InvalidPassword`` with all the rules a password breaks.
By default a password may not look like the user's own details, must have
at least 8 characters, may not be one of 20,000 common passwords, and may
not be only digits.

A validator is any class with ``validate(password, user)``, which raises
``PasswordValidationError``, and ``help_text()``.
"""

import gzip
import re
from collections.abc import Sequence
from difflib import SequenceMatcher
from functools import cache
from pathlib import Path
from typing import Final, Protocol

from domainkit.exceptions import ImproperlyConfigured
from domainkit.utils.module_loading import import_string

from .exceptions import InvalidPassword
from .settings import PasswordValidatorConfig, auth_settings

__all__ = [
    "CommonPasswordValidator",
    "MinimumLengthValidator",
    "NumericPasswordValidator",
    "UserAttributeSimilarityValidator",
]

COMMON_PASSWORDS: Final = Path(__file__).with_name("common-passwords.txt.gz")
"""Django's list of 20,000 common passwords (see ``common-passwords.NOTICE``)."""


class PasswordValidationError(ValueError):
    """A password breaks one rule; the message says which."""


class PasswordValidator(Protocol):
    """What ``PASSWORD_VALIDATORS`` classes provide."""

    def validate(self, password: str, user: object | None = None) -> None:
        """Raise ``PasswordValidationError`` if ``password`` breaks the rule."""
        ...

    def help_text(self) -> str:
        """Describe the rule to the person choosing a password."""
        ...


class MinimumLengthValidator:
    """At least ``min_length`` characters (default 8)."""

    def __init__(self, min_length: int = 8) -> None:
        """Require ``min_length`` characters."""
        self.min_length = min_length

    def validate(self, password: str, user: object | None = None) -> None:
        """Refuse shorter passwords."""
        if len(password) < self.min_length:
            raise PasswordValidationError(
                f"This password is too short. It must contain at least "
                f"{self.min_length} characters."
            )

    def help_text(self) -> str:
        """Name the minimum."""
        return f"Your password must contain at least {self.min_length} characters."


class NumericPasswordValidator:
    """Not only digits."""

    def validate(self, password: str, user: object | None = None) -> None:
        """Refuse passwords made only of digits."""
        if password.isdigit():
            raise PasswordValidationError("This password is entirely numeric.")

    def help_text(self) -> str:
        """Describe the rule."""
        return "Your password can't be entirely numeric."


class CommonPasswordValidator:
    """Not on a list of common passwords (default: Django's 20,000).

    The list is one lower-case password per line, gzipped or plain.
    """

    def __init__(self, password_list_path: str | Path = COMMON_PASSWORDS) -> None:
        """Read the list (once per path)."""
        self.passwords = _password_list(Path(password_list_path))

    def validate(self, password: str, user: object | None = None) -> None:
        """Refuse passwords on the list, whatever their case."""
        if password.lower().strip() in self.passwords:
            raise PasswordValidationError("This password is too common.")

    def help_text(self) -> str:
        """Describe the rule."""
        return "Your password can't be a commonly used password."


class UserAttributeSimilarityValidator:
    """Not too much like the user's username, names, or email.

    Each attribute is compared whole and in parts (an email's local part
    and domain), with ``difflib``; ``max_similarity`` is between 0.1 and 1.
    """

    DEFAULT_USER_ATTRIBUTES: Final = ("username", "first_name", "last_name", "email")

    def __init__(
        self,
        user_attributes: Sequence[str] = DEFAULT_USER_ATTRIBUTES,
        max_similarity: float = 0.7,
    ) -> None:
        """Compare with ``user_attributes`` up to ``max_similarity``."""
        if max_similarity < 0.1:
            raise ImproperlyConfigured("max_similarity must be at least 0.1.")
        self.user_attributes = tuple(user_attributes)
        self.max_similarity = max_similarity

    def validate(self, password: str, user: object | None = None) -> None:
        """Refuse passwords too much like one of the user's attributes."""
        if user is None:
            return
        password = password.lower()
        for name in self.user_attributes:
            value = getattr(user, name, None)
            if isinstance(value, str) and value and self._similar(password, value):
                label = name.replace("_", " ")
                raise PasswordValidationError(
                    f"The password is too similar to the {label}."
                )

    def help_text(self) -> str:
        """Describe the rule."""
        return "Your password can't be too similar to your other personal information."

    def _similar(self, password: str, value: str) -> bool:
        """Whether ``password`` is like ``value`` or one of its parts."""
        value = value.lower()
        return any(
            part
            and SequenceMatcher(a=password, b=part).quick_ratio() >= self.max_similarity
            for part in [value, *re.split(r"\W+", value)]
        )


@cache
def load_validators(
    configs: tuple[PasswordValidatorConfig, ...],
) -> tuple[PasswordValidator, ...]:
    """Instantiate the validators ``configs`` names, with their options."""
    validators = []
    for config in configs:
        try:
            cls = import_string(config.NAME)
            validators.append(cls(**config.OPTIONS))
        except (ImportError, TypeError) as exc:
            raise ImproperlyConfigured(
                f"AUTH.PASSWORD_VALIDATORS: {config.NAME}: {exc}"
            ) from exc
    return tuple(validators)


def get_password_validators() -> tuple[PasswordValidator, ...]:
    """The validators ``AUTH.PASSWORD_VALIDATORS`` configures."""
    return load_validators(auth_settings().PASSWORD_VALIDATORS)


def validate_password(
    password: str,
    user: object | None = None,
    validators: Sequence[PasswordValidator] | None = None,
) -> None:
    """Raise ``InvalidPassword`` listing every rule ``password`` breaks.

    Pass the user (or what is known of a new one) so passwords like the
    username are refused.
    """
    errors = []
    for validator in get_password_validators() if validators is None else validators:
        try:
            validator.validate(password, user)
        except PasswordValidationError as exc:
            errors.append(str(exc))
    if errors:
        raise InvalidPassword(errors)


def password_help_texts() -> list[str]:
    """What each configured rule asks of a password, for API clients."""
    return [validator.help_text() for validator in get_password_validators()]


@cache
def _password_list(path: Path) -> frozenset[str]:
    """Read a password list, gzipped or plain, once."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ImproperlyConfigured(f"Cannot read the password list {path}.") from exc
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return frozenset(line.strip() for line in data.decode().splitlines())
