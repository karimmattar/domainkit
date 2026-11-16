"""Rules for new passwords (Django's ``password_validation``).

``AUTH.PASSWORD_VALIDATORS`` lists them; ``validate_password`` runs every
one and raises ``InvalidPassword`` with all the rules a password breaks.
By default a password may not look like the user's own details, must have
at least 8 characters, may not be one of 20,000 common passwords, and may
not be only digits. ``AUTH.PRESET = "pci-dss-4"`` asks for 12
characters, letters and digits both (``LettersAndDigitsValidator``), and
none of the user's last four passwords (``AUTH.PASSWORD_HISTORY``, checked
where the password is stored, ``UserRepository.set_password``).
``AUTH.BREACHED_PASSWORDS`` adds ``BreachedPasswordValidator``, which asks
Have I Been Pwned's range API whether the password was in a data breach.

A validator is any class with ``validate(password, user)``, which raises
``PasswordValidationError``, and ``help_text()``. ``validate`` may be
``async``: ``avalidate_password`` awaits it, as the routes and commands
do, and ``validate_password`` refuses to run it (ADR 0148).
"""

import gzip
import hashlib
import inspect
import logging
import re
from collections.abc import Awaitable, Iterator, Sequence
from difflib import SequenceMatcher
from functools import cache
from pathlib import Path
from typing import ClassVar, Final, Protocol

from domainkit.exceptions import DomainkitError, ImproperlyConfigured, UpstreamError
from domainkit.i18n import gettext
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
    BreachedPasswordSettings,
    PasswordValidatorConfig,
    auth_settings,
)
from .tokens import IDLE_PRECISION, LAST_USED_EVERY

__all__ = [
    "BreachedPasswordValidator",
    "BreachedPasswordsUnavailable",
    "CommonPasswordValidator",
    "LettersAndDigitsValidator",
    "MinimumLengthValidator",
    "NumericPasswordValidator",
    "UserAttributeSimilarityValidator",
]

COMMON_PASSWORDS: Final = Path(__file__).with_name("common-passwords.txt.gz")
"""Django's list of 20,000 common passwords (see ``common-passwords.NOTICE``)."""
PREFIX_LENGTH: Final = 5
"""Characters of the SHA-1 the range API is sent; the rest never leaves."""
BREACHED: Final = "This password has appeared in a data breach: choose another one."
"""The refusal of a breached password (NIST SP 800-63B: give the reason)."""
BREACH_LINE: Final = re.compile(r"[0-9A-Fa-f]{35}:[0-9]{1,10}")
"""A line of the range API's answer: the rest of a SHA-1, then its count."""

logger = logging.getLogger(__name__)


class PasswordValidationError(ValueError, DomainkitError):
    """A password breaks one rule; the message says which."""


class BreachedPasswordsUnavailable(UpstreamError):
    """The breached-password check could not answer, and fails closed: 503."""

    status_code: ClassVar[int] = 503
    code: ClassVar[str] = "breached_passwords_unavailable"
    detail: ClassVar[str] = (
        "Passwords cannot be checked against data breaches now; try again later."
    )


class PasswordValidator(Protocol):
    """What ``PASSWORD_VALIDATORS`` classes provide."""

    def validate(
        self, password: str, user: object | None = None
    ) -> Awaitable[None] | None:
        """Raise ``PasswordValidationError`` if ``password`` breaks the rule.

        An ``async`` one is awaited by ``avalidate_password``.
        """
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


class BreachedPasswordValidator:
    """Not seen in a data breach, as Have I Been Pwned's range API says.

    Only the first five characters of the password's SHA-1 are sent, with
    ``Add-Padding: true`` so the answer's size does not tell them; the
    API answers every suffix it knows under them, and the rest of the
    hash is compared here (k-anonymity). The call goes through
    ``domainkit.http.client`` (the ``httpclient`` extra): bounded by
    ``TIMEOUT`` and the request's deadline, retried, and failing at once
    while the API's circuit is open. Logs name its origin, never the
    prefix. An answer that does not come in time, or does not read as
    one, lets the password through with a warning, or refuses it with
    ``BreachedPasswordsUnavailable`` under ``FAIL_CLOSED``.

    ``AUTH.BREACHED_PASSWORDS`` turns it on, after ``PASSWORD_VALIDATORS``,
    with its settings; one made by hand takes ``config``.
    """

    def __init__(self, config: BreachedPasswordSettings | None = None) -> None:
        """Check with ``config`` (default: ``BreachedPasswordSettings()``).

        Raises:
            ImproperlyConfigured: The ``httpclient`` extra is not installed.
        """
        self.config = BreachedPasswordSettings() if config is None else config
        try:
            from domainkit.http.client import HttpClient
        except ImportError as exc:
            raise ImproperlyConfigured(
                "AUTH.BREACHED_PASSWORDS needs the httpclient extra: "
                "pip install 'domainkit[httpclient]'"
            ) from exc
        self.client = HttpClient(
            timeout=self.config.TIMEOUT, propagate=False, log_paths=False
        )

    async def validate(self, password: str, user: object | None = None) -> None:
        """Refuse a password seen ``MIN_COUNT`` times or more in breaches.

        Raises:
            PasswordValidationError: It was.
            BreachedPasswordsUnavailable: The API did not answer, and
                ``FAIL_CLOSED`` is set.
            DeadlineExceeded: The request's deadline passed first.
        """
        import httpx2

        digest = hashlib.sha1(password.encode(), usedforsecurity=False)
        hexdigest = digest.hexdigest().upper()
        prefix, suffix = hexdigest[:PREFIX_LENGTH], hexdigest[PREFIX_LENGTH:]
        try:
            response = await self.client.get(
                self.config.URL + prefix, headers={"Add-Padding": "true"}
            )
            response.raise_for_status()
            count = dict(_breaches(response.text)).get(suffix, 0)
        except (httpx2.HTTPError, ValueError) as exc:
            closed = self.config.FAIL_CLOSED
            logger.warning(
                "The breached-password check failed (%s); the password was %s",
                type(exc).__name__,
                "refused" if closed else "let through",
            )
            if closed:
                raise BreachedPasswordsUnavailable(
                    f"The breached-password check failed: {type(exc).__name__}."
                ) from exc
            return
        if count >= self.config.MIN_COUNT:
            raise PasswordValidationError(BREACHED)

    def help_text(self) -> str:
        """Describe the rule."""
        return "Your password can't be one that has appeared in a data breach."


def _breaches(text: str) -> Iterator[tuple[str, int]]:
    """Each suffix the range API answered, upper case, and its count.

    Padding lines count 0, so ``MIN_COUNT`` (1 or more) never refuses on
    them.

    Raises:
        ValueError: A line is not ``<35 hex digits>:<count>``, or there is
            none: an answer from something else, such as a proxy's page.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("The range API answered nothing.")
    for line in lines:
        if not BREACH_LINE.fullmatch(line):
            raise ValueError("The range API's answer does not read as one.")
        found, _, count = line.partition(":")
        yield found.upper(), int(count)


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


@cache
def breach_validators(
    config: BreachedPasswordSettings,
) -> tuple[PasswordValidator, ...]:
    """The breached-password check ``config`` turns on, if it does; made once."""
    return (BreachedPasswordValidator(config),) if config.ENABLED else ()


def get_password_validators() -> tuple[PasswordValidator, ...]:
    """The validators ``AUTH.PASSWORD_VALIDATORS`` configures.

    Then the breached-password check, with ``AUTH.BREACHED_PASSWORDS``.
    """
    config = auth_settings()
    return load_validators(config.PASSWORD_VALIDATORS) + breach_validators(
        config.BREACHED_PASSWORDS
    )


def validate_password(
    password: str,
    user: object | None = None,
    validators: Sequence[PasswordValidator] | None = None,
) -> None:
    """Raise ``InvalidPassword`` listing every rule ``password`` breaks.

    Pass the user (or what is known of a new one) so passwords like the
    username are refused. Async code calls ``avalidate_password``.

    Raises:
        InvalidPassword: A rule refuses the password.
        ImproperlyConfigured: A validator is ``async``: it would never run
            here, so the password is not taken unchecked.
    """
    errors = []
    for validator in get_password_validators() if validators is None else validators:
        try:
            checked = validator.validate(password, user)
        except PasswordValidationError as exc:
            errors.append(gettext(str(exc)))
            continue
        if inspect.isawaitable(checked):
            if inspect.iscoroutine(checked):
                checked.close()
            raise ImproperlyConfigured(
                f"{type(validator).__name__} checks passwords asynchronously: "
                "await avalidate_password() instead of validate_password()."
            )
    if errors:
        raise InvalidPassword(errors)


async def avalidate_password(
    password: str,
    user: object | None = None,
    validators: Sequence[PasswordValidator] | None = None,
) -> None:
    """``validate_password`` for async code: ``async`` validators are awaited.

    Every rule runs, in order, whether or not an earlier one refused, so
    the error lists them all. An exception other than
    ``PasswordValidationError`` (``BreachedPasswordsUnavailable``) goes up.

    Raises:
        InvalidPassword: A rule refuses the password.
    """
    errors = []
    for validator in get_password_validators() if validators is None else validators:
        try:
            checked = validator.validate(password, user)
            if inspect.isawaitable(checked):
                await checked
        except PasswordValidationError as exc:
            errors.append(gettext(str(exc)))
    if errors:
        raise InvalidPassword(errors)


def password_help_texts() -> list[str]:
    """What each configured rule asks of a password, for API clients."""
    return [gettext(validator.help_text()) for validator in get_password_validators()]


def preset_contradictions(config: AuthSettings) -> list[str]:
    """What ``config`` sets against its ``PRESET``; empty without one.

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
    if config.PRESET != PCI_DSS_4:
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
