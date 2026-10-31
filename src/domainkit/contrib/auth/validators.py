"""Rules for new passwords (Django's ``password_validation``).

``AUTH.PASSWORD_VALIDATORS`` lists them; ``validate_password`` runs every
one and raises ``InvalidPassword`` with all the rules a password breaks.
By default a password may not look like the user's own details, must have
at least 8 characters, may not be one of 20,000 common passwords, and may
not be only digits. ``AUTH.PASSWORD_PRESET = "pci-dss-4"`` asks for 12
characters, letters and digits both (``LettersAndDigitsValidator``), and
none of the user's last four passwords (``AUTH.PASSWORD_HISTORY``, checked
where the password is stored, ``UserRepository.set_password``).

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

from domainkit.exceptions import DomainkitError, ImproperlyConfigured
from domainkit.utils.module_loading import import_string
from domainkit.utils.rates import parse_rate

from .exceptions import InvalidPassword
from .settings import (
    PCI_ATTEMPTS,
    PCI_DSS_4,
    PCI_HISTORY,
    PCI_IDLE,
    PCI_LOCKOUT,
    PCI_MIN_LENGTH,
    AuthSettings,
    PasswordValidatorConfig,
    auth_settings,
)
from .tokens import IDLE_PRECISION, LAST_USED_EVERY

__all__ = [
    "CommonPasswordValidator",
    "LettersAndDigitsValidator",
    "MinimumLengthValidator",
    "NumericPasswordValidator",
    "UserAttributeSimilarityValidator",
]

COMMON_PASSWORDS: Final = Path(__file__).with_name("common-passwords.txt.gz")
"""Django's list of 20,000 common passwords (see ``common-passwords.NOTICE``)."""


class PasswordValidationError(ValueError, DomainkitError):
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


class LettersAndDigitsValidator:
    """At least one letter and one digit, as PCI DSS v4.0 asks (8.3.6).

    Letters and digits of any script count (``str.isalpha``,
    ``str.isdecimal``): Arabic letters and Arabic-Indic digits too.
    """

    def validate(self, password: str, user: object | None = None) -> None:
        """Refuse passwords without a letter, or without a digit."""
        if not (
            any(character.isalpha() for character in password)
            and any(character.isdecimal() for character in password)
        ):
            raise PasswordValidationError(
                "This password must contain both letters and digits."
            )

    def help_text(self) -> str:
        """Describe the rule."""
        return "Your password must contain at least one letter and one digit."


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


def preset_contradictions(config: AuthSettings) -> list[str]:
    """What ``config`` sets against its ``PASSWORD_PRESET``; empty without one.

    For ``"pci-dss-4"``: validators that take passwords under 12
    characters, or without letters and digits both (8.3.6); a history of
    fewer than 4 (8.3.7); staff-set passwords not changed at their first
    use (8.3.5); more than 10 wrong passwords, or more than 10 every 30
    minutes, per account (8.3.4); and logins idle for more than 15 minutes
    (8.2.8, ``IDLE_TIMEOUT`` and the tenth by which an ending lags it; a
    JSON Web Token's ``ACCESS_TOKEN_TTL`` and ``LEEWAY``, for which an
    access token works unrefreshed); and password checks of signed-in users
    counted apart from logins (8.3.4, ``PASSWORD_CHECKS_COUNT_AS_LOGINS``).
    """
    if config.PASSWORD_PRESET != PCI_DSS_4:
        return []
    found = []
    validators = load_validators(config.PASSWORD_VALIDATORS)
    if not any(
        isinstance(each, MinimumLengthValidator) and each.min_length >= PCI_MIN_LENGTH
        for each in validators
    ):
        found.append(
            f"PASSWORD_VALIDATORS take passwords under {PCI_MIN_LENGTH} "
            "characters (8.3.6): give MinimumLengthValidator "
            f'{{"min_length": {PCI_MIN_LENGTH}}} or more'
        )
    if not any(isinstance(each, LettersAndDigitsValidator) for each in validators):
        found.append(
            "PASSWORD_VALIDATORS take passwords without both letters and digits "
            "(8.3.6): add LettersAndDigitsValidator"
        )
    if config.PASSWORD_HISTORY < PCI_HISTORY:
        found.append(
            f"PASSWORD_HISTORY is {config.PASSWORD_HISTORY}, under "
            f"{PCI_HISTORY} (8.3.7)"
        )
    if not config.PASSWORD_CHANGE_ON_FIRST_USE:
        found.append(
            "PASSWORD_CHANGE_ON_FIRST_USE is off: passwords staff set are not "
            "changed at their first use (8.3.5)"
        )
    if config.LOGIN_RATE is None or _over(config.LOGIN_RATE):
        found.append(
            f"LOGIN_RATE {config.LOGIN_RATE!r} allows more than {PCI_ATTEMPTS} wrong "
            f"passwords, or more than {PCI_ATTEMPTS} every "
            f"{int(PCI_LOCKOUT.total_seconds()) // 60} minutes (8.3.4)"
        )
    idle = config.IDLE_TIMEOUT
    if idle is None or idle + min(LAST_USED_EVERY, idle / IDLE_PRECISION) > PCI_IDLE:
        found.append(
            f"IDLE_TIMEOUT {idle} ends idle logins more than "
            f"{int(PCI_IDLE.total_seconds()) // 60} minutes after their last use "
            "(8.2.8): a login ends up to a tenth after it, so 13 minutes at most"
        )
    jwt = config.JWT
    if _signs_jwts(config) and jwt.ACCESS_TOKEN_TTL + jwt.LEEWAY > PCI_IDLE:
        found.append(
            f"JWT.ACCESS_TOKEN_TTL {jwt.ACCESS_TOKEN_TTL} and LEEWAY {jwt.LEEWAY} "
            "let a JSON Web Token login idle that long, unrefreshed (8.2.8): "
            f"{int(PCI_IDLE.total_seconds()) // 60} minutes together at most"
        )
    if not config.PASSWORD_CHECKS_COUNT_AS_LOGINS:
        found.append(
            "PASSWORD_CHECKS_COUNT_AS_LOGINS is off: password checks of "
            "signed-in users guess on a count of their own, besides the "
            "login's (8.3.4)"
        )
    return found


def _signs_jwts(config: AuthSettings) -> bool:
    """Whether a class of ``AUTHENTICATION_CLASSES`` is ``JWTAuthentication``'s.

    A path that does not import is ``auth``'s own startup error to report.
    """
    from .authentication import JWTAuthentication  # Imports this module.

    for path in config.AUTHENTICATION_CLASSES:
        try:
            found = import_string(path)
        except ImportError:
            continue
        if isinstance(found, type) and issubclass(found, JWTAuthentication):
            return True
    return False


def _over(rate: str) -> bool:
    """Whether ``rate`` passes more than PCI's 10 attempts, or 10 per 30 minutes."""
    limit, period = parse_rate(rate)
    return limit > PCI_ATTEMPTS or limit * PCI_LOCKOUT.total_seconds() > (
        PCI_ATTEMPTS * period
    )


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
